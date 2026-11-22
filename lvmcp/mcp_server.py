#!/usr/bin/env python3
"""MCP server: lets an AI agent build and edit LabVIEW VIs through the lvmcp scripting server VI.

    MCP client (Claude Code, Claude Desktop, ...)  --stdio-->  this process  --TCP-->  lvmcp_server.vi in LabVIEW

Run (from the repo):  .venv/bin/python lvmcp/mcp_server.py --port 5577 --boot-dir <boot folder as LabVIEW sees it>
All options also come from environment variables (LVMCP_HOST, LVMCP_PORT, ...); see `--help` and docs/mcp.md.

The tools are thin wrappers over `lvmcp.ops.LabVIEW` (docs/ops.md). The connection to LabVIEW is made on the
first tool call and re-made after a connection loss.
"""
from __future__ import annotations

import argparse
import atexit
import difflib
import functools
import importlib
import io
import json
import logging
import os
import re
import signal
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import Field  # noqa: E402

from mcp.server.mcpserver import Image, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from lvmcp import __version__  # noqa: E402
from lvmcp.lvclient import LVError  # noqa: E402
from lvmcp.ops import STRUCTURES, LabVIEW, OpError, private_boot_copy  # noqa: E402

log = logging.getLogger("lvmcp")

# ------------------------------------------------------------------------------------------------ configuration

ENV = {
    "host": ("LVMCP_HOST", "127.0.0.1"),
    "port": ("LVMCP_PORT", "5577"),
    "token": ("LVMCP_TOKEN", ""),
    "boot_dir": ("LVMCP_BOOT_DIR", str(ROOT / "labview" / "boot")),
    "boot_prefix": ("LVMCP_BOOT_PREFIX", ""),
    "boot_copy_from": ("LVMCP_BOOT_COPY_FROM", ""),
    "gen_dir": ("LVMCP_GEN_DIR", str(ROOT / "build" / "gen")),
    "cache_dir": ("LVMCP_CACHE_DIR", ""),
    "prefix": ("LVMCP_PREFIX", "lvmcp_"),
    "timeout": ("LVMCP_TIMEOUT", "60"),
    "palette": ("LVMCP_PALETTE", "build"),
    "palette_cache": ("LVMCP_PALETTE_CACHE", ""),
    "max_image": ("LVMCP_MAX_IMAGE", "1600"),
    "autostart": ("LVMCP_AUTOSTART", "auto"),
    "labview": ("LVMCP_LABVIEW", ""),
    "server_vi": ("LVMCP_SERVER_VI", str(ROOT / "labview" / "lvmcp_server.vi")),
}


def parse_args(argv=None) -> argparse.Namespace:
    def d(k):
        env, default = ENV[k]
        return os.environ.get(env, default)

    ap = argparse.ArgumentParser(
        description="MCP server for building/editing LabVIEW VIs through lvmcp_server.vi. Every option can also be "
                    "set by the environment variable shown.")
    ap.add_argument("--host", default=d("host"), help="host of the lvmcp server VI [LVMCP_HOST, 127.0.0.1]")
    ap.add_argument("--port", type=int, default=int(d("port")), help="its TCP port [LVMCP_PORT, 5577]")
    ap.add_argument("--token", default=d("token"), help="its token [LVMCP_TOKEN, empty]")
    ap.add_argument("--boot-dir", default=d("boot_dir"),
                    help="folder of the boot wrapper VIs AS LABVIEW SEES IT (Windows/UNC paths ok) "
                         "[LVMCP_BOOT_DIR, <repo>/labview/boot]")
    ap.add_argument("--boot-prefix", default=d("boot_prefix"),
                    help="file-name prefix of the boot wrappers (private copies) [LVMCP_BOOT_PREFIX, empty]")
    ap.add_argument("--boot-copy-from", default=d("boot_copy_from"),
                    help="if set: copy the boot wrappers from this local folder to --boot-dir as "
                         "<boot-prefix><name>.vi first (several clients on one LabVIEW; local paths only) "
                         "[LVMCP_BOOT_COPY_FROM]")
    ap.add_argument("--gen-dir", default=d("gen_dir"),
                    help="folder for generated wrapper VIs AS LABVIEW SEES IT [LVMCP_GEN_DIR, <repo>/build/gen]")
    ap.add_argument("--cache-dir", default=d("cache_dir"),
                    help="LOCAL folder for the wrapper index and style table; must belong to --gen-dir "
                         "[LVMCP_CACHE_DIR, default = --gen-dir]")
    ap.add_argument("--prefix", default=d("prefix"),
                    help="file-name prefix of generated wrappers (unique per client of one LabVIEW) "
                         "[LVMCP_PREFIX, lvmcp_]")
    ap.add_argument("--timeout", type=float, default=float(d("timeout")),
                    help="seconds to wait for one server reply [LVMCP_TIMEOUT, 60]")
    ap.add_argument("--palette", choices=("auto", "build", "off"), default=d("palette"),
                    help="find_object source: auto = palette table (lvmcp.palette) if already built, else the "
                         "style table; build = build the palette table on first use (~1 min); off = style "
                         "table only [LVMCP_PALETTE, auto]")
    ap.add_argument("--palette-cache", default=d("palette_cache"),
                    help="local folder of the palette table [LVMCP_PALETTE_CACHE, default = --cache-dir]")
    ap.add_argument("--max-image", type=int, default=int(d("max_image")),
                    help="images larger than this (px, longest side) are scaled down [LVMCP_MAX_IMAGE, 1600]")
    ap.add_argument("--autostart", choices=("auto", "on", "off"), default=d("autostart"),
                    help="when the server VI is not running, open it (it runs when opened) and wait for it: auto = "
                         "on Windows for a local server [LVMCP_AUTOSTART, auto]")
    ap.add_argument("--labview", default=d("labview"),
                    help="LabVIEW executable used by --autostart (default: the program Windows opens .vi files "
                         "with) [LVMCP_LABVIEW]")
    ap.add_argument("--server-vi", default=d("server_vi"),
                    help="server VI opened by --autostart [LVMCP_SERVER_VI, <package>/labview/lvmcp_server.vi]")
    ap.add_argument("--log-level", default=os.environ.get("LVMCP_LOG_LEVEL", "WARNING"))
    a = ap.parse_args(argv)
    for k in ("boot_dir", "gen_dir", "cache_dir", "palette_cache", "boot_copy_from"):
        setattr(a, k, _abs(getattr(a, k)))
    a.cache_dir = a.cache_dir or a.gen_dir
    a.palette_cache = a.palette_cache or a.cache_dir
    return a


def _abs(p: str) -> str:
    """Relative paths mean nothing to LabVIEW: resolve them against the current directory. Absolute POSIX,
    Windows drive (C:\\...) and UNC (\\\\server\\share) paths are kept as they are."""
    if not p or p.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", p):
        return p
    return str(Path(p).resolve())


# ------------------------------------------------------------------------------------------------ state

class State:
    """The LabVIEW connection (lazy) and the palette, shared by all tools. Tools run in worker threads: every
    LabVIEW call is serialized by `lock` (the server VI serves one request at a time anyway)."""

    def __init__(self, cfg: argparse.Namespace):
        self.cfg = cfg
        self.lock = threading.RLock()
        self._lv: LabVIEW | None = None
        self._palette = None            # None = not tried yet, False = unavailable, else a Palette
        self._autostart_t = None        # time of the last autostart attempt
        self.palette_note = ""

    @property
    def lv(self) -> LabVIEW:
        if self._lv is None:
            c = self.cfg
            boot = c.boot_dir
            if c.boot_copy_from:
                boot = private_boot_copy(c.boot_copy_from, c.boot_dir, c.boot_prefix)
            Path(c.cache_dir).mkdir(parents=True, exist_ok=True)
            self._lv = LabVIEW(c.host, c.port, c.token, boot, c.gen_dir, c.cache_dir, prefix=c.prefix,
                               timeout=c.timeout, boot_prefix=c.boot_prefix)
            atexit.register(self.release)
            self.reap()
        return self._lv

    # ---------------------------------------------------------------- handle ledger
    # VIs stay in LabVIEW's memory while the server VI holds a reference to them, and a VI name can be in memory
    # only once. So a session that ends without closing its handles (killed, crashed) blocks saving VIs of the
    # same names later. Every session therefore writes its open handles to <cache>/sessions/<pid>.json, and a
    # new session closes the handles of sessions whose process is gone (refnums stay valid in the server VI).
    def _ledger_dir(self) -> Path:
        return Path(self.cfg.cache_dir) / "sessions"

    def save_ledger(self):
        lv = self._lv
        if lv is None:
            return
        refs = {str(vi): sorted(lv._refs.get(vi, ())) for vi in lv._vis}
        data = json.dumps({"pid": os.getpid(), "host": self.cfg.host, "port": self.cfg.port, "refs": refs})
        if data == getattr(self, "_ledger_last", None):
            return
        try:
            d = self._ledger_dir()
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f"{os.getpid()}.tmp"
            tmp.write_text(data)
            os.replace(tmp, d / f"{os.getpid()}.json")
            self._ledger_last = data
        except OSError:
            pass

    def reap(self) -> int:
        """Close the handles of ended sessions (same server). Returns how many sessions were cleaned up."""
        n = 0
        for f in sorted(self._ledger_dir().glob("*.json")) if self._ledger_dir().exists() else []:
            try:
                d = json.loads(f.read_text())
                pid = int(d["pid"])
            except (OSError, ValueError, KeyError):
                continue
            if pid == os.getpid() or _pid_alive(pid) or (d.get("host"), d.get("port")) != (self.cfg.host,
                                                                                            self.cfg.port):
                continue
            for vi, refs in d.get("refs", {}).items():
                for r in list(refs) + [int(vi)]:
                    try:
                        self._lv.S.close(int(r))
                    except Exception:  # noqa: BLE001  (already closed, server restarted)
                        pass
            try:
                f.unlink()
            except OSError:
                pass
            n += 1
        return n

    # ---------------------------------------------------------------- autostart
    def autostart(self) -> bool:
        """Open the server VI in LabVIEW and wait until it listens (lvmcp.autostart). True if it does now. Tried
        at most once per minute (a refused connection right after a failed start must not start LabVIEW again)."""
        import time
        from lvmcp import autostart
        c = self.cfg
        now = time.monotonic()
        if not autostart.enabled(c.autostart, c.host) or now - (self._autostart_t or -1e9) < 60:
            return False
        self._autostart_t = now
        return autostart.start(c.host, c.port, c.server_vi, c.labview)

    def release(self):
        """At exit: close every VI handle this session opened or created (closing references only: unsaved
        VIs are dropped, VIs open in the LabVIEW editor stay open). Otherwise the server VI keeps them in
        memory and a later session cannot save a VI under the same name (error 1357/1051)."""
        lv = self._lv
        if lv is None or getattr(self, "_released", False):
            return
        self._released = True
        for vi in list(getattr(lv, "_vis", ())):
            try:
                lv.close_vi(vi)
            except Exception:  # noqa: BLE001  (LabVIEW gone, handle stale: nothing to release)
                pass
        try:
            lv.close()
            (self._ledger_dir() / f"{os.getpid()}.json").unlink()
        except OSError:
            pass

    def unreachable_msg(self, detail="") -> str:
        c = self.cfg
        tried = (f" lvmcp tried to open {c.server_vi} automatically, but it did not start listening (LabVIEW not "
                 "installed for .vi files, or a dialog in LabVIEW is waiting for an answer)."
                 if self._autostart_t is not None else "")
        return (f"Cannot reach the lvmcp scripting server at {c.host}:{c.port}{' (' + detail + ')' if detail else ''}."
                f"{tried} Start LabVIEW and open lvmcp_server.vi (labview/lvmcp_server.vi; it runs when opened, "
                "default port 5577, token empty). If it is running, LabVIEW may be busy or blocked by a modal "
                "dialog: close the dialog in LabVIEW and retry (the connection is re-opened on the next call).")

    # ---------------------------------------------------------------- palette (optional, lvmcp.palette)
    def palette(self):
        """A loaded lvmcp.palette.Palette, or None (then the New VI Object style table is used)."""
        if self._palette is not None:
            return self._palette or None
        mode = self.cfg.palette
        self._palette = False
        if mode == "off":
            self.palette_note = "palette disabled (--palette off): style table only"
            return None
        try:
            mod = importlib.import_module("lvmcp.palette")
        except Exception as e:          # not there (yet) or broken: style table
            self.palette_note = f"lvmcp.palette not usable ({e.__class__.__name__}: {e}): style table only"
            return None
        try:
            P = mod.Palette(self.lv.S, self.cfg.palette_cache)
            if mode == "auto":
                try:                    # only use a table that is already built (building takes ~1 min)
                    fs = mod.LVFiles(self.lv.S)
                    P.fs = fs
                    exists = P._cache_file(fs).exists()
                except AttributeError:
                    exists = True       # palette API changed: just load it
                if not exists:
                    self.palette_note = ("palette table not built yet (start with --palette build to build it "
                                         "on first use, ~1 min): style table only")
                    return None
            P.load()
            if not hasattr(P, "find"):
                raise AttributeError("Palette has no find()")
        except (OpError, LVError, OSError, AttributeError, KeyError, ValueError) as e:
            if isinstance(e, OSError) and not isinstance(e, FileNotFoundError):
                raise
            self.palette_note = f"palette table not available ({e}): style table only"
            return None
        self._palette = P
        self.palette_note = f"palette table: {len(P.table.get('items', []))} items"
        return P


S: State | None = None


# ------------------------------------------------------------------------------------------------ helpers

def _json(x) -> str:
    return json.dumps(x, separators=(",", ":"), default=str)


def _hint(code) -> str:
    if code in (1026, 1055):
        return (" Hint: the handle is not valid any more (object deleted, VI closed, or the server VI was restarted:"
                " handles do not survive a restart). Get fresh handles with describe_vi.")
    if code == 7:
        return (" Hint: a file was not found. Paths are as LabVIEW sees them (absolute). If it is a wrapper VI, check"
                " the --boot-dir / --gen-dir settings.")
    if code in (1051, 1357):
        return (" Hint: a VI with that file name is already loaded in LabVIEW (VI names must be unique there): save"
                " under another file name, or close the other VI (or the program holding it) first.")
    return ""


def _err(e: Exception) -> ToolError:
    if isinstance(e, OpError):
        msg = str(e)
        if e.code is None and "server not reachable" in msg and ("TimeoutError" in msg or "timed out" in msg):
            return ToolError(f"lvmcp_server.vi did not answer within {S.cfg.timeout:.0f} s. It is busy (another "
                             "session's long operation, e.g. a run_vi or the self-test) or LabVIEW shows a dialog "
                             "that waits for an answer: retry; if it keeps failing, look at LabVIEW.")
        if e.code is None and "server not reachable" in msg:
            return ToolError(S.unreachable_msg(msg.split("(", 1)[-1].split(")")[0]))
        return ToolError(msg + _hint(e.code))
    if isinstance(e, LVError):
        return ToolError(f"LabVIEW error {e.code}: {re.sub(r'<[^>]+>', ' ', str(e.source or ''))}".strip()
                         + _hint(e.code))
    if isinstance(e, (ConnectionError, OSError)):
        return ToolError(S.unreachable_msg(f"{e.__class__.__name__}: {e}"))
    return ToolError(f"{e.__class__.__name__}: {e}")


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000 | 0x1000, False, pid)     # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.GetLastError() == 5                    # access denied: exists
        try:
            return k.WaitForSingleObject(h, 0) == 0x102          # WAIT_TIMEOUT: still running
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _refused(e) -> bool:
    return isinstance(e, ConnectionRefusedError) or (isinstance(e, OpError) and "ConnectionRefused" in str(e))


def lv_tool(fn):
    """Run a tool body under the LabVIEW lock; LabVIEW/conversion errors become MCP tool errors."""
    @functools.wraps(fn)
    def inner(*a, **k):
        with S.lock:
            try:
                try:
                    return fn(*a, **k)
                except (ConnectionRefusedError, OpError) as e:
                    # server VI not running: start it (autostart) and try once more. Nothing was done yet: the
                    # refused connect is the first request of the tool.
                    if not _refused(e) or not S.autostart():
                        raise
                    return fn(*a, **k)
            except ToolError:
                raise
            except (LVError, OSError, ValueError, TypeError, KeyError) as e:
                raise _err(e) from None
            finally:
                # The server VI serves one connection at a time: release it after every tool call so other
                # clients (another session, selftest) get their turn. Also drops any late reply of a failed call.
                if S._lv is not None:
                    S._lv.close()
                    S.save_ledger()
    return inner


def _png_image(png: bytes) -> Image:
    m = S.cfg.max_image
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(png))
        if m and max(im.size) > m:
            im.thumbnail((m, m))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            png = buf.getvalue()
    except ImportError:
        pass
    return Image(data=png, format="png")


def _vi_of(h: int) -> int | None:
    lv = S.lv
    if h in lv._vis:
        return h
    return lv._vi(h)


def _with_image(result, h: int | None, image: bool, what="diagram"):
    text = result if isinstance(result, str) else _json(result)
    if not image:
        return text
    vi = _vi_of(h) if h is not None else None
    if vi is None:
        return [text, "(no image: the VI of this handle is not known; use get_image with the VI handle)"]
    return [text, _png_image(S.lv.get_image(vi, what))]


# ------------------------------------------------------------------------------------------------ tools

INSTRUCTIONS = """\
Build and edit LabVIEW VIs (block diagrams and front panels) by VI Scripting, through lvmcp_server.vi running in
LabVIEW (LabVIEW 2014 or newer). Nothing is done through the GUI.

Workflow:
1. status (checks the connection) -> new_vi (or open_vi) gives a VI handle `vi`.
2. find_object "what you want" -> exact object names (e.g. "Add", "Multiply", "While Loop", "Greater Or Equal?",
   "Numeric Constant", "Wait (ms)") and whether to place them with add_node (built-in) or add_subvi (VI file).
3. add_node / add_subvi / add_structure on an owner: the VI handle (top-level diagram) or a structure's inner
   diagram handle (from the "diagrams" list of add_structure/describe_vi; one per loop, one per case frame).
4. wire node.terminal -> node.terminal; create_constant / create_control / create_indicator on a terminal
   (creates the object already wired). Wires crossing structure borders get tunnels automatically.
   Graphs, charts, buttons and other specific front-panel kinds: add_panel_object, then wire to its "terminal".
   Bundle/Unbundle By Name, Build Waveform, Get Waveform Components: set_elements picks the elements (e.g. dt).
   Concatenate Strings, Build Array, Bundle, Index Array, Unbundle start with one input: resize_node adds more.
   Anything else: class_members + get_property / set_property / invoke_method (generic VI Scripting).
5. run_vi (inputs by control label, returns indicator values) -> look at get_image (diagram/panel) or
   describe_vi (full JSON: nodes, terminals, wires, panel). errors lists why a VI is broken.
6. Front panel (what the user sees): arrange_panel lays it out (inputs left, outputs right, graphs on the
   right, or your own rows/columns) and sizes the window; resize_object (e.g. a bigger graph), add_label
   (title, instructions), add_panel_object (decorations, tab controls; move_object onto a tab page). Look at the
   panel image it returns.
7. save_vi (and save_for_previous "14.0" for LabVIEW 2014), close_vi.

Conventions:
- Handles are integers (LabVIEW refnums) for VIs and objects (nodes, constants, controls, terminals, tunnels,
  structures, diagrams, wires). describe_vi lists objects you created by the handles the creating tool returned;
  other objects (e.g. in a VI opened from a file) get a handle there. Handles die when their VI is closed or the
  server VI restarts.
- Terminals are addressed by (object handle, terminal name) exactly as listed in "terms" ([name, dir, type,
  wired]) of add_node/describe_vi or by the terminals tool, e.g. "x", "y", "x+y", "x >= y?", "error in (no
  error)". Repeated names get "#2", "#3" ("element#2"); an integer is an index into the list; omit the terminal
  for single-terminal objects (constants, controls, indicators). Special names: While Loop "i", "stop"; For Loop
  "N", "i"; Case Structure "selector"; tunnels and shift registers "outside" and "inside" (left shift register
  "inside" = value of the previous iteration; right shift register "inside" = value for the next one, "outside"
  = the final value after the loop); constants/controls "value".
- Coordinates are absolute block-diagram pixels (x right, y down) everywhere, also inside structures: place
  inner nodes inside the structure's rectangle (pos + size). Leave ~60-100 px between nodes. Front-panel
  objects use front-panel pixels; their pos/size include the label.
- Types: DBL, I32, U8, bool, string, path, DBL[] (1D array), U8[2d], enum{a|b}, cluster{x:DBL,..}, error,
  refnum, variant, void (adapts to wiring). Values in JSON: numbers, true/false, strings, paths as strings,
  arrays as lists, clusters as lists or {label: value}, enums by item name.
- Every editing tool takes image=true to also return the block diagram image (off by default: images are big).
- Errors come back as tool errors with the LabVIEW error code and message.
"""

mcp = MCPServer("labview", version=__version__, instructions=INSTRUCTIONS)
tool = functools.partial(mcp.tool, structured_output=False)   # results are text (compact JSON) and images

H = Annotated[int, Field(description="handle (integer)")]
VIH = Annotated[int, Field(description="VI handle from new_vi/open_vi")]
OWNER = Annotated[int, Field(description="where to place it: VI handle (top-level block diagram) or a diagram "
                                          "handle from a structure's 'diagrams' list (or a loop handle = its "
                                          "diagram)")]
X = Annotated[int, Field(description="absolute block-diagram x (pixels, left edge)")]
Y = Annotated[int, Field(description="absolute block-diagram y (pixels, top edge)")]
TERM = Annotated[str | int | None, Field(description="terminal name as listed in 'terms' (e.g. 'x', 'x+y', "
                                                     "'element#2', 'i', 'stop', 'N', 'selector', 'inside', "
                                                     "'outside'), or an index; omit for single-terminal objects")]
IMG = Annotated[bool, Field(description="also return a PNG image of the block diagram")]
FP_OWNER = Annotated[int | None, Field(description="optional: VI or diagram handle to put the block-diagram terminal "
                                                   "on (default: the node's diagram); needs x and y")]
FP_X = Annotated[int | None, Field(description="absolute x of the terminal (with owner)")]
FP_Y = Annotated[int | None, Field(description="absolute y of the terminal (with owner)")]


@tool()
def status() -> str:
    """Check the connection to LabVIEW: server reachable, LabVIEW version, application kind, OS, number of
    creatable objects in the style table, and this MCP server's settings. Call this first."""
    with S.lock:
        c = S.cfg
        out = {"config": {"host": c.host, "port": c.port, "boot_dir": c.boot_dir, "gen_dir": c.gen_dir,
                          "prefix": c.prefix}}
        try:
            try:
                out |= S.lv.status()
            except (LVError, OSError) as e:
                if S._lv is not None:
                    S._lv.close()
                if not (_refused(e) and S.autostart()):
                    out["reachable"] = False
                    out["error"] = str(_err(e))
                    return _json(out)
                out |= S.lv.status()
                out["autostarted"] = True
            try:
                S.palette()
            except Exception as e:      # palette problems must not break status
                S.palette_note = f"palette error: {e}"
            out["find_object_source"] = S.palette_note
            return _json(out)
        except (LVError, OSError) as e:
            out["reachable"] = False
            out["error"] = str(_err(e))
            return _json(out)
        finally:
            if S._lv is not None:
                S._lv.close()           # release the server VI for other clients


@tool()
@lv_tool
def new_vi(image: IMG = False) -> Any:
    """Create a new, untitled VI (automatic error handling off). Returns {"vi": VI handle, "diagram": block
    diagram handle}. Use the vi handle as owner for add_node etc. Save it with save_vi (absolute path)."""
    vi = S.lv.new_vi()
    return _with_image({"vi": vi, "diagram": S.lv.block_diagram(vi)}, vi, image)


@tool()
@lv_tool
def open_vi(path: Annotated[str, Field(description="absolute path of the .vi file as LabVIEW sees it (e.g. "
                                                    "C:\\Users\\me\\x.vi)")],
            image: IMG = False) -> Any:
    """Open a VI file for editing/running. Returns {"vi", "name", "path", "state", "diagram"}. Call describe_vi
    to get its objects, handles and terminals. Opening a VI that is already in memory returns a new handle."""
    vi = S.lv.open_vi(path)
    out = {"vi": vi} | S.lv.vi_info(vi) | {"diagram": S.lv.block_diagram(vi)}
    return _with_image(out, vi, image)


@tool()
@lv_tool
def describe_vi(vi: VIH, values: Annotated[bool, Field(description="include constant/control values")] = True) -> str:
    """Full JSON description of a VI: {"vi", "name", "path", "state" (bad = broken / idle / running), "panel":
    [{h, label, kind (control/indicator), class, type, value, terminal (its diagram terminal handle), pos, size
    (front-panel box with label), pages? (tab controls)}], "decorations"? ([{h, class, text?, pos, size}]),
    "diagram": top-level diagram handle, "nodes": [{h, class, name?, label?, value?, pos [x,y], size [w,h],
    in (handle of the diagram it is on), terms: [[name, dir in/out, type, wired]], diagrams? (structures:
    [{h, name? (case value), default?}]), tunnels?, shift_registers? ([{right, left, right_terms, left_terms}])}],
    "wires": [{h, from: [handle, terminal], to: [[handle, terminal], ...]}], "errors"? (when broken)}.
    Terminal names are exactly what wire/create_* accept. ~8 KB for a 25-node VI."""
    return _json(S.lv.describe_vi(vi, values))


def _style_find(query: str, n: int) -> list[dict]:
    st = S.lv.styles()
    q = query.strip().lower()
    words = re.findall(r"[a-z0-9]+|[^a-z0-9\s]", q)
    scores: dict[str, float] = {}
    for name in st:
        nl = name.lower()
        if nl == q:
            s = 100
        elif nl.startswith(q):
            s = 60 - min(len(nl) - len(q), 20) / 2
        elif words and all(w in nl for w in words):
            s = 50 - min(len(nl) - len(q), 30) / 3
        elif len(q) >= 3 and q in nl:
            s = 40
        else:
            continue
        scores[name] = s
    for alias in STRUCTURES.get(q, []):
        if alias in st:
            scores[alias] = max(scores.get(alias, 0), 95)
    if len(scores) < min(n, 3):                     # fuzzy matches only when the word matches found little
        for name in difflib.get_close_matches(query, list(st), n=n, cutoff=0.5):
            scores.setdefault(name, 30 * difflib.SequenceMatcher(None, q, name.lower()).ratio())
    best = sorted(scores.items(), key=lambda kv: (-kv[1], len(kv[0])))[:n]
    return [{"name": k, "use": "add_node", "score": round(v), "style": st[k]} for k, v in best]


_VI_KINDS = ("vi", "vim", "polyvi", "mergevi")


def _palette_item(score, it) -> dict:
    out = {"name": it.get("name"), "kind": it.get("kind"), "score": round(score)}
    flags = it.get("flags") or []
    if it.get("kind") == "prim":
        out["use"] = "add_node"
    elif it.get("kind") in _VI_KINDS and (it.get("path") or it.get("sym")):
        out["use"] = "add_subvi"
        out["path"] = it.get("path") or it.get("sym")
    else:
        out["use"] = "not supported (configured by dialogs)"
    if "dialog" in flags:
        out["use"] = "not supported (Express VI / XNode, configured by dialogs)"
    for k in ("palette", "shortcuts", "desc"):
        if it.get(k):
            out[k] = it[k] if k != "desc" else str(it[k])[:200]
    if it.get("terminals"):
        out["terminals"] = [[t.get("name"), t.get("dir"), t.get("type")] for t in it["terminals"][:24]]
    return out


@tool()
@lv_tool
def find_object(query: Annotated[str, Field(description="what you look for: a LabVIEW name or words, e.g. "
                                                         "'add', 'multiply', 'while loop', 'greater or equal', "
                                                         "'numeric constant', 'wait ms', 'build array'")],
                n: Annotated[int, Field(description="max results")] = 10) -> str:
    """Look up creatable LabVIEW objects by name. Returns {"source", "results": [{"name", "use", ...}]}, best
    first. "use" says how to create it: "add_node" (pass the exact "name") or "add_subvi" (pass "path").
    Results from the palette table may also list "palette" (category), Quick Drop "shortcuts" and "terminals".
    Names are LabVIEW's own: "Add", "Subtract", "Multiply", "Divide", "Increment", "Greater Or Equal?",
    "Equal?", "Not", "And", "Select", "Build Array", "Index Array", "Array Size", "Concatenate Strings",
    "Number To Fractional String", "Wait (ms)", "Numeric Constant", "String Constant", "True Constant",
    "While Loop", "For Loop", "Case Structure", "Flat Sequence Structure" ..."""
    P = S.palette()
    if P is not None:
        res = [_palette_item(s, it) for s, it in P.find(query, n)]
        return _json({"source": "palette", "results": res})
    return _json({"source": "style table" + (f" ({S.palette_note})" if S.palette_note else ""),
                  "results": _style_find(query, n)})


@tool()
@lv_tool
def add_node(owner: OWNER, name: Annotated[str, Field(description="exact object name from find_object (e.g. "
                                                                   "'Multiply', 'Numeric Constant')")],
             x: X, y: Y, image: IMG = False) -> Any:
    """Place a built-in object (function, constant, structure, ...) by name. Returns {"h": node handle,
    "class", "pos", "size", "terms": [[terminal name, "in"/"out", type, wired]], "diagrams"? (structures)}.
    Then wire its terminals or use create_constant/create_control/create_indicator on them. A palette VI name
    (when the palette table is available) is placed as a subVI. Constants made here have a default value: use
    set_constant, or rather create_constant on the input terminal."""
    lv = S.lv
    try:
        r = lv.add_node(owner, name, x, y)
    except OpError as e:
        if e.code is not None or "no object named" not in str(e):
            raise
        P = S.palette()
        it = P.get(name) if P is not None else None
        if not it or it.get("kind") not in _VI_KINDS or not (it.get("path") or it.get("sym")) \
                or "dialog" in (it.get("flags") or []):
            raise
        r = lv.add_subvi(owner, it.get("path") or it.get("sym"), x, y)
    return _with_image(r, owner, image)


@tool()
@lv_tool
def add_subvi(owner: OWNER, path: Annotated[str, Field(description="absolute path of the .vi as LabVIEW sees "
                                                                    "it (a vi.lib VI, or one you saved)")],
              x: X, y: Y, image: IMG = False) -> Any:
    """Place a VI file as a subVI node. Returns like add_node (+ "name"). A VI built with this server needs
    set_connector before it shows terminals as a subVI."""
    return _with_image(S.lv.add_subvi(owner, path, x, y), owner, image)


@tool()
@lv_tool
def add_structure(owner: OWNER,
                  kind: Annotated[str, Field(description="'while', 'for', 'case', 'flat sequence' (or a structure "
                                                         "name such as 'Stacked Sequence Structure')")],
                  x: X, y: Y,
                  width: Annotated[int, Field(description="width in pixels")] = 300,
                  height: Annotated[int, Field(description="height in pixels")] = 200,
                  image: IMG = False) -> Any:
    """Add a loop / case / sequence structure. Returns add_node's dict with "diagrams": [{"h", "name"?}]: use a
    diagram h as owner to put nodes inside (absolute coordinates inside the rectangle pos..pos+size). Loop
    terminals: While Loop "i" (iteration, out) and "stop" (bool in: wire a condition, the loop stops when it is
    true); For Loop "N" (count, in) and "i". Case Structure: "selector" (in; frames "False"/"True" for a bool,
    see add_frame/set_case_names for numbers or strings). Shift registers: add_shift_register."""
    return _with_image(S.lv.add_structure(owner, kind, x, y, width, height), owner, image)


@tool()
@lv_tool
def add_frame(structure: Annotated[int, Field(description="case structure or flat sequence handle")],
              after: Annotated[int | None, Field(description="frame index to add after (default: last)")] = None,
              image: IMG = False) -> Any:
    """Add a frame (case) to a Case Structure or Flat Sequence. Returns {"diagrams": [{"h", "name"?}]}."""
    return _with_image(S.lv.add_frame(structure, after), structure, image)


@tool()
@lv_tool
def set_case_names(structure: Annotated[int, Field(description="case structure handle")],
                   names: Annotated[list[str], Field(description="selector value per frame, e.g. ['0', '1', "
                                                                 "'2..5', 'Default'] or ['\"abc\"', 'Default']")],
                   image: IMG = False) -> Any:
    """Set the selector values of a case structure's frames (one per frame: add frames first). Wire the
    selector first so its type (number, string, enum) is known. Returns {"diagrams"}."""
    return _with_image(S.lv.set_case_names(structure, names), structure, image)


@tool()
@lv_tool
def add_shift_register(loop: Annotated[int, Field(description="while/for loop handle")],
                       y: Annotated[int | None, Field(description="y position (default: LabVIEW's)")] = None,
                       image: IMG = False) -> Any:
    """Add a shift register pair to a loop. Returns {"right": {h, terms}, "left": {h, terms}}. Wire the initial
    value to left.outside (e.g. create_constant(left.h, "outside", 0)), read the previous value from
    left.inside, wire the next value to right.inside, read the final value at right.outside."""
    return _with_image(S.lv.add_shift_register(loop, y), loop, image)


@tool()
@lv_tool
def set_tunnel_indexing(tunnel: Annotated[int, Field(description="loop tunnel handle (from describe_vi)")],
                        indexing: bool) -> str:
    """Turn auto-indexing of a loop tunnel on (arrays in/out, one element per iteration) or off."""
    S.lv.set_tunnel_indexing(tunnel, indexing)
    return _json({"ok": True})


@tool()
@lv_tool
def terminals(handle: Annotated[int, Field(description="any object handle: node, structure, tunnel, shift "
                                                        "register, constant, control")]) -> str:
    """List an object's terminals: [{"name", "h", "dir": in/out, "type", "wired"}] (names as wire accepts)."""
    return _json(S.lv.terminals(handle))


@tool()
@lv_tool
def wire(src: Annotated[int, Field(description="source object handle")],
         dst: Annotated[int, Field(description="destination object handle")],
         src_terminal: TERM = None, dst_terminal: TERM = None,
         image: IMG = False) -> Any:
    """Wire an output terminal to an input terminal, e.g. wire(add, "x+y", mul, "x"), wire(loop, "i", ge,
    "x"), wire(ge, "x >= y?", loop, "stop"). Objects can be on different diagrams (tunnels are created). Given
    in the wrong direction, it is swapped. Returns {"wire": handle}. An output can feed several inputs: LabVIEW
    makes that one branched wire, so wiring an already wired output returns the same wire handle."""
    return _with_image(S.lv.wire(src, src_terminal, dst, dst_terminal), dst, image)


@tool()
@lv_tool
def create_constant(node: Annotated[int, Field(description="object whose input terminal gets the constant")],
                    terminal: TERM = None,
                    value: Annotated[Any, Field(description="value (converted to the terminal type), e.g. 3, "
                                                            "2.5, true, \"text\", [1, 2, 3]")] = None,
                    type: Annotated[str | None, Field(description="explicit type when the terminal adapts to "
                                                                  "its input, e.g. 'I32', 'DBL', 'U8', 'string', "
                                                                  "'bool', 'path', 'DBL[]', 'I32[,]', "
                                                                  "'cluster{x:DBL, y:DBL}'")] = None,
                    label: Annotated[str | None, Field(description="label to show")] = None,
                    image: IMG = False) -> Any:
    """Create a diagram constant wired to an input terminal. Returns {"h", "class", "value"}. Examples:
    create_constant(mul, "x", 3); create_constant(loop, "N", 10); create_constant(ge, "y", 9, type="I32");
    create_constant(sr_left, "outside", 0, type="I32") (shift register initial value)."""
    return _with_image(S.lv.create_constant(node, terminal, value, type, label), node, image)


@tool()
@lv_tool
def set_constant(constant: Annotated[int, Field(description="constant handle")], value: Any) -> str:
    """Change a diagram constant's value (converted to its type)."""
    S.lv.set_constant(constant, value)
    return _json({"ok": True})


@tool()
@lv_tool
def create_control(node: Annotated[int, Field(description="object whose INPUT terminal gets the control")],
                   terminal: TERM = None,
                   label: Annotated[str | None, Field(description="control label (used by run_vi inputs, "
                                                                  "set_value)")] = None,
                   owner: FP_OWNER = None, x: FP_X = None, y: FP_Y = None,
                   type: Annotated[str | None, Field(description="control type if not the terminal's own, e.g. "
                                                                 "'DBL[]' for a Multiply input that should take "
                                                                 "an array, 'I32', 'string', 'bool', 'I32[,]' (2D), "
                                                                 "'cluster{x:DBL, y:DBL}', "
                                                                 "'cluster{Name:string, Score:DBL}[]'")] = None,
                   image: IMG = False) -> Any:
    """Create a front-panel control wired to an input terminal. Returns {"h" (panel control), "terminal"
    (its block-diagram terminal handle, can be wired further), "label", "type"}. The block-diagram terminal is
    put next to the node, on the node's own diagram; for a node inside a loop/case, pass owner (e.g. the VI
    handle) and x, y to put the terminal outside instead: the wire then gets a tunnel."""
    return _with_image(S.lv.create_control(node, terminal, label, owner, x, y, type), node, image)


@tool()
@lv_tool
def create_indicator(node: Annotated[int, Field(description="object whose OUTPUT terminal gets the indicator")],
                     terminal: TERM = None,
                     label: Annotated[str | None, Field(description="indicator label (key in run_vi results)")] = None,
                     owner: FP_OWNER = None, x: FP_X = None, y: FP_Y = None,
                     image: IMG = False) -> Any:
    """Create a front-panel indicator wired to an output terminal. Returns {"h", "terminal", "label", "type"}.
    Example: create_indicator(mul, "x*y", label="product"); create_indicator(sr_right, "outside", "count").
    The terminal is put on the node's own diagram: an indicator created for a node inside a case frame or loop
    is inside too (in a loop it then shows every iteration; in a case it is only written by that frame). Pass
    owner (e.g. the VI handle) and x, y to put it outside: the wire gets an output tunnel (a case structure's
    output tunnel must then be wired in every frame, or the VI is broken)."""
    return _with_image(S.lv.create_indicator(node, terminal, label, owner, x, y), node, image)


@tool()
@lv_tool
def add_panel_object(vi: Annotated[int, Field(description="VI handle")],
                     name: Annotated[str, Field(description="front-panel object style name, e.g. 'Waveform Graph "
                                                            "(modern)', 'Waveform Chart (modern)', 'Stop Button', "
                                                            "'Numeric Control (modern)' (find_object lists them)")],
                     x: Annotated[int, Field(description="front-panel x")],
                     y: Annotated[int, Field(description="front-panel y")],
                     label: Annotated[str | None, Field(description="label text")] = None,
                     indicator: Annotated[bool | None, Field(description="force indicator (True) or control "
                                                                         "(False); graphs/charts are indicators")] = None,
                     width: int = 0, height: int = 0, image: IMG = False) -> Any:
    """Put a control/indicator of a specific kind on the front panel (graphs, charts, buttons, gauges...: things
    create_control/create_indicator cannot make, since those give the terminal's plain type). Returns {"h",
    "label", "kind", "terminal", "pos", "size"}: wire to it with wire(src, terminal_h, src_terminal, "value") where
    terminal_h is the returned "terminal" (its block-diagram terminal; move it with move_object, e.g. into a
    loop). Also decorations ('Flat Box', 'Raised Box (modern)', 'Line (thin)'...: returns {"h", "class", "pos",
    "size"}) and 'Tab Control (modern)' (returns "pages": [{h, label, pos, size}]: put objects on a page with
    move_object(h, x, y, owner=page h), x, y inside the page's pos..pos+size; arrange_panel(page=...) tidies it)."""
    r = S.lv.add_panel_object(vi, name, x, y, label, indicator, width, height)
    return _with_image(r, vi, image, "panel")


@tool()
@lv_tool
def set_elements(node: Annotated[int, Field(description="Bundle By Name / Unbundle By Name / Build Waveform / "
                                                        "Get Waveform Components node")],
                 names: Annotated[list[str], Field(description="element names to show, in order, e.g. "
                                                               "['Y', 'dt'] (see the error message for the "
                                                               "available ones)")],
                 image: IMG = False) -> Any:
    """Choose which cluster/waveform elements a by-name (un)bundle node shows; its terminals become those
    elements (e.g. Get Waveform Components shows only Y by default: set_elements(h, ["Y", "dt"])). Returns the
    node's terminals."""
    return _with_image(S.lv.set_elements(node, names), node, image)


@tool()
@lv_tool
def resize_node(node: Annotated[int, Field(description="Build Array / Concatenate Strings / Bundle / Index Array / "
                                                       "Unbundle node")],
                count: Annotated[int, Field(description="number of inputs (Index Array: index pairs, Unbundle: "
                                                        "outputs)")],
                image: IMG = False) -> Any:
    """Set how many inputs a growable node has (these nodes start with one: Concatenate Strings, Build Array,
    Bundle; also Index Array's indices and Unbundle's outputs). New terminals are named like the first one with
    "#2", "#3"... (e.g. "string", "string#2"). Returns the node's terminals."""
    return _with_image([[t["name"], t["dir"], t["type"], t["wired"]] for t in S.lv.resize_node(node, count)],
                       node, image)


@tool()
@lv_tool
def class_members(class_name: Annotated[str, Field(description="VI Server class, e.g. 'WhileLoop', 'Control', "
                                                               "'VI', 'NamedBundler' (describe_vi shows classes)")]) -> str:
    """List the scripting properties and methods of a LabVIEW class as [short name, long name] pairs, for use
    with get_property / set_property / invoke_method when no dedicated tool exists."""
    return _json(S.lv.class_members(class_name))


@tool()
@lv_tool
def get_property(handle: int, prop: Annotated[str, Field(description="property name (short or long, see "
                                                                     "class_members)")],
                 class_name: Annotated[str | None, Field(description="class to use (default: the object's)")] = None
                 ) -> str:
    """Read any scripting property of an object or VI handle (escape hatch for things no other tool covers)."""
    return _json({"value": S.lv.get_property(handle, prop, class_name)})


@tool()
@lv_tool
def set_property(handle: int, prop: str, value: Annotated[Any, Field(description="new value (JSON)")],
                 class_name: str | None = None, image: IMG = False) -> Any:
    """Write any scripting property of an object or VI handle (e.g. a control's 'Visible', a loop's
    'Auto Grow?'). The value is converted to the property's type."""
    S.lv.set_property(handle, prop, value, class_name)
    return _with_image({"ok": True}, handle, image)


@tool()
@lv_tool
def invoke_method(handle: int, method: Annotated[str, Field(description="method name (see class_members)")],
                  inputs: Annotated[dict | None, Field(description="parameters by name")] = None,
                  class_name: str | None = None, image: IMG = False) -> Any:
    """Call any scripting method of an object or VI handle. Returns its outputs. Methods that open dialogs or
    affect files outside your work must not be used."""
    return _with_image(S.lv.invoke_method(handle, method, inputs, class_name), handle, image)


@tool()
@lv_tool
def set_label(handle: Annotated[int, Field(description="control, indicator, constant or node handle")],
              text: str) -> str:
    """Rename the label of a control/indicator/constant/node."""
    S.lv.set_label(handle, text)
    return _json({"ok": True})


@tool()
@lv_tool
def set_value(vi: VIH, label: Annotated[str, Field(description="front-panel control label")], value: Any,
              default: Annotated[bool, Field(description="also make it the control's default value (kept when the "
                                                          "VI is saved; LabVIEW makes all current values default "
                                                          "at once)")] = False) -> str:
    """Set a front-panel control's value (converted to its type; enums by name, clusters as list or dict).
    default=true also makes it the default value saved with the VI."""
    S.lv.set_value(vi, label, value, default)
    return _json({"ok": True})


@tool()
@lv_tool
def get_value(vi: VIH, label: Annotated[str, Field(description="front-panel control/indicator label")]) -> str:
    """Read a front-panel control's or indicator's current value."""
    return _json({label: S.lv.get_value(vi, label)})


@tool()
@lv_tool
def run_vi(vi: VIH,
           inputs: Annotated[dict[str, Any] | None, Field(description="{control label: value} set before the "
                                                                      "run")] = None,
           outputs: Annotated[list[str] | None, Field(description="labels to read after the run (default: all "
                                                                  "indicators)")] = None,
           timeout: Annotated[float, Field(description="seconds; a VI still running then is aborted")] = 10.0,
           image: Annotated[bool, Field(description="also return the front panel image after the run")] = False
           ) -> Any:
    """Run the VI as a top-level VI and wait until it stops. Returns {indicator label: value}. A broken VI
    gives an error with the error list (see errors); a VI still running after `timeout` is aborted (error).
    Loops need a stop condition (While Loop "stop" wired) or a count (For Loop "N")."""
    return _with_image(S.lv.run_vi(vi, inputs, outputs, timeout), vi, image, "panel")


@tool()
@lv_tool
def errors(vi: VIH) -> str:
    """Is the VI broken, and why: {"broken", "state", "errors": [{"message", "details"}]} (e.g. unwired
    required inputs, type conflicts, loops without stop condition)."""
    return _json(S.lv.errors(vi))


@tool()
@lv_tool
def cleanup_diagram(vi: VIH, image: IMG = False) -> Any:
    """Clean Up Diagram: let LabVIEW rearrange the whole block diagram (positions change; handles stay)."""
    S.lv.cleanup(vi)
    return _with_image({"ok": True}, vi, image)


@tool()
@lv_tool
def get_image(handle: Annotated[int, Field(description="VI handle, or a node/structure handle for an image of "
                                                        "just that object")],
              what: Annotated[str, Field(description="'diagram' or 'panel' (VI handles only)")] = "diagram") -> list:
    """PNG image of a VI's block diagram or front panel (or of one node/structure). Large images are scaled
    down."""
    if what not in ("diagram", "panel"):
        raise ToolError("what must be 'diagram' or 'panel'")
    png = S.lv.get_image(handle, what)
    img = _png_image(png)
    return [img, f"{what} image of {handle}"]


@tool()
@lv_tool
def save_vi(vi: VIH, path: Annotated[str | None, Field(description="absolute .vi path as LabVIEW sees it; "
                                                                   "omit to save to the VI's current file")] = None
            ) -> str:
    """Save the VI in the running LabVIEW's version. The VI takes the file's name (VI names must be unique in
    LabVIEW: error 1051 if another VI of that name is in memory). Returns {"path", "warning"? (e.g. overlapping
    front-panel objects)}."""
    try:
        out = {"path": S.lv.save_vi(vi, path)}
    except OpError as e:
        if e.code not in (1051, 1357) or not S.reap():   # name held by an ended session: free it and retry
            raise
        out = {"path": S.lv.save_vi(vi, path)}
    try:
        ov = S.lv.check_panel(vi)
        if ov:
            out["warning"] = (f"front-panel objects overlap: {ov[:6]}; arrange_panel lays the panel out (then "
                              "save again)")
    except (OpError, LVError):
        pass                                             # the check is a hint only
    return _json(out)


@tool()
@lv_tool
def save_for_previous(vi: VIH, path: Annotated[str, Field(description="absolute target .vi path (as LabVIEW "
                                                                       "sees it)")],
                      version: Annotated[str, Field(description="target LabVIEW version, e.g. '14.0' = 2014, "
                                                                "'20.0' = 2020")] = "14.0") -> str:
    """Save a copy of the VI for an older LabVIEW version (the VI in memory keeps its path). Not possible while
    the VI runs. Returns {"path", "warnings"}."""
    return _json(S.lv.save_for_previous(vi, path, version))


@tool()
@lv_tool
def set_connector(vi: VIH,
                  inputs: Annotated[list[str] | None, Field(description="control labels for the left connector "
                                                                        "terminals, top to bottom")] = None,
                  outputs: Annotated[list[str] | None, Field(description="indicator labels for the right "
                                                                         "terminals, top to bottom")] = None) -> str:
    """Assign front-panel controls/indicators to the VI's connector pane, so it can be used as a subVI
    (add_subvi). New VIs have 4 left + 4 right terminals. Returns {"pattern", "assigned"}."""
    return _json(S.lv.set_connector(vi, list(inputs or []), list(outputs or [])))


@tool()
@lv_tool
def close_vi(vi: VIH) -> str:
    """Close the VI and every handle obtained for it. Unsaved changes are discarded (no dialog)."""
    S.lv.close_vi(vi)
    return _json({"ok": True})


@tool()
@lv_tool
def delete_object(handle: Annotated[int, Field(description="node, constant, structure, control, or wire handle")],
                  image: IMG = False) -> Any:
    """Delete an object from the block diagram / front panel (deleting a control's terminal deletes the control).
    Wires attached to a deleted node become broken wires: delete them too (describe_vi lists wires)."""
    vi = _vi_of(handle)
    S.lv.delete_object(handle)
    return _with_image({"ok": True}, vi, image)


@tool()
@lv_tool
def move_object(handle: H, x: X, y: Y,
                owner: Annotated[int | None, Field(description="optional diagram/structure handle to move the "
                                                               "object into")] = None,
                image: IMG = False) -> Any:
    """Move an object to absolute (x, y); with owner, move it into another diagram (e.g. into a loop). Front-panel
    objects: x, y are panel coordinates; owner = a tab control page handle (from "pages" of add_panel_object or
    describe_vi) puts the object on that page, owner = the VI handle takes it back onto the panel."""
    S.lv.move_object(handle, x, y, owner)
    return _with_image({"ok": True}, handle, image)


ROWS = Annotated[list[list[str | int]] | None,
                 Field(description="rows top to bottom, each a list of control/indicator labels (or free-label "
                                   "texts, or handles) left to right, e.g. [['Title'], ['a', 'b'], ['graph']]")]
COLS = Annotated[list[list[str | int]] | None,
                 Field(description="columns left to right, each a list of labels top to bottom, e.g. "
                                   "[['frequency', 'amplitude', 'stop'], ['signal graph']]")]


@tool()
@lv_tool
def arrange_panel(vi: VIH, rows: ROWS = None, columns: COLS = None,
                  fit_window: Annotated[bool, Field(description="size the panel window to the objects (saved with "
                                                                "the VI)")] = True,
                  page: Annotated[int | None, Field(description="optional tab control page handle: lay out the "
                                                                "objects on that page instead")] = None,
                  image: Annotated[bool, Field(description="also return the front panel image")] = True) -> Any:
    """Lay out the front panel neatly (call it once the panel objects exist, before save_vi). Without rows/columns:
    free labels on top as titles, controls in a left column, indicators in the next column, graphs/charts/tables
    in a column on the right, each in block-diagram order (Stop buttons last). With rows or columns you choose the
    grouping; objects you leave out are placed the default way after them; decorations move only when listed.
    Objects on tab control pages: call it again with page=<page handle>.
    Returns {"objects": [{label, pos, size}], "window": [w, h], "overlaps"} (+ the panel image)."""
    return _with_image(S.lv.arrange_panel(vi, rows, columns, fit_window, page), vi, image, "panel")


@tool()
@lv_tool
def resize_object(handle: Annotated[int, Field(description="front-panel object handle (control, indicator, graph, "
                                                           "decoration, free label)")],
                  width: Annotated[int | None, Field(description="total width in pixels (label included)")] = None,
                  height: Annotated[int | None, Field(description="total height in pixels (label included)")] = None,
                  image: Annotated[bool, Field(description="also return the front panel image")] = False) -> Any:
    """Resize a front-panel object, e.g. a graph to 500 x 300, a string control to 300 wide, a box decoration.
    Graphs grow their plot area; numerics take only a width; arrays a width and the number of visible elements.
    Returns {"pos", "size"} after resizing (LabVIEW may round)."""
    return _with_image(S.lv.resize_object(handle, width, height), handle, image, "panel")


@tool()
@lv_tool
def add_label(vi: VIH, text: str,
              x: Annotated[int, Field(description="front-panel x")],
              y: Annotated[int, Field(description="front-panel y")],
              size: Annotated[int | None, Field(description="font size in LabVIEW units (normal text is 13-15; e.g. 24 for a title)")] = None,
              bold: bool = False,
              owner: Annotated[int | None, Field(description="optional tab control page handle (from 'pages')")] = None,
              image: Annotated[bool, Field(description="also return the front panel image")] = False) -> Any:
    """Put a free label (a title, instructions, units...) on the front panel. Returns {"h", "pos", "size"}.
    Change its text with set_property(h, "Text", ...); delete it with delete_object."""
    return _with_image(S.lv.add_label(vi, text, x, y, size, bold, owner), vi, image, "panel")


# ------------------------------------------------------------------------------------------------ main

def main(argv=None):
    global S
    cfg = parse_args(argv)
    logging.basicConfig(level=cfg.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    S = State(cfg)
    # clients end a stdio server with SIGTERM (or SIGINT/SIGHUP): exit normally so atexit closes our VI handles.
    # (Must be set here: tools run in worker threads, where signal handlers cannot be installed.)
    for sig in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), lambda *_: sys.exit(0))
            except (ValueError, OSError):
                pass
    try:
        mcp.run(transport="stdio")
    finally:
        S.release()                     # stdin closed: release before the client kills the process


if __name__ == "__main__":
    main()
