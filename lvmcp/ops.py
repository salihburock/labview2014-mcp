"""High-level LabVIEW editing operations (what the MCP tools call).

Everything goes through the lvmcp scripting server (lvclient.py) and wrapper VIs (scripting.py). All public
methods take and return plain JSON-able data: object handles are ints (LabVIEW refnums), values are Python
numbers/bools/strings/lists. Failures raise `OpError` (a `LVError` subclass) with the LabVIEW error code and text.

    lv = LabVIEW(port=5577, boot_dir=".../boot", gen_dir=".../gen", cache_dir=".../gen", prefix="lvmcp_")
    vi = lv.new_vi()
    add = lv.add_node(vi, "Add", 200, 100)["h"]
    lv.create_constant(add, "x", 2)
    lv.create_constant(add, "y", 3)
    lv.create_indicator(add, "x+y", label="sum")
    print(lv.run_vi(vi)["sum"])          # 5.0

Handles: a VI handle (from new_vi/open_vi) or a GObject handle (node, constant, control, terminal, tunnel,
structure, diagram, wire). The same LabVIEW object can have several handles (e.g. the one add_node returned and
the one describe_vi lists); all of them work. Terminals are addressed by (object handle, terminal name), see
`terminals()`; names that occur twice get a "#2" suffix; an int is an index into that list.

Written for LabVIEW 2014+ (tested on 2022). Things that may not exist in 2014 have fallbacks or are noted in
docs/ops.md.
"""
from __future__ import annotations

import difflib
import io
import re
import struct
import time
from pathlib import Path

from .lvclient import (BOOL, DBL, I32, NUMERIC_CODES, PATH, STRING, WAVEFORM_Y, Client, LVError, Variant,
                       make_variant, parse_td)
from .scripting import (STYLE_INVOKE_NODE, STYLE_NEW_VI_OBJECT, STYLE_PROPERTY_NODE as STYLE_PROPERTY_NODE_,
                        Scripting, Wrapper, lv_join)

ROOT = Path(__file__).resolve().parent.parent

ERR_TEXT = {
    1: "an input parameter is invalid",
    7: "file not found",
    8: "file permission error",
    91: "variant data type does not match the requested type",
    1000: "the VI is not in a state compatible with this operation (e.g. it is running or broken)",
    1003: "the VI is not executable (broken); see errors()",
    1004: "the VI is not in memory",
    1026: "VI reference is invalid",
    1043: "this operation is not supported in this LabVIEW edition/version",
    1051: "a VI with this name is already in memory (VI names must be unique)",
    1054: "the specified object was not found",
    1055: "object reference is invalid (Not a Refnum / closed reference / deleted object)",
    1057: "the object cannot be type cast to the required class",
    1058: "the object is not of the class this operation needs",
    1059: "the object cannot be created / placed here",
    1074: "this terminal does not accept that operation",
    1077: "invalid property/method name for this class",
    1113: "unknown VI Server class name",
    1311: "invalid class name",
    1507: "Save for Previous is not possible while the VI is running",
}

_STD = ("reference", "reference out", "error in (no error)", "error out")

STATE = {0: "bad", 1: "idle", 2: "running", 3: "running as subVI"}

STRUCTURES = {
    "while": ["While Loop", "While Loop #1", "While Loop #2"],
    "for": ["For Loop"],
    "case": ["Case Structure"],
    "flat sequence": ["Flat Sequence Structure"],
    "sequence": ["Flat Sequence Structure"],
    "stacked sequence": ["Stacked Sequence Structure"],
    "event": ["Event Structure"],
    "disable": ["Diagram Disable Structure"],
}

TYPE_CODES = {"I8": 0x01, "I16": 0x02, "I32": 0x03, "I64": 0x04, "U8": 0x05, "U16": 0x06, "U32": 0x07,
              "U64": 0x08, "SGL": 0x09, "DBL": 0x0A, "EXT": 0x0B, "CSG": 0x0C, "CDB": 0x0D, "CXT": 0x0E,
              "BOOL": BOOL, "BOOLEAN": BOOL, "STRING": STRING, "PATH": PATH}


class OpError(LVError):
    """A failed operation: .code (LabVIEW error code or None), .source (LabVIEW error source), .op."""

    def __init__(self, code, source, op=""):
        self.op = op
        self.text = ERR_TEXT.get(code, "") if isinstance(code, int) else ""
        LVError.__init__(self, code, source)
        src = _clean(source)
        msg = f"{op}: " if op else ""
        if code is None:
            msg += src
        else:
            msg += f"LabVIEW error {code}" + (f" ({self.text})" if self.text else "") + (f" in {src}" if src else "")
        self.args = (msg,)


def _clean(s) -> str:
    s = str(s or "")
    for a, b in (("<APPEND>", " |"), ("<b>", ""), ("</b>", ""), ("\r", " "), ("\n", " ")):
        s = s.replace(a, b)
    return " ".join(s.split())


# ------------------------------------------------------------------------------------------ value conversion

def type_str(td) -> str:
    """Compact type name of a lvclient.TD: DBL, I32, bool, string, path, DBL[], enum{a|b}, cluster{x:DBL,..}..."""
    c = td.code
    if c == 0xF1:
        return type_str(td.items[0])
    if c in (0x15, 0x16, 0x17):
        names = td.names if len(td.names) <= 12 else td.names[:12] + ["..."]
        return "enum{" + "|".join(names) + "}"
    if c == 0x40:
        return type_str(td.items[0]) + ("[]" if td.dims == 1 else f"[{td.dims}d]")
    if c == 0x50:
        labels = [type_str_label(e) for e in td.items]
        if [x.split(":")[0] for x in labels] == ["status", "code", "source"]:
            return "error"
        return "cluster{" + ",".join(labels) + "}"
    if c == 0x70:
        return "refnum"
    if c == 0x53:
        return "variant"
    return td.describe()


def type_str_label(td) -> str:
    t = type_str(td)
    lab = td.label if td.code != 0xF1 else (td.items[0].label or td.label)
    return f"{lab}:{t}" if lab else t


def VERSION_OF_LV() -> int:
    from lvmcp import lvclient
    return lvclient.VERSION


def empty_value(td):
    """Default value of a type (for constants/controls made from a type only)."""
    c = td.code
    if c == 0x40:
        return [] if td.dims == 1 else [[]] if td.dims == 2 else []
    if c == 0x50:
        return [empty_value(x) for x in td.items]
    if c in (0x30, 0x32):
        return ""
    if c == 0x21:
        return False
    return 0


def parse_type_spec(spec: str):
    """'cluster{Name:string, Score:DBL}[]' -> (type descriptor table, index of the top type)."""
    from lvmcp.lvclient import parse_td
    toks = re.findall(r"[{}:,\[\]]|[^{}:,\[\]]+", spec.strip())
    toks = [t.strip() for t in toks if t.strip()]
    types: list = []
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take(x=None):
        t = peek()
        if t is None or (x is not None and t != x):
            raise ValueError(f"bad type {spec!r} (expected {x or 'more'} at {t!r})")
        pos[0] += 1
        return t

    def add(body: bytes, label: str):
        flag = 0x40 if label else 0x00
        body = bytes([flag]) + body
        if label:
            ps = bytes([len(label)]) + label.encode("latin-1")
            body += ps + (b"\x00" if len(ps) % 2 else b"")
        raw = struct.pack(">H", len(body) + 2) + body
        types.append(parse_td(raw, 0, types)[0])
        return len(types) - 1

    def one(label=""):
        t = take()
        if t in ("{",) or t.lower() == "cluster":
            if t != "{":
                take("{")
            idx = []
            while True:
                idx.append(field())
                if peek() == ",":
                    take(",")
                    continue
                take("}")
                break
            k = None
            body = bytes([0x50]) + struct.pack(">H", len(idx)) + b"".join(struct.pack(">H", i) for i in idx)
            k = ("cluster", body)
        else:
            name = t.split()[0].upper()
            code = TYPE_CODES.get(name)
            if code is None:
                raise ValueError(f"unknown type {t!r}")
            body = bytes([code]) + (b"\x00" if code in NUMERIC_CODES else b"\xff\xff\xff\xff"
                                    if code in (0x30, 0x32) else b"")
            k = ("scalar", body)
        dims = []
        while peek() == "[":
            take("[")
            d = 1
            while peek() == ",":
                take(",")
                d += 1
            take("]")
            dims.append(d)
        if not dims:
            return add(k[1], label)
        el = add(k[1], "")
        for i, d in enumerate(dims):
            body = bytes([0x40]) + struct.pack(">H", d) + b"\xff\xff\xff\xff" * d + struct.pack(">H", el)
            el = add(body, label if i == len(dims) - 1 else "")
        return el

    def field():
        # "label: type" or "type label" or just "type"
        if pos[0] + 1 < len(toks) and toks[pos[0] + 1] == ":":
            label = take()
            take(":")
            return one(label)
        t = peek()
        parts = t.split() if t else []
        if len(parts) >= 2 and parts[0].upper() in TYPE_CODES:
            toks[pos[0]] = parts[0]
            return one(" ".join(parts[1:]))
        return one("")

    top = one("")
    if peek() is not None:
        raise ValueError(f"bad type {spec!r} (unexpected {peek()!r})")
    return types, top


_REPR_ALIASES = {"EXT": "Extended Precision", "DBL": "Double Precision", "SGL": "Single Precision",
                 "I64": "Quad", "I32": "Long", "I16": "Word", "I8": "Byte", "U64": "Unsigned Quad",
                 "U32": "Unsigned Long", "U16": "Unsigned Word", "U8": "Unsigned Byte", "CXT": "Complex Extended",
                 "CDB": "Complex Double", "CSG": "Complex Single", "FXP": "Fixed Point"}


def to_lv(td, v):
    """Python value -> data acceptable to lvclient.write_data for type td (enums by name, clusters from dicts)."""
    c = td.code
    if isinstance(v, Variant):
        return v.value
    if c == 0xF1:
        return to_lv(td.items[0], v)
    if c in (0x15, 0x16, 0x17):
        if isinstance(v, str):
            names = [n.strip() for n in td.names]
            if v.strip() in names:
                return names.index(v.strip())
            low = [n.lower() for n in names]
            if v.strip().lower() in low:
                return low.index(v.strip().lower())
            alias = _REPR_ALIASES.get(v.strip().upper())     # Representation enums: "I32" -> "Long", ...
            if alias and alias.lower() in low:
                return low.index(alias.lower())
            raise ValueError(f"{v!r} is not an item of enum {td.names}")
        return int(v)
    if c in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08):
        if isinstance(v, str):
            v = float(v)
        return int(round(v)) if isinstance(v, float) else int(v)
    if c == 0x54 and getattr(td, "subtype", 0) in WAVEFORM_Y:   # waveform: dict {t0, dt, Y} or just Y
        if isinstance(v, dict):
            return {"t0": float(v.get("t0", 0)), "dt": float(v.get("dt", 1)), "Y": [float(x) for x in v.get("Y", [])]}
        return {"t0": 0.0, "dt": 1.0, "Y": [float(x) for x in v]}
    if c in (0x09, 0x0A, 0x0B, 0x54):
        return float(v)
    if c in (0x0C, 0x0D, 0x0E):
        return complex(*v) if isinstance(v, (list, tuple)) else complex(v)
    if c == 0x21:
        if isinstance(v, str):
            return v.strip().lower() in ("true", "t", "1", "yes", "on")
        return bool(v)
    if c == 0x30:
        return v if isinstance(v, (bytes, bytearray)) else str(v)
    if c == 0x32:
        return v if isinstance(v, dict) else str(v)
    if c == 0x40:
        el = td.items[0]

        def conv(x, d):
            return [conv(y, d - 1) for y in x] if d > 1 else [to_lv(el, y) for y in x]
        return conv(v, td.dims)
    if c == 0x50:
        if isinstance(v, dict):
            out = []
            for el in td.items:
                lab = el.label if el.code != 0xF1 else (el.items[0].label or el.label)
                if lab not in v:
                    raise ValueError(f"cluster element {lab!r} missing (elements: {[type_str_label(e) for e in td.items]})")
                out.append(to_lv(el, v[lab]))
            return out
        if len(v) != len(td.items):
            raise ValueError(f"cluster needs {len(td.items)} elements: {[type_str_label(e) for e in td.items]}")
        return [to_lv(el, x) for el, x in zip(td.items, v)]
    if c == 0x70:
        return int(v)
    return v


def path_str(p) -> str | None:
    if not isinstance(p, dict):
        return p
    ptype, parts = p.get("type"), p.get("parts", [])
    if ptype == 1:                                   # relative
        return "/".join(parts)
    if ptype not in (0, 3):
        return None                                  # <Not A Path>
    if not parts:
        return ""
    if parts[0].startswith("\\\\"):
        return "\\".join(parts)
    if len(parts[0]) == 1 and parts[0].isalpha():    # Windows drive
        return parts[0] + ":\\" + "\\".join(parts[1:])
    return "/" + "/".join(parts)


def to_py(td, v):
    """lvclient.read_data result -> nicer Python (enum names, path strings, complex as [re, im])."""
    c = td.code
    if c == 0xF1:
        return to_py(td.items[0], v)
    if c in (0x15, 0x16, 0x17):
        return td.names[v].strip() if 0 <= v < len(td.names) else v
    if c == 0x32:
        return path_str(v)
    if c in (0x0C, 0x0D, 0x0E):
        return [v.real, v.imag]
    if c == 0x40:
        el = td.items[0]

        def conv(x, d):
            return [conv(y, d - 1) for y in x] if d > 1 else [to_py(el, y) for y in x]
        return conv(v, td.dims)
    if c == 0x50:
        return [to_py(el, x) for el, x in zip(td.items, v)]
    if c == 0x53 and isinstance(v, Variant):
        return variant_py(v)
    return v


class _Elem:
    """Element i of an array Variant, with .td and .value like a Variant (for batch results)."""

    def __init__(self, arr: Variant, i: int):
        self.td = arr.td.items[0] if arr.td.code == 0x40 else arr.td
        if self.td.code == 0xF1:
            self.td = self.td.items[0]
        v = arr.value
        self.value = v[i] if isinstance(v, list) and i < len(v) else None


def variant_py(var: Variant):
    try:
        return to_py(var.td, var.value)
    except NotImplementedError:
        return f"<{type_str(var.td)}: not decoded>"


def short(v, n=20, s=200):
    """Shorten a value for describe output."""
    if isinstance(v, str):
        return v if len(v) <= s else v[:s] + f"...({len(v)} chars)"
    if isinstance(v, list):
        out = [short(x, n, s) for x in v[:n]]
        if len(v) > n:
            out.append(f"...({len(v)} items)")
        return out
    return v


def legacy_type_str(words) -> str:
    """Type name from a Terminal 'Type Descriptor' (I16 array, nested legacy format); fallback for old LabVIEW."""
    b = b"".join(int(w & 0xFFFF).to_bytes(2, "big") for w in words)

    def parse(i):
        size = int.from_bytes(b[i:i + 2], "big")
        code = b[i + 3]
        if code == 0x40:
            dims = int.from_bytes(b[i + 4:i + 6], "big")
            el, _ = parse(i + 6 + 4 * dims)
            return el + ("[]" if dims == 1 else f"[{dims}d]"), i + size
        if code == 0x50:
            n = int.from_bytes(b[i + 4:i + 6], "big")
            j, items = i + 6, []
            for _ in range(n):
                t, j = parse(j)
                items.append(t)
            return ("error" if items == ["bool", "I32", "string"] else "cluster{" + ",".join(items) + "}"), i + size
        try:
            td = parse_td(b[i:i + size], 0, [])[0]
            return type_str(td), i + size
        except Exception:
            return f"0x{code:02x}", i + size
    try:
        return parse(0)[0]
    except Exception:
        return "?"


def image_png(img) -> bytes:
    """LabVIEW 'Image Data' cluster [type, depth, image U8[], mask U8[], colors U32[], [left, top, right, bottom]]
    -> PNG bytes (Pillow)."""
    from PIL import Image
    _, depth, data, _mask, colors, rect = img
    l, t, r, b = rect
    w, h = max(r - l, 0), max(b - t, 0)
    if w == 0 or h == 0 or not data:
        im = Image.new("RGB", (1, 1), "white")
    elif depth == 24:
        stride = len(data) // h
        raw = bytes(data)
        if stride != 3 * w:
            raw = b"".join(raw[y * stride:y * stride + 3 * w] for y in range(h))
        im = Image.frombytes("RGB", (w, h), raw)
    elif depth == 32:
        im = Image.frombytes("RGBA", (w, h), bytes(data)[:4 * w * h], "raw", "ARGB")
    elif depth == 8:
        stride = len(data) // h
        pal = []
        for col in colors[:256]:
            pal += [(col >> 16) & 255, (col >> 8) & 255, col & 255]
        im = Image.frombytes("P", (w, h), b"".join(bytes(data[y * stride:y * stride + w]) for y in range(h)))
        im.putpalette(pal + [0] * (768 - len(pal)))
        im = im.convert("RGB")
    else:
        raise OpError(None, f"image depth {depth} not supported", "get_image")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------------------------------ the API

def private_boot_copy(src_dir, dst_dir, prefix: str) -> str:
    """Copy the boot wrappers to dst_dir as <prefix><name>.vi (unique VI names) for a client that shares its
    LabVIEW with other clients; pass dst_dir as boot_dir and prefix as boot_prefix. Local paths only (the
    LabVIEW machine must see dst_dir under the same path). Returns dst_dir."""
    import shutil
    src, dst = Path(src_dir), Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.vi"):
        t = dst / f"{prefix}{f.name}"
        if not t.exists() or t.stat().st_mtime < f.stat().st_mtime:
            shutil.copy2(f, t)
    return str(dst)


class LabVIEW:
    """High-level operations on one LabVIEW instance through one lvmcp server.

    host/port/token: the server; boot_dir/gen_dir: folders *as LabVIEW sees them* (boot wrappers, generated
    wrappers); cache_dir: local folder for the wrapper index + style table; prefix: file-name prefix of the
    generated wrappers (VI names are unique per LabVIEW instance, so two clients sharing one LabVIEW need
    different prefixes and gen dirs); boot_prefix: file-name prefix of the boot wrappers (several clients on
    one LabVIEW: give each a renamed copy of labview/boot, see `private_boot_copy`)."""

    def __init__(self, host="127.0.0.1", port=5577, token="", boot_dir=None, gen_dir=None, cache_dir=None,
                 prefix="lvmcp_", timeout=60, boot_prefix=""):
        self.c = Client(host, port, token, timeout=timeout)
        boot_dir = boot_dir or str(ROOT / "labview" / "boot")
        gen_dir = gen_dir or str(ROOT / "build" / "gen")
        self.S = Scripting(self.c, boot_dir, gen_dir, cache_dir, prefix=prefix, boot_prefix=boot_prefix)
        self._vis: set[int] = set()        # VI handles we know
        self._bd: dict[int, int] = {}      # vi -> block diagram handle
        self._vi_of: dict[int, int] = {}   # object handle -> vi handle
        self._refs: dict[int, set] = {}    # vi -> object handles to close with it
        self._cls: dict[int, str] = {}     # handle -> VI Server class name
        self._td_mode = "Data Type"        # terminal type property ("Type Descriptor" on old LabVIEW)
        self._batch_ok: dict = {}          # (class, props) -> False when the batch wrapper can't be used
        # (vi, object UID) -> the handle the creating tool returned: describe_vi/panel report that one, so an
        # object keeps one handle (LabVIEW hands out a new refnum for the same object on every listing)
        self._canon: dict[tuple, int] = {}
        self._term_of: dict[int, int] = {}  # panel object handle -> the terminal handle its creating tool returned

    # ---------------------------------------------------------------- low level
    def close(self):
        """Close the TCP connection (LabVIEW objects stay)."""
        self.c.close()

    def _wrap(self, kind, cls, item, write=False, skip=()) -> Wrapper:
        try:
            try:
                return self.S.wrapper(kind, cls, item, write, skip)
            except LVError as e:
                if "cannot run" not in str(e.source):
                    raise
                return self.S.wrapper(kind, cls, item, write, skip)   # rare flaky generation: retry once
        except LVError as e:
            if isinstance(e, OpError):
                raise
            what = "+".join(item) if isinstance(item, (list, tuple)) else item
            raise OpError(e.code, e.source, f"generate wrapper {kind} {cls}:{what}") from None

    def _run(self, w: Wrapper, inputs=None, outs=(), check=True, op="") -> dict:
        """Run a wrapper. inputs: {label or terminal name: python value | Variant}. Returns {label: Variant} plus
        "error out" (python list) when the wrapper has one."""
        try:
            return self._run1(w, inputs, outs, check, op)
        except (OSError, ConnectionError) as e:
            self.c.close()          # a late reply must not be taken for the answer to the next request
            raise OpError(None, f"server not reachable or not answering ({e.__class__.__name__}: {e}); "
                                "LabVIEW may be busy or blocked by a dialog", op) from None

    def _run1(self, w: Wrapper, inputs, outs, check, op) -> dict:
        lab = {t["name"]: t["label"] for t in w.terms}
        if w.terms:
            ins = {t["label"] for t in w.terms if t["dir"] == "in"}
            bad = [k for k in (inputs or {}) if lab.get(k, k) not in ins]
            if bad:
                raise OpError(None, f"no input {bad}; inputs are: "
                                    f"{sorted(x for x in ins if x not in ('reference', 'error in (no error)'))}", op)
        vals = {}
        for k, v in (inputs or {}).items():
            k = lab.get(k, k)
            if not isinstance(v, Variant):
                try:
                    v = w.typed(k, v, to_lv)
                except (ValueError, TypeError, OverflowError, KeyError) as e:
                    t = w.template(k)
                    raise OpError(None, f"cannot convert {v!r} to {type_str(t.td)} for '{k}': {e}", op) from None
            vals[k] = v
        outs = [lab.get(o, o) for o in outs]
        has_err = any(t["label"] == "error out" for t in w.terms)
        r = self.c.call(w.path, vals, outs=outs + (["error out"] if has_err else []), check=False)
        srv = r.pop("__error__")
        if srv.get("status"):
            raise OpError(srv.get("code"), srv.get("source"), op)
        if has_err:
            r["error out"] = r["error out"].value
            if check and r["error out"][0]:
                raise OpError(r["error out"][1], r["error out"][2], op)
        return r

    def _getv(self, ref, cls, props, check=True, op="") -> dict:
        """Read properties -> {property label: Variant} (+ "error out")."""
        w = self._wrap("prop", cls, props)
        labels = [t["label"] for t in w.terms if t["name"] not in _STD]
        return self._run(w, {"reference": ref}, labels, check=check, op=op or f"read {cls}:{props}")

    def _get(self, ref, cls, props, check=True, op=""):
        """Read one property (str) -> python value, or several (list) -> list of values."""
        single = isinstance(props, str)
        r = self._getv(ref, cls, [props] if single else props, check, op)
        vals = [r[k].value for k in list(r) if k != "error out"]
        return vals[0] if single else vals

    def _set(self, ref, cls, prop, value, op=""):
        w = self._wrap("prop", cls, prop, write=True)
        self._run(w, {"reference": ref, w.item: value}, op=op or f"write {cls}:{prop}")

    def _meth(self, ref, cls, method, inputs=None, outs=None, check=True, op="") -> dict:
        """Invoke a method. outs None = all outputs. Returns {label: python value} (+ "error out")."""
        w = self._wrap("meth", cls, method)
        if outs is None:
            outs = [t["label"] for t in w.terms if t["dir"] == "out" and t["label"] not in ("reference out", "error out")]
        r = self._run(w, {"reference": ref} | dict(inputs or {}), outs, check=check, op=op or f"{cls}:{method}")
        return {k: (v.value if isinstance(v, Variant) else v) for k, v in r.items()}

    def _reg(self, vi, *refs):
        if vi is None:
            return
        s = self._refs.setdefault(vi, set())
        for r in refs:
            if isinstance(r, int) and r and r != vi:
                s.add(r)
                self._vi_of[r] = vi

    def _class(self, h) -> str:
        if h in self._vis:
            return "VI"
        if h not in self._cls:
            try:
                self._cls[h] = self._get(h, "GObject", "Class Name", op="class of handle")
            except OpError as e:
                try:                                   # a VI handle this instance did not create/open?
                    self._get(h, "VI", "VI Name", op="class of handle")
                except OpError:
                    raise e from None
                self._vis.add(h)
                return "VI"
        return self._cls[h]

    def _as_vi(self, vi) -> int:
        if vi not in self._vis and self._class(vi) != "VI":
            raise OpError(None, f"handle {vi} is a {self._cls.get(vi)}, not a VI", "vi")
        return vi

    def _vi(self, h):
        return h if h in self._vis else self._vi_of.get(h)

    # ---------------------------------------------------------------- status / VIs
    def status(self) -> dict:
        """Server reachable? LabVIEW version, application kind, OS."""
        out = {"reachable": False, "host": self.c.addr[0], "port": self.c.addr[1]}
        vi = self.open_vi(self.S.boot_w("close_ref").path)       # any VI: its Owning Application
        out["reachable"] = True
        try:
            app = self._get(vi, "VI", "Owning Application", op="status")
            r = self._getv(app, "Application", ["Application:Version Number", "Application:Kind",
                                                "Operating System:Name", "Application:Target:CPU"], op="status")
            for k, key in (("Application:Version Number", "version"), ("Application:Kind", "app_kind"),
                           ("Operating System:Name", "os"), ("Application:Target:CPU", "cpu")):
                out[key] = variant_py(r[k])
        finally:
            self.close_vi(vi)
        out["styles"] = len(self.S.styles())
        return out

    def new_vi(self) -> int:
        """Create a new, untitled VI (automatic error handling off). Returns its handle."""
        try:
            vi = self.S.new_vi()
        except LVError as e:
            raise OpError(e.code, e.source, "new_vi") from None
        self._vis.add(vi)
        self._set(vi, "VI", "Automatic Error Handling", False, op="new_vi")
        return vi

    def open_vi(self, path: str) -> int:
        """Open a VI file (path as LabVIEW sees it). Returns its handle. Opening a VI already in memory returns
        a new handle to it."""
        w = self._wrap("prim", "", "Open VI Reference",
                       skip=("application reference (local)", "type specifier VI Refnum (for type only)"))
        vi = self._run(w, {"vi path": str(path)}, ["vi reference"], op=f"open_vi {path}")["vi reference"].value
        self._vis.add(vi)
        return vi

    def close_vi(self, vi: int):
        """Close the VI handle and every object handle obtained for it. A modified VI is NOT saved and no
        dialog is shown; it leaves memory once no handle to it is open."""
        for r in self._refs.pop(vi, set()):
            try:
                self.S.close(r)
            except LVError:
                pass
            self._vi_of.pop(r, None)
            self._cls.pop(r, None)
            self._term_of.pop(r, None)
        self._bd.pop(vi, None)
        self._vis.discard(vi)
        self._canon = {k: v for k, v in self._canon.items() if k[0] != vi}
        try:
            self.S.close(vi)
        except LVError as e:
            raise OpError(e.code, e.source, "close_vi") from None

    def vi_info(self, vi: int) -> dict:
        """Name, path, state (bad/idle/running), modified?"""
        self._as_vi(vi)
        r = self._getv(vi, "VI", ["VI Name", "VI Path", "Execution:State"], op="vi_info")
        return {"name": r["VI Name"].value, "path": variant_py(r["VI Path"]),
                "state": STATE.get(r["Execution:State"].value, r["Execution:State"].value)}

    def save_vi(self, vi: int, path: str | None = None) -> str:
        """Save the VI (to `path`, or to its current file). Returns the saved path. The VI is renamed after the
        file; a VI of that name already in memory gives error 1051."""
        self._as_vi(vi)
        if path is None:
            path = self.vi_info(vi)["path"]
            if not path:
                raise OpError(None, "the VI has never been saved: give a path", "save_vi")
        try:
            self.S.save(vi, str(path))
        except LVError as e:
            raise OpError(e.code, e.source, f"save_vi {path}") from None
        return str(path)

    def save_for_previous(self, vi: int, path: str, version: str = "14.0") -> dict:
        """Save a copy of the VI in an older LabVIEW format (e.g. "14.0" = LabVIEW 2014). The VI in memory
        keeps its own path. Returns {"path", "warnings"}."""
        self._as_vi(vi)
        r = self._meth(vi, "VI", "Save:For Previous", {"Path to Saved File": str(path), "Target Version": version},
                       op=f"save_for_previous {path}")
        warns = r.get("Warnings")
        return {"path": str(path), "warnings": warns or ""}

    def block_diagram(self, vi: int) -> int:
        """Handle of the VI's block diagram (a diagram handle for add_node)."""
        self._as_vi(vi)
        if vi not in self._bd:
            try:
                self._bd[vi] = self.S.block_diagram(vi)
            except LVError as e:
                raise OpError(e.code, e.source, "block_diagram") from None
            self._reg(vi, self._bd[vi])
            self._cls[self._bd[vi]] = "TopLevelDiagram"
        return self._bd[vi]

    # ---------------------------------------------------------------- terminals
    _TERM_PROPS = ["UID", "Name", "Is Source?", "Connected Wire"]

    def _term_info(self, refs) -> list[tuple]:
        """(uid, name, is_source, wire, type) per terminal ref, in one batch call."""
        if not refs:
            return []
        tprop = self._td_mode
        try:
            rows = self._batch("Terminal", self._TERM_PROPS + [tprop], refs, op="terminals")
        except OpError as e:
            if tprop != "Data Type" or e.code not in (1077, 1113, 1311):
                raise
            self._td_mode = "Type Descriptor"               # LabVIEW without Terminal:Data Type
            return self._term_info(refs)
        out = []
        for r in rows:
            v = r[tprop].value
            if r["error"][0] and not r["UID"].value:
                raise OpError(r["error"][1], r["error"][2], "terminal")
            if r["error"][0] or v is None:
                typ = "?"
            elif tprop == "Data Type":
                typ = type_str(v.td) if isinstance(v, Variant) else "?"
            else:
                typ = legacy_type_str(v or [])
            out.append((r["UID"].value, r["Name"].value, bool(r["Is Source?"].value), r["Connected Wire"].value, typ))
        return out

    def _term_rows(self, refs, names=None, info=None, subvi=False) -> list[dict]:
        """[{name, h, dir, type, wired, uid, wire}] for terminal refs; names: forced names (else Terminal:Name).
        Duplicate names get "#2", "#3"...; unassigned connector-pane slots of subVIs (subvi=True) are left out;
        other unnamed terminals (e.g. the element inputs of a Bundle with no cluster wired yet) are called
        "element" / "output"."""
        info = info if info is not None else self._term_info(refs)
        rows = []
        used: dict[str, int] = {}
        for i, (t, (uid, tname, src, wire, typ)) in enumerate(zip(refs, info)):
            if subvi and not names and not tname and typ == "void" and not wire:
                continue                                  # unassigned connector-pane slot of a subVI
            name = names[i] if names else (tname or ("output" if src else "element"))
            n = used.get(name, 0) + 1
            used[name] = n
            if n > 1:
                name = f"{name}#{n}"
            rows.append({"name": name, "h": t, "dir": "out" if src else "in", "type": typ, "wired": bool(wire),
                         "uid": uid, "wire": wire})
        return rows

    def _term_refs(self, h) -> tuple[list, list | None]:
        """(terminal refs, forced names or None) of any object handle."""
        cls = self._class(h)
        if cls == "VI":
            raise OpError(None, "a VI handle has no terminals; use a node/control handle", "terminals")
        if cls in ("WhileLoop",):
            i, stop = self._get(h, "WhileLoop", ["Loop Counter", "Loop End Ref"])
            return [i, stop], ["i", "stop"]
        if cls in ("ForLoop",):
            n, i = self._get(h, "ForLoop", ["Loop Count", "Loop Counter"])
            out, ins = self._get(n, "Tunnel", ["Outside Terminal", "Inside Terminals[]"])   # N is a tunnel
            return [out, i] + ins, ["N", "i"] + ["N.inside"] * len(ins)
        if cls in ("TimedLoop",):
            return [self._get(h, "Loop", "Loop Counter")], ["i"]
        if cls == "CaseStructure":
            sel = self._get(h, "CaseStructure", "Selector")
            out, ins = self._get(sel, "Tunnel", ["Outside Terminal", "Inside Terminals[]"])
            return [out] + ins, ["selector"] + ["selector.inside"] * len(ins)
        if "Tunnel" in cls or "ShiftRegister" in cls or cls == "Tunnel":
            out, ins = self._get(h, "Tunnel", ["Outside Terminal", "Inside Terminals[]"])
            return [out] + ins, ["outside"] + ["inside"] * len(ins)
        if cls.endswith("Terminal") and cls != "Terminal" or cls == "Terminal":
            return [h], ["value"]
        if "Constant" in cls:
            return [self._get(h, "Constant", "Terminal")], ["value"]
        try:                                              # Node and everything with Terminals[]
            return self._get(h, "Node", "Terminals[]", op="terminals"), None
        except OpError as e:
            if e.code != 1058:
                raise
        try:                                              # front-panel control -> its diagram terminal
            return [self._get(h, "Control", "Terminal", op="terminals")], ["value"]
        except OpError:
            pass
        try:
            return [self._get(h, "Constant", "Terminal", op="terminals")], ["value"]
        except OpError:
            raise OpError(1058, f"{cls} has no terminals", "terminals") from None

    def _is_subvi(self, h) -> bool:
        cls = self._cls.get(h) or ""
        if not cls:
            try:
                cls = self._class(h)
            except OpError:
                return False
        return "SubVI" in cls or cls in ("ExpressVI", "PolymorphicVI")

    def terminals(self, h: int) -> list[dict]:
        """Terminals of an object: [{"name", "h", "dir": "in"|"out", "type", "wired"}].
        Loops: i, stop (While) / N, i, N.inside (For); case structure: selector; tunnels and shift registers: outside,
        inside (one inside per case frame); constants, controls and control terminals: value."""
        refs, names = self._term_refs(h)
        rows = self._term_rows(refs, names, subvi=self._is_subvi(h))
        self._reg(self._vi(h), *[r["h"] for r in rows])
        return [{k: r[k] for k in ("name", "h", "dir", "type", "wired")} for r in rows]

    def _term(self, h, spec):
        """Resolve (object handle, terminal spec) -> terminal row. spec: name, "name#2", int index or None
        (the only terminal)."""
        refs, names = self._term_refs(h)
        sub = self._is_subvi(h)
        if spec is None:
            if len(refs) == 1:
                return self._term_rows(refs, names, subvi=sub)[0]
            spec = "value"
        rows = self._term_rows(refs, names, subvi=sub)
        if isinstance(spec, int):
            if 0 <= spec < len(rows):
                return rows[spec]
            raise OpError(None, f"terminal index {spec} out of range (0..{len(rows) - 1})", "terminal")
        for r in rows:
            if r["name"] == spec:
                return r
        for r in rows:
            if r["name"].lower() == str(spec).lower():
                return r
        if spec in ("value", "out", "output") and len(rows) == 1:
            return rows[0]
        raise OpError(None, f"no terminal {spec!r}; terminals: {[r['name'] for r in rows]}", "terminal")

    # ---------------------------------------------------------------- creating objects
    def _diagram(self, owner, frame=0) -> int:
        """Diagram handle for an owner: VI -> block diagram; loop -> its diagram; case/sequence -> frame
        `frame`; diagram -> itself."""
        if owner in self._vis:
            return self.block_diagram(owner)
        cls = self._class(owner)
        vi = self._vi(owner)
        if cls in ("Diagram", "TopLevelDiagram"):
            return owner
        if cls in ("WhileLoop", "ForLoop", "TimedLoop"):
            d = self._get(owner, "Loop", "Diagram")
        elif cls == "FlatSequence":
            fr = self._get(owner, "FlatSequence", "Frames[]")[frame]
            d = self._get(fr, "FlatSequenceFrame", "Diagram")
        elif cls == "FlatSequenceFrame":
            d = self._get(owner, "FlatSequenceFrame", "Diagram")
        else:
            try:
                d = self._get(owner, "Structure", "Diagrams[]", op="diagram of owner")[frame]
            except OpError:
                raise OpError(None, f"{cls} handle is not a diagram owner (use a VI, diagram or structure)",
                              "diagram") from None
        self._reg(vi, d)
        self._cls[d] = "Diagram"
        return d

    def styles(self) -> dict:
        """New VI Object style table {name: id} (names usable in add_node)."""
        return self.S.styles()

    def _style(self, name: str) -> int:
        st = self.S.styles()
        if name in st:
            return st[name]
        low = {k.lower(): v for k, v in st.items()}
        if name.lower() in low:
            return low[name.lower()]
        for alias in STRUCTURES.get(name.lower(), []):
            if alias in st:
                return st[alias]
        near = difflib.get_close_matches(name, list(st), n=8, cutoff=0.5)
        raise OpError(None, f"no object named {name!r}; close matches: {near}", "add_node")

    def _remember(self, vi, h, uid=None):
        """Make h the handle describe_vi/panel report for its object (see _canon). Best effort."""
        try:
            if uid is None:
                uid = self._get(h, "GObject", "UID", op="handle")
            if vi is not None and uid:
                self._canon.setdefault((vi, uid), h)
        except OpError:
            pass

    def _node_summary(self, h, vi) -> dict:
        r = self._getv(h, "GObject", ["UID", "Class Name", "Position", "Bounds"], op="node")
        cls = r["Class Name"].value
        self._cls[h] = cls
        self._remember(vi, h, r["UID"].value)
        out = {"h": h, "class": cls, "pos": r["Position"].value, "size": r["Bounds"].value}
        try:
            out["terms"] = [[t["name"], t["dir"], t["type"], t["wired"]] for t in self.terminals(h)]
        except OpError:
            out["terms"] = []
        if cls in ("WhileLoop", "ForLoop", "TimedLoop", "CaseStructure", "FlatSequence", "StackedSequence",
                   "EventStructure", "DisableStructure") or "Structure" in cls or "Sequence" in cls:
            out["diagrams"] = self._diagrams(h, cls)
        return out

    def add_node(self, owner: int, name: str, x: int, y: int, w: int = 0, h: int = 0) -> dict:
        """Drop an object by its New VI Object style name (see styles(): "Add", "Multiply", "Numeric Constant",
        "While Loop", "Case Structure", "Property Node", ...) on the diagram of `owner` (VI handle, diagram
        handle, or structure handle = its first diagram). Returns {"h", "class", "pos", "size", "terms":
        [[name, dir, type, wired]], "diagrams" (structures)}."""
        style = self._style(name)
        d = self._diagram(owner)
        nested = self._cls.get(d) != "TopLevelDiagram"     # sub-diagram: New VI Object coordinates are relative
        try:
            n = self.S.new_object(d, style, 0 if nested else int(x), 0 if nested else int(y), int(w), int(h))
        except LVError as e:
            raise OpError(e.code, e.source, f"add_node {name}") from None
        vi = self._vi(owner)
        self._reg(vi, n)
        self._place(n, x, y)
        return self._node_summary(n, vi)

    def add_panel_object(self, vi: int, name: str, x: int, y: int, label: str | None = None,
                         indicator: bool | None = None, w: int = 0, h: int = 0) -> dict:
        """Put a control/indicator on the front panel by its style name (e.g. "Waveform Graph (modern)",
        "Waveform Chart (modern)", "Numeric Control (modern)", "Stop Button"...; see styles()). x, y are front-panel
        coordinates; w, h: size for resizable objects (graphs/charts default to 320 x 200).
        indicator: True/False to force indicator/control (graphs/charts are indicators already).
        Returns {"h", "label", "kind", "terminal" (its block-diagram terminal: wire to it with terminal "value"),
        "type"}."""
        style = self._style(name)
        panel = self._get(vi, "VI", "Front Panel", op="add_panel_object")
        self._reg(vi, panel)
        try:
            c = self.S.new_object(panel, style, int(x), int(y), int(w), int(h))
        except LVError as e:
            raise OpError(e.code, e.source, f"add_panel_object {name}") from None
        self._reg(vi, c)
        self._remember(vi, c)
        cls = self._class(c)
        if not (w and h):              # objects that would be tiny: a sensible size
            nl = name.lower()
            size = (320, 200) if ("graph" in nl or "chart" in nl) else (320, 220) if cls == "TabControl" else \
                (200, 100) if cls == "Decoration" else None
            if size:
                self.resize_object(c, *size)
        if cls in ("Decoration", "Text"):
            if label and cls == "Text":
                self._set(c, "Text", "Text", str(label), op="add_panel_object")
            return {"h": c, "class": cls} | self._geometry(c)
        if indicator is not None:
            self._set(c, "Control", "Indicator", bool(indicator), op="add_panel_object")
        if label:
            self.set_label(c, label)
        term = self._get(c, "Control", "Terminal", op="add_panel_object")
        self._reg(vi, term)
        self._remember(vi, term)
        self._term_of[c] = term
        try:   # LabVIEW may drop the new terminal into a structure that covers its default spot
            bd = self.block_diagram(vi)
            own = self._get(term, "GObject", "Owner", op="add_panel_object")
            self._reg(vi, own)
            if self._get(own, "GObject", "UID") != self._get(bd, "GObject", "UID"):
                self._meth(term, "GObject", "Move", {"position": [20, 20], "owner": bd}, op="add_panel_object")
        except OpError:
            pass
        self._declutter(term, vi)
        ind = self._get(c, "Control", "Indicator", op="add_panel_object")
        out = {"h": c, "label": self.get_label(c), "kind": "indicator" if ind else "control", "terminal": term}
        if cls == "TabControl":
            out["pages"] = self._pages(c, vi)
        return out | self._geometry(c)

    _COUNT_PROP = {"BuildArray": "Input Count", "Bundler": "Input Count", "IndexArray": "Index Count",
                   "Unbundler": "Output Count"}

    def resize_node(self, node: int, count: int) -> list:
        """Grow/shrink a node with a variable number of terminals: inputs of Build Array, Concatenate Strings,
        Bundle; index pairs of Index Array; outputs of Unbundle. Returns the node's terminals (the new ones are
        named like the first, with "#2", "#3"...)."""
        cls = self._class(node)
        prop = self._COUNT_PROP.get(cls)
        if prop is None:                # other growable nodes (Build Cluster Array, ...): by their properties
            try:
                names = {p[1].split(":")[-1].strip() for p in self.class_members(cls)["properties"]}
                prop = next((p for p in ("Input Count", "Index Count", "Output Count") if p in names), None)
            except OpError:
                pass
        if prop is None:                # growable functions: Resize:Add/Remove Chunk
            ins = [t["name"].split("#")[0] for t in self.terminals(node) if t["dir"] == "in"]
            cur = max((ins.count(n) for n in set(ins)), default=0)
            try:
                if count > cur:
                    self._meth(node, "GrowableFunction", "Resize:Add Chunk", {"Chunks To Add": int(count) - cur},
                               op="resize_node")
                elif count < cur:
                    w = self._wrap("meth", "GrowableFunction", "Resize:Remove Chunk")
                    k = next(t["label"] for t in w.terms if t["dir"] == "in" and "hunk" in t["label"])
                    self._meth(node, "GrowableFunction", "Resize:Remove Chunk", {k: cur - int(count)},
                               op="resize_node")
                return self.terminals(node)
            except (OpError, StopIteration):
                pass
            raise OpError(None, f"handle {node} is a {cls}, which has no input/index/output count (resize_node "
                                "handles Build Array, Concatenate Strings, Bundle, Build Cluster Array, Index Array, "
                                "Unbundle, ...; for Bundle/Unbundle By Name use set_elements)", "resize_node")
        if int(count) < 1:
            raise OpError(None, "count must be >= 1", "resize_node")
        self._set(node, cls, prop, int(count), op="resize_node")
        return self.terminals(node)

    def set_elements(self, node: int, names: list[str]) -> list:
        """Choose the elements shown by a Bundle By Name / Unbundle By Name style node (also Build Waveform and
        Get Waveform Components), e.g. set_elements(gwc, ["Y", "dt"]). Returns the node's terminals."""
        cls = self._class(node)
        if cls not in ("NamedBundler", "NamedUnbundler"):
            raise OpError(None, f"handle {node} is a {cls}, not a Bundle/Unbundle By Name node", "set_elements")
        add, rem = (("AddInputAfter", "RemoveInput") if cls == "NamedBundler"
                    else ("AddOutputAfter", "RemoveOutput"))
        cur = self._get(node, cls, "Element Names[]", op="set_elements")
        avail = self._get(node, cls, "Available Elements[]", op="set_elements")
        bad = [n for n in names if n not in avail]
        if bad:
            raise OpError(None, f"no element {bad} (available: {avail})", "set_elements")
        while len(cur) < len(names):
            self._meth(node, cls, add, {"Index": len(cur) - 1}, op="set_elements")
            cur.append(None)
        while len(cur) > len(names):
            self._meth(node, cls, rem, {"Index": len(cur) - 1}, op="set_elements")
            cur.pop()
        self._set(node, cls, "Element Names[]", list(names), op="set_elements")
        return self.terminals(node)

    # ---------------------------------------------------------------- generic scripting (escape hatch)
    def class_members(self, cls: str) -> dict:
        """Properties and methods of a VI Server class (e.g. "WhileLoop", "NamedBundler", "VI", "Control"),
        as LabVIEW lists them: {"properties": [[short name, long name], ...], "methods": [...]}."""
        cache = getattr(self, "_members", None)
        if cache is None:
            cache = self._members = {}
        if cls in cache:
            return cache[cls]
        S = self.S
        vi = S.new_vi()
        try:
            bd = S.block_diagram(vi)
            out = {}
            for kind, style, cfg, ncls, prop in (
                    ("properties", STYLE_PROPERTY_NODE_, "propnode_config", "Property", "All Supported Properties"),
                    ("methods", STYLE_INVOKE_NODE, "invknode_config", "Invoke", "All Supported Methods")):
                n = S.new_object(bd, style, 100, 100)
                if kind == "properties":   # class only (no items: the Is Write step then fails harmlessly)
                    S.boot_w(cfg).run({"reference": n, "Property Node Class Name": S._cls(cls), "ID Strings[]": [],
                                       "Allow Alternate Names? (F)": True, "index": 0, "Is Write": False},
                                      check=False)
                else:
                    S.boot_w(cfg).run({"reference": n, "Invoke Node Class Name": S._cls(cls), "ID String": "",
                                       "Allow Alternate Names? (F)": True}, check=False)
                rows = self._get(n, ncls, prop, op="class_members") or []
                out[kind] = [[r[2], r[3]] for r in rows]
            # an unknown class name leaves the nodes at their default class (Application) without an error
            if cls.split(":")[-1].strip().lower() not in ("application", "app") and \
                    out == self.class_members("Application"):
                raise OpError(None, f"unknown VI Server class {cls!r} (use the class names add_node/describe_vi "
                                    "report, e.g. WhileLoop, Bundler, Control, Digital, VI)", "class_members")
            cache[cls] = out
            return out
        except LVError as e:
            raise OpError(e.code, e.source, f"class_members {cls}") from None
        finally:
            S.close(vi)

    def get_property(self, h: int, prop: str, cls: str | None = None):
        """Read any property (short or long name, see class_members) of an object; cls defaults to its class."""
        return self._get(h, cls or self._class(h), prop, op=f"get_property {prop}")

    def set_property(self, h: int, prop: str, value, cls: str | None = None):
        """Write any property of an object (value converted to the property's type)."""
        self._set(h, cls or self._class(h), prop, value, op=f"set_property {prop}")

    def invoke_method(self, h: int, method: str, inputs: dict | None = None, cls: str | None = None) -> dict:
        """Call any method of an object (or a VI handle); inputs by parameter label. Returns the outputs."""
        r = self._meth(h, cls or self._class(h), method, inputs or {}, op=f"invoke_method {method}")
        return {k: v for k, v in r.items() if k != "error out"}

    def _place(self, h, x, y):
        """Put an object at absolute block-diagram coordinates (New VI Object and Create Constant From Data
        Type take coordinates relative to the owning (sub)diagram; Position is absolute)."""
        pos = self._get(h, "GObject", "Position", op="place")
        if pos != [int(x), int(y)]:
            self._set(h, "GObject", "Position", [int(x), int(y)], op="place")

    def add_structure(self, owner: int, kind: str, x: int, y: int, w: int = 300, h: int = 200) -> dict:
        """Add a structure: kind "while", "for", "case", "flat sequence" (or any structure style name).
        Returns add_node's dict with "diagrams": [{"h", "name"?}] (add nodes inside with owner = a diagram h)."""
        return self.add_node(owner, kind, x, y, w, h)

    def _diagrams(self, s, cls=None) -> list[dict]:
        cls = cls or self._class(s)
        vi = self._vi(s)
        if cls in ("WhileLoop", "ForLoop", "TimedLoop"):
            ds = [{"h": self._get(s, "Loop", "Diagram")}]
        elif cls == "FlatSequence":
            ds = [{"h": self._get(f, "FlatSequenceFrame", "Diagram")} for f in self._get(s, "FlatSequence", "Frames[]")]
        else:
            ds = [{"h": d} for d in self._get(s, "Structure", "Diagrams[]")]
            if cls == "CaseStructure":
                names, default = self._get(s, "CaseStructure", ["Frame Names", "Default Case"])
                for i, (d, n) in enumerate(zip(ds, names)):
                    d["name"] = n.strip()
                    if i == default:
                        d["default"] = True
        for d in ds:
            self._reg(vi, d["h"])
            self._cls[d["h"]] = "Diagram"
        return ds

    def add_frame(self, struct: int, after: int | None = None) -> dict:
        """Add a frame to a case structure / flat sequence (after frame index `after`, default: last).
        Returns {"diagrams": [...]} of the structure."""
        cls = self._class(struct)
        mcls = "FlatSequence" if cls == "FlatSequence" else "MultiFrameStructure"
        if after is None:
            after = len(self._diagrams(struct, cls)) - 1
        self._meth(struct, mcls, "Add Frame", {"Reference Frame Index": after, "After? (T)": True}, op="add_frame")
        return {"diagrams": self._diagrams(struct, cls)}

    def set_case_names(self, case: int, names: list[str]) -> dict:
        """Set the selector values of a case structure's frames, e.g. ["0", "1", "2..5", "Default"] or
        ["False", "True"] (wire the selector first so the type is known). Returns {"diagrams"}."""
        self._set(case, "CaseStructure", "Frame Names", [str(n) for n in names], op="set_case_names")
        return {"diagrams": self._diagrams(case, "CaseStructure")}

    def add_shift_register(self, loop: int, y: int | None = None) -> dict:
        """Add a shift register pair to a loop. Returns {"right": {h, terms}, "left": {h, terms}};
        terminals "outside" (initializer on the left / result on the right) and "inside"."""
        inp = {} if y is None else {"Y Positon": int(y)}
        right = self._meth(loop, "Loop", "Add Shift Register", inp, op="add_shift_register")["Add Shift Register"]
        vi = self._vi(loop)
        self._reg(vi, right)
        self._cls[right] = "RightShiftRegister"
        left = self._get(right, "RightShiftRegister", "Left Registers[]")[0]
        self._reg(vi, left)
        self._cls[left] = "LeftShiftRegister"
        return {"right": {"h": right, "terms": [[t["name"], t["dir"], t["type"], t["wired"]] for t in self.terminals(right)]},
                "left": {"h": left, "terms": [[t["name"], t["dir"], t["type"], t["wired"]] for t in self.terminals(left)]}}

    def set_tunnel_indexing(self, tunnel: int, indexing: bool):
        """Loop tunnel: auto-indexing on/off (LoopTunnel:Index Mode)."""
        self._set(tunnel, "LoopTunnel", "Index Mode", 1 if indexing else 0, op="set_tunnel_indexing")

    def _subvi_wrapper(self) -> Wrapper:
        """Wrapper of New VI Object with class SubVI and a path input (generated once, cached)."""
        S = self.S
        name = S.prefix + self.SUBVI_WRAPPER
        path = lv_join(S.gen, name)
        if not S.has(name):
            vi = S.new_vi()
            try:
                bd = S.block_diagram(vi)
                n = S.new_object(bd, STYLE_NEW_VI_OBJECT, 300, 200)
                T = {S.term_name(t): t for t in S.node_info(n)[0]}
                k = S.create_constant(T["vi object class"])
                types = self._get(k, "ClassSpecifierConstant", "All Types[]", op="SubVI class")
                sub = [t for t in types if t[2] == "SubVI"]
                if not sub:
                    raise OpError(None, "class SubVI not found in class specifier types", "add_subvi")
                self._meth(k, "ClassSpecifierConstant", "Set Type", {"ID String": sub[0][0]}, op="SubVI class")
                info = []
                for tn, d in (("owner refnum", "in"), ("path", "in"), ("position/next to", "in"),
                              ("error in (no error)", "in"), ("object refnum", "out"), ("error out", "out")):
                    ref, err = (S.create_control if d == "in" else S.create_indicator)(T[tn], check=False)
                    if err[0]:
                        raise OpError(err[1], err[2], f"subVI wrapper: control for {tn}")
                    info.append({"name": tn, "dir": d, "label": S.label_of(ref)})
                S.save_new(vi, path)
            finally:
                S.close(vi)
            S.index[name] = {"kind": "custom", "class": "SubVI", "item": "New VI Object", "write": False,
                             "terms": info}
            S.save_index()
        w = Wrapper(self.c, path)
        w.terms = S.index[name]["terms"]
        w.peek = S.peek
        return w

    # array source of each batch class: a property whose value is an array of that class (its control type)
    _BATCH_SRC = {"Terminal": ("Node", "Terminals[]"), "GObject": ("Diagram", "All Objects[]"),
                  "Node": ("Diagram", "Nodes[]"), "Wire": ("Diagram", "Wires[]"), "Control": ("Panel", "Controls[]")}

    SUBVI_WRAPPER = "custom_New_SubVI.vi"

    def batch_wrapper_name(self, cls: str, props: list[str]) -> str:
        key = f"batch_{cls}_{'+'.join(props)}"
        return self.S.prefix + __import__("re").sub(r"[^A-Za-z0-9_.-]+", "_", key)[:120] + ".vi"

    def _batch_wrapper(self, cls: str, props: list[str]) -> Wrapper:
        """Wrapper reading `props` of every refnum in an array (For Loop around a property node), so one
        server call serves many objects. Controls: "refs"; indicators: one array per property + "errors"."""
        S = self.S
        name = self.batch_wrapper_name(cls, props)
        path = lv_join(S.gen, name)
        if not S.has(name):
            st = S.styles()
            src_cls, src_prop = self._BATCH_SRC[cls]
            vi = S.new_vi()
            try:
                bd = S.block_diagram(vi)
                loop = S.new_object(bd, st["For Loop"], 150, 40, 300, 60 + 40 * len(props))
                ld = self._get(loop, "Loop", "Diagram")
                src = S.new_object(bd, STYLE_PROPERTY_NODE_, 20, 60)
                S.config_property(src, src_cls, [src_prop])
                pn = S.new_object(ld, STYLE_PROPERTY_NODE_, 60, 10)
                S.config_property(pn, cls, list(props))
                st_names = {S.term_name(t): t for t in S.node_info(src)[0]}
                item_t = [t for n, t in st_names.items() if n not in _STD][0]
                ctl, err = S.create_control(item_t, check=False)        # unwired array control on an output
                if err[0]:
                    raise OpError(err[1], err[2], "batch wrapper: refs control")
                self._set(self._get(ctl, "Control", "Label"), "Text", "Text", "refs")
                ctl_t = self._get(ctl, "Control", "Terminal")
                pts = S.node_info(pn)[0]
                pnames = [S.term_name(t) for t in pts]
                S.connect(ctl_t, pts[pnames.index("reference")])      # auto-indexing tunnel
                info = [{"name": "refs", "dir": "in", "label": "refs"}]
                outs = [(t, n) for t, n in zip(pts, pnames) if n not in ("reference", "reference out",
                                                                          "error in (no error)")]
                k = 0
                for i, (t, n) in enumerate(outs):
                    lab = "errors" if n == "error out" else props[k]
                    if n != "error out":
                        k += 1
                    sub = S.new_object(bd, st["Array Subset"], 520, 40 + 50 * i)
                    sts = {S.term_name(x): x for x in S.node_info(sub)[0]}
                    S.connect(t, sts["array"])                          # output tunnel, auto-indexing
                    ind, err = S.create_indicator(sts["subarray"], check=False)
                    if err[0]:
                        raise OpError(err[1], err[2], f"batch wrapper: indicator {lab}")
                    self._set(self._get(ind, "Control", "Label"), "Text", "Text", lab)
                    info.append({"name": lab, "dir": "out", "label": lab})
                self._meth(src, "GObject", "Delete")
                self._set(vi, "VI", "Automatic Error Handling", False)
                if self._get(vi, "VI", "Execution:State") == 0:
                    raise OpError(None, f"generated batch wrapper {name} is broken", "batch wrapper")
                S.save_new(vi, path)
            finally:
                S.close(vi)
            S.index[name] = {"kind": "batch", "class": cls, "item": list(props), "write": False, "terms": info}
            S.save_index()
        w = Wrapper(self.c, path)
        w.terms = S.index[name]["terms"]
        w.peek = S.peek
        return w

    def _batch(self, cls: str, props: list[str], refs: list[int], op="batch") -> list[dict]:
        """[{prop: Variant, "error": [status, code, source]}] per ref, in one server call (falls back to one
        call per ref when the batch wrapper cannot be built)."""
        if not refs:
            return []
        if self._batch_ok.get((cls, tuple(props)), True):
            try:
                w = self._batch_wrapper(cls, props)
            except OpError as e:
                if e.code in (1077, 1113, 1311):
                    raise                                   # unknown property: same for the slow path
                w = None
                self._batch_ok[(cls, tuple(props))] = False  # can't build it: use one call per ref
            try:
                r = self.c.call(w.path, {"refs": w.template("refs").with_value(list(refs))},
                                outs=list(props) + ["errors"], check=False) if w else None
                if r and not r["__error__"].get("status"):
                    cols = {p: r[p] for p in props}
                    errs = r["errors"].value
                    out = []
                    for i in range(len(refs)):
                        row = {"error": errs[i] if i < len(errs) else [True, 0, "missing"]}
                        for p in props:
                            row[p] = _Elem(cols[p], i)
                        out.append(row)
                    return out
            except (OpError, LVError, KeyError, IndexError, NotImplementedError):
                pass                                        # this call only: fall back below
        out = []
        for ref in refs:
            r = self._getv(ref, cls, props, check=False, op=op)
            out.append({"error": r.pop("error out")} | {p: r[lab] for p, lab in zip(props, list(r))})
        return out

    def add_subvi(self, owner: int, vi_path: str, x: int, y: int) -> dict:
        """Place a VI file as a subVI node (path as LabVIEW sees it, e.g. a vi.lib VI). Returns add_node's dict."""
        w = self._subvi_wrapper()
        d = self._diagram(owner)
        nested = self._cls.get(d) != "TopLevelDiagram"
        r = self._run(w, {"owner refnum": d, "path": str(vi_path),
                          "position/next to": [0, 0] if nested else [int(x), int(y)]},
                      ["object refnum"], op=f"add_subvi {vi_path}")
        n = r[[t["label"] for t in w.terms if t["name"] == "object refnum"][0]].value
        vi = self._vi(owner)
        self._reg(vi, n)
        self._place(n, x, y)
        out = self._node_summary(n, vi)
        try:
            out["name"] = self._get(n, "SubVI", "VI Name")
        except OpError:
            pass
        return out

    def set_connector(self, vi: int, inputs: list[str] = (), outputs: list[str] = ()) -> dict:
        """Assign front-panel controls (by label) to the VI's connector pane so it can be used as a subVI:
        `inputs` go to the left-side terminals (top to bottom), `outputs` to the right side. Uses the VI's
        current pattern (new VIs: 4x2x2x4 = 4 left, 4 right). Returns {"pattern", "assigned": {label: index}}."""
        self._as_vi(vi)
        cp = self._get(vi, "VI", "Connector Pane:Reference", op="set_connector")
        self._reg(vi, cp)
        pattern, bounds = self._get(cp, "ConnectorPane", ["Pattern", "Terminal Bounds[]"], op="set_connector")
        # bounds: [left, top, right, bottom] per terminal index
        mid = (min(b[0] for b in bounds) + max(b[2] for b in bounds)) / 2
        left = sorted([i for i, b in enumerate(bounds) if (b[0] + b[2]) / 2 < mid], key=lambda i: bounds[i][1])
        right = sorted([i for i, b in enumerate(bounds) if (b[0] + b[2]) / 2 > mid], key=lambda i: bounds[i][1])
        if len(inputs) > len(left) or len(outputs) > len(right):
            raise OpError(None, f"pattern {pattern} has {len(left)} left / {len(right)} right terminals",
                          "set_connector")
        ctls = {c["label"]: c["h"] for c in self.panel(vi, values=False)}
        assigned = {}
        for labels, idxs in ((inputs, left), (outputs, right)):
            for lab, idx in zip(labels, idxs):
                if lab not in ctls:
                    raise OpError(None, f"no front-panel object labelled {lab!r}; have {list(ctls)}", "set_connector")
                self._meth(cp, "ConnectorPane", "Assign Control To Terminal", {"Control": ctls[lab], "Terminal Index": idx},
                           op="set_connector")
                assigned[lab] = idx
        return {"pattern": pattern, "assigned": assigned}

    def wire(self, src: int, src_terminal, dst: int, dst_terminal) -> dict:
        """Wire src.src_terminal (an output) to dst.dst_terminal (an input). Terminals by name ("x", "x+y",
        "element#2"), index, or None for single-terminal objects (constants, controls). Crossing structure
        borders creates tunnels automatically. Returns {"wire": handle}."""
        s = self._term(src, src_terminal)
        d = self._term(dst, dst_terminal)
        if s["dir"] == "in" and d["dir"] == "out":
            s, d = d, s                                    # given the other way round
        try:
            self.S.connect(s["h"], d["h"])
        except LVError as e:
            raise OpError(e.code, e.source, f"wire {src_terminal} -> {dst_terminal}") from None
        wire = self._get(d["h"], "Terminal", "Connected Wire")
        self._reg(self._vi(dst) or self._vi(src), wire)
        return {"wire": wire}

    @staticmethod
    def _type_variant(type_: str, value) -> Variant:
        """Variant of a type written as text: scalars (I32, DBL, U8, ..., bool, string, path), arrays ("DBL[]",
        2D "DBL[,]"), clusters with element labels ("cluster{x:DBL, y:DBL}", "{Name:string, Score:DBL}[]";
        "string Name" works too). value None/0/[] gives an empty value."""
        try:
            types, top = parse_type_spec(str(type_))
        except ValueError as e:
            raise OpError(None, f"{e}; known scalars: {sorted(TYPE_CODES)}, arrays like 'DBL[]' or 'DBL[,]', "
                                "clusters like 'cluster{x:DBL, y:DBL}'", "constant") from None
        var = Variant(VERSION_OF_LV(), types, top, b"", b"\x00\x00\x00\x00")
        td = types[top]
        if value is None or (value == 0 and td.code in (0x40, 0x50)) or (value == [] and td.code == 0x50):
            value = empty_value(td)
        return var.with_value(to_lv(td, value))

    def _const_variant(self, tdv: Variant | None, value, type_=None) -> Variant:
        if type_:
            return self._type_variant(type_, value)
        if tdv is not None and tdv.td.code not in (0x00, 0x53):
            try:
                return tdv.with_value(to_lv(tdv.td, value))
            except (ValueError, TypeError, OverflowError, NotImplementedError) as e:
                raise OpError(None, f"cannot convert {value!r} to {type_str(tdv.td)}: {e}", "constant") from None
        if isinstance(value, bool):
            return make_variant(BOOL, value)
        if isinstance(value, int):
            return make_variant(I32, value)
        if isinstance(value, float):
            return make_variant(DBL, value)
        if isinstance(value, str):
            return make_variant(STRING, value)
        if isinstance(value, (list, tuple)) and value:
            x = value[0]
            t = "bool" if isinstance(x, bool) else "I32" if isinstance(x, int) else "DBL" if isinstance(x, float) \
                else "string" if isinstance(x, str) else None
            if t:
                if t == "I32" and any(isinstance(y, float) for y in value):
                    t = "DBL"
                return self._type_variant(t + "[]", list(value))
        raise OpError(None, f"give type= for value {value!r}", "constant")

    def create_constant(self, node: int, terminal=None, value=None, type: str | None = None,
                        label: str | None = None) -> dict:
        """Create a constant wired to an input terminal. value: Python value converted to the terminal's type
        (or to `type`: "I32", "DBL", "U8", "string", "bool", "path", ...). Returns {"h", "class", "value"}."""
        t = self._term(node, terminal)
        if type:
            try:
                return self._typed_constant(node, t, type, value, label)
            except OpError as e:
                if e.code not in (1077, 1113):
                    raise                                   # (old LabVIEW: no Create Constant From Data Type)
        var = None
        if value is not None:
            tdv = None
            if not type and self._td_mode == "Data Type":
                r = self._getv(t["h"], "Terminal", "Data Type", check=False, op="create_constant")
                tdv = None if r["error out"][0] else r["Data Type"]   # e.g. a For Loop's N: no Data Type
            var = self._const_variant(tdv, value, type)
        try:
            k = self.S.create_constant(t["h"], var)
        except LVError as e:
            raise OpError(e.code, e.source, "create_constant") from None
        vi = self._vi(node)
        self._reg(vi, k)
        cls = self._class(k)
        self._declutter(k, vi)
        if value is not None and var is None:          # (old LabVIEW without Data Type): set afterwards
            self.set_constant(k, value)
        if label:
            self.set_label(k, label)
        out = {"h": k, "class": cls}
        try:
            out["value"] = variant_py(self._getv(k, "Constant", "Value")["Value"])
        except OpError:
            pass
        return out

    def _typed_constant(self, node, t, type_, value, label) -> dict:
        """Constant of an explicit type (e.g. I32 for a case selector whose default type is bool): Diagram
        method Create Constant From Data Type next to the terminal, set its value, wire it."""
        given = value is not None
        if not given:       # an empty value of that type
            tl = type_.strip().lower()
            value = [] if tl.endswith("]") else "" if tl in ("string", "str", "path") else False \
                if tl in ("bool", "boolean") else 0
        var = self._type_variant(type_, value)
        d = self._get(t["h"], "Terminal", "Diagram", op="create_constant")
        try:
            pos = self._get(node, "GObject", "Position")
        except OpError:
            pos = [60, 20]
        vi = self._vi(node)
        self._reg(vi, d)
        r = self._meth(d, "Diagram", "Create Constant From Data Type",
                       {"Data Type": var, "Position": [0, 0]}, op="create_constant")
        k = r["New Object Reference"]
        self._reg(vi, k)
        self._place(k, max(pos[0] - 40, 0), pos[1])
        if given:
            self.set_constant(k, value)
        kt = self._get(k, "Constant", "Terminal")
        self._reg(vi, kt)
        try:
            self.S.connect(kt, t["h"])
        except LVError as e:
            raise OpError(e.code, e.source, "create_constant: wire") from None
        self._declutter(k, vi)
        if label:
            self.set_label(k, label)
        return {"h": k, "class": self._class(k),
                "value": variant_py(self._getv(k, "Constant", "Value")["Value"])}

    def set_constant(self, const: int, value):
        """Change a diagram constant's value (converted to the constant's own type)."""
        cur = self._getv(const, "Constant", "Value", op="set_constant")["Value"]
        try:
            v = cur.with_value(to_lv(cur.td, value))
        except (ValueError, TypeError, OverflowError) as e:
            raise OpError(None, f"cannot convert {value!r} to {type_str(cur.td)}: {e}", "set_constant") from None
        self._set(const, "Constant", "Value", v, op="set_constant")

    def _make_fp(self, node, terminal, label, indicator: bool, owner=None, x=None, y=None, type=None) -> dict:
        op = "create_indicator" if indicator else "create_control"
        t = self._term(node, terminal)
        if indicator and t["dir"] != "out":
            raise OpError(None, f"terminal {t['name']!r} is an input: use create_control or create_constant", op)
        if not indicator and t["dir"] != "in":
            raise OpError(None, f"terminal {t['name']!r} is an output: use create_indicator", op)
        if owner is not None and (x is None or y is None):
            raise OpError(None, "give x and y (absolute) together with owner", op)
        vi = self._vi(node)
        if type and not indicator:
            # typed constant on the terminal, changed into a control (e.g. DBL[] for a terminal that would
            # make a scalar control before it is wired)
            k = self._typed_constant(node, t, type, None, None)["h"]
            r = self._meth(k, "Constant", "Change to Control", op=op)
            new = next((v for v in r.values() if isinstance(v, int) and v), None)
            if not new:
                raise OpError(None, "Change to Control returned no object", op)
            ctl = new                                       # (the new front-panel control)
        else:
            try:
                ctl = (self.S.create_indicator if indicator else self.S.create_control)(t["h"])[0]
            except LVError as e:
                raise OpError(e.code, e.source, op) from None
        term = self._get(ctl, "Control", "Terminal")
        self._reg(vi, ctl, term)
        self._cls[term] = "ControlTerminal"
        if owner is not None:
            # the terminal is created on the node's diagram: delete its wire, move it to the owner's diagram,
            # wire again (crossing structure borders makes tunnels)
            d = self._diagram(owner)
            wire = self._get(t["h"], "Terminal", "Connected Wire", op=op)
            keep = []
            if wire:
                self._reg(vi, wire)
                if indicator:   # the output's wire may have had other branches: rewire those afterwards
                    own = self._get(term, "Terminal", "UID", op=op)
                    for x_ in self._get(wire, "Wire", "Terminals[]", op=op) or []:
                        self._reg(vi, x_)
                        q = self._getv(x_, "Terminal", ["UID", "Is Source?"], check=False, op=op)
                        if not q["error out"][0] and not q["Is Source?"].value and q["UID"].value != own:
                            keep.append(x_)
                self._meth(wire, "GObject", "Delete", op=op)
            self._meth(term, "GObject", "Move", {"position": [int(x), int(y)], "owner": d}, op=op)
            self._place(term, x, y)
            try:
                if indicator:
                    self.S.connect(t["h"], term)
                    for k in keep:
                        self.S.connect(t["h"], k)
                else:
                    self.S.connect(term, t["h"])
            except LVError as e:
                raise OpError(e.code, e.source, f"{op}: wire") from None
            if indicator:
                ctl, term = self._fix_tunnel_type(vi, ctl, term, x, y, op)
        else:
            self._declutter(term, vi)
        if label:
            self.set_label(ctl, label)
        self._remember(vi, ctl)
        self._remember(vi, term)
        self._term_of[ctl] = term
        typ = self._term_rows([term])[0]["type"] if owner is not None or type else t["type"]
        return {"h": ctl, "terminal": term, "label": self.get_label(ctl), "type": typ}

    def _declutter(self, h, vi=None):
        """If the new object `h` overlaps others on its diagram (LabVIEW puts the controls/indicators/constants of
        neighbouring terminals on top of each other), move it down to the first free spot. Labels (above the
        object) are counted in. Best effort: layout problems never fail the operation."""
        try:
            d = self._get(h, "GObject", "Owner", op="layout")
            self._reg(vi, d)
            objs = self._get(d, "Diagram", "All Objects[]", op="layout") or []
            self._reg(vi, *objs)
            rows = self._batch("GObject", ["UID", "Class Name", "Position", "Bounds"], objs, op="layout")
            me = self._getv(h, "GObject", ["UID", "Position", "Bounds"], op="layout")
            uid, (x, y), (w, ht) = me["UID"].value, me["Position"].value, me["Bounds"].value
            rects = []
            for r in rows:
                if r["error"][0]:
                    continue
                if r["UID"].value == uid or r["Class Name"].value in ("Wire", "Label", "FreeLabel"):
                    continue
                (ox, oy), (ow, oh) = r["Position"].value, r["Bounds"].value
                rects.append((ox, oy - 14, ox + ow, oy + oh))        # + label above
            pad, y0 = 4, y
            for _ in range(40):
                hit = [r for r in rects if x < r[2] + pad and x + w + pad > r[0] and y - 14 < r[3] + pad
                       and y + ht + pad > r[1]]
                if not hit:
                    break
                y = max(r[3] for r in hit) + pad + 15
            if y != y0:
                self._set(h, "GObject", "Position", [int(x), int(y)], op="layout")
        except (OpError, LVError, TypeError, ValueError, IndexError):
            pass

    def _fix_tunnel_type(self, vi, ctl, term, x, y, op):
        """An indicator wired out through a structure border has the inner terminal's type, but the output tunnel
        may carry another one (auto-indexing: an array). Then make it again on the tunnel's outside terminal."""
        w = self._get(term, "Terminal", "Connected Wire", op=op)
        if not w:
            return ctl, term
        self._reg(vi, w)
        src = None
        for x_ in self._get(w, "Wire", "Terminals[]", op=op) or []:
            self._reg(vi, x_)
            if self._get(x_, "Terminal", "Is Source?", op=op):
                src = x_
        if src is None:
            return ctl, term
        a, b = self._term_rows([src, term], None)
        if a["type"] == b["type"]:
            return ctl, term
        lab = self.get_label(ctl)
        self._meth(w, "GObject", "Delete", op=op)
        self._meth(ctl, "GObject", "Delete", op=op)          # the panel object takes its terminal along
        try:
            ctl = self.S.create_indicator(src)[0]
        except LVError as e:
            raise OpError(e.code, e.source, op) from None
        term = self._get(ctl, "Control", "Terminal")
        self._reg(vi, ctl, term)
        self._cls[term] = "ControlTerminal"
        self._place(term, x, y)
        self.set_label(ctl, lab)
        return ctl, term

    def create_control(self, node: int, terminal=None, label: str | None = None, owner: int | None = None,
                       x: int | None = None, y: int | None = None, type: str | None = None) -> dict:
        """Create a front-panel control wired to an input terminal. Returns {"h" (panel control),
        "terminal" (its diagram terminal), "label", "type"}. The diagram terminal goes next to the node (same
        (sub)diagram); with owner (VI / diagram / structure handle) and absolute x, y it is put on that diagram
        instead and wired from there (tunnels are made, e.g. a control outside a loop).
        type: make the control of this type instead of the terminal's (e.g. "DBL[]", "I32", "string")."""
        return self._make_fp(node, terminal, label, False, owner, x, y, type)

    def create_indicator(self, node: int, terminal=None, label: str | None = None, owner: int | None = None,
                         x: int | None = None, y: int | None = None) -> dict:
        """Create a front-panel indicator wired to an output terminal. Returns like create_control; owner/x/y
        likewise (e.g. an indicator outside the case structure that holds the node)."""
        return self._make_fp(node, terminal, label, True, owner, x, y)

    def _label_ref(self, h) -> int:
        cls = self._class(h)
        order = ["Control", "Node", "Constant"]
        if "Constant" in cls:
            order = ["Constant", "Node", "Control"]
        elif cls == "ControlTerminal":
            h = self._get(h, "ControlTerminal", "Control")
        last = None
        for c in order:
            try:
                lbl = self._get(h, c, "Label", op="label")
                self._reg(self._vi(h), lbl)
                return lbl
            except OpError as e:
                last = e
                if e.code not in (1058, 1077):
                    raise
        raise last

    def get_label(self, h: int) -> str:
        """Label text of a control/indicator/constant/node."""
        return self._get(self._label_ref(h), "Text", "Text", op="get_label")

    def set_label(self, h: int, text: str):
        """Rename the label of a control/indicator/constant/node."""
        self._set(self._label_ref(h), "Text", "Text", str(text), op="set_label")

    def delete_object(self, h: int):
        """Delete a diagram/panel object (node, constant, structure, control, wire). Wire ends left loose by the
        deletion are removed (the rest of those wires stays)."""
        diagrams = []
        for src, cls, prop in ((h, "Control", "Terminal"), (h, None, None)):   # a panel control's terminal
            try:
                o = self._get(src, cls, prop, check=True, op="delete_object") if cls else src
                d = self._get(o, "GObject", "Owner", op="delete_object")
                if self._get(d, "GObject", "Class Name", op="delete_object") in ("Diagram", "TopLevelDiagram"):
                    diagrams.append(d)
            except OpError:
                pass
        self._meth(h, "GObject", "Delete", op="delete_object")
        for d in diagrams:
            try:
                self._meth(d, "Diagram", "Remove Wire Loose Ends", op="delete_object")
            except OpError:
                pass                           # (method not in this LabVIEW version)
            try:
                self.S.close(d)
            except LVError:
                pass
        vi = self._vi_of.pop(h, None)
        if vi in self._refs:
            self._refs[vi].discard(h)
        self._cls.pop(h, None)

    def move_object(self, h: int, x: int, y: int, owner: int | None = None):
        """Move an object to (x, y) (diagram/panel coordinates); with owner = a diagram/structure handle, move
        it into that diagram. Front-panel objects: owner = a tab control page (from "pages") or the VI (back onto
        the panel itself); x, y stay panel coordinates."""
        if owner is None:
            self._set(h, "GObject", "Position", [int(x), int(y)], op="move_object")
            return
        oc = self._class(owner)
        if oc == "Page" or (oc == "VI" and self._on_panel(h)):
            dest = owner if oc == "Page" else self._get(owner, "VI", "Front Panel", op="move_object")
            self._reg(self._vi(h), dest)
            ox, oy = self._get(dest, "GObject", "Position", op="move_object") if oc == "Page" else (0, 0)
            # Move takes coordinates relative to the new owner
            self._meth(h, "GObject", "Move", {"position": [int(x) - ox, int(y) - oy], "owner": dest},
                       op="move_object")
            self._place(h, x, y)
            return
        self._meth(h, "GObject", "Move", {"position": [int(x), int(y)], "owner": self._diagram(owner)},
                   op="move_object")

    def _on_panel(self, h) -> bool:
        cls = self._class(h)
        if cls in ("Decoration", "Page"):
            return True
        if "Terminal" in cls:
            return False
        try:
            return self._get(h, "GObject", "Is On Block Diagram?", op="move_object") is False
        except OpError:
            try:                                  # (property missing in this LabVIEW): a control has a terminal
                self._get(h, "Control", "Terminal", op="move_object")
                return True
            except OpError:
                return False

    def cleanup(self, vi: int):
        """Clean Up Diagram (rearranges the whole block diagram)."""
        self._as_vi(vi)
        try:
            self._meth(vi, "VI", "Block Diagram:Clean Up", op="cleanup")
        except OpError as e:
            if e.code not in (1077, 1113):
                raise
            self._meth(self.block_diagram(vi), "Diagram", "Clean Up Diagram", op="cleanup")

    # ---------------------------------------------------------------- front-panel layout
    # Front-panel Position/Bounds cover the whole object with its label (Position = top-left of that box).
    _BIG = ("WaveformGraph", "WaveformChart", "XYGraph", "IntensityGraph", "IntensityChart", "DigitalGraph",
            "MixedSignalGraph", "Table", "MulticolumnListbox", "ListBox", "TabControl", "Picture", "SubPanel",
            "TreeControl")
    # size properties ({Width, Height} clusters) in the order tried; Digital numerics only have "Text Width"
    _SIZE_PROPS = ("Size", "Button Size", "Housing Size", "Named Numeric Size", "Plot Area:Size", "Cluster Size",
                   "Color Area Size", "Tab Control Pane Dimension")
    _MARGIN, _COL_GAP, _ROW_GAP, _ITEM_GAP = 20, 40, 14, 20

    def _geometry(self, h) -> dict:
        pos, size = self._get(h, "GObject", ["Position", "Bounds"], op="geometry")
        return {"pos": pos, "size": size}

    def _page_list(self, page, what, vi) -> list[int]:
        """Controls or Decorations on a tab page (the property's name differs between LabVIEW versions)."""
        names = [p[1] for p in self.class_members("Page")["properties"]]
        prop = next((n for n in names if what in n and n.endswith("[]")), None)
        refs = (self._get(page, "Page", prop, op="page objects") or []) if prop else []
        self._reg(vi, *refs)
        return refs

    def _pages(self, tab, vi) -> list[dict]:
        """A tab control's pages: [{"h", "label"?, "pos", "size"}] (pos/size: the page area, panel coordinates)."""
        pages = self._get(tab, "TabControl", "Pages", op="pages") or []
        self._reg(vi, *pages)
        out = []
        for p, r in zip(pages, self._batch("GObject", ["Position", "Bounds"], pages, op="pages")):
            self._cls[p] = "Page"
            out.append({"h": p, "pos": r["Position"].value, "size": r["Bounds"].value})
        try:
            for d in out:
                d["label"] = self._get(d["h"], "Page", "Page Label", op="pages")
        except OpError:
            pass                                          # (no Page Label property in this LabVIEW)
        return out

    def resize_object(self, h: int, width: int | None = None, height: int | None = None) -> dict:
        """Resize a front-panel object (control, indicator, graph, decoration, free label) so that its whole box
        (with the label) is width x height pixels where LabVIEW allows it. Graphs: the plot area grows; numerics:
        only the width; arrays: element width and number of visible elements. Returns {"pos", "size"} after."""
        if not width and not height:
            raise OpError(None, "give width and/or height", "resize_object")
        cls = self._class(h)
        if cls == "Array":
            return self._resize_array(h, width, height)
        props = {p[1] for p in self.class_members(cls)["properties"]}
        prop = next((p for p in self._SIZE_PROPS if p in props), None)
        subs = {"Width": f"{prop}:Width", "Height": f"{prop}:Height"} if prop else \
            {"Width": "Text Width"} if "Text Width" in props else {}
        if not subs:
            raise OpError(None, f"{cls} objects have no size property that scripting can set (resize_object is "
                                "for front-panel objects)", "resize_object")
        for _ in range(3):           # the size property is the inner part: correct by the difference, twice
            cur = self._get(h, "GObject", "Bounds", op="resize_object")
            changed = False
            for i, (dim, target) in enumerate((("Width", width), ("Height", height))):
                if not target or dim not in subs or abs(cur[i] - int(target)) <= 1:
                    continue
                v = self._get(h, cls, subs[dim], op="resize_object")
                nv = max(4, int(v) + int(target) - cur[i])
                if nv != v:
                    self._set(h, cls, subs[dim], nv, op="resize_object")
                    changed = True
            if not changed:
                break
        out = self._geometry(h)
        if height and "Height" not in subs:
            out["note"] = f"{cls}: only the width can be set (the height follows the font)"
        return out

    def _resize_array(self, h, width, height) -> dict:
        el = self._get(h, "Array", "Array Element", op="resize_object")
        self._reg(self._vi(h), el)
        cur = self._get(h, "GObject", "Bounds", op="resize_object")
        ew, eh = self._get(el, "GObject", "Bounds", op="resize_object")
        if width and abs(cur[0] - int(width)) > 1:
            try:
                self.resize_object(el, width=max(10, ew + int(width) - cur[0]))
            except OpError:
                pass                                       # element kind without a size property
        if height and abs(cur[1] - int(height)) > 1:
            ndim = len(self._get(h, "Array", "Dimension Sizes", check=False, op="resize_object") or [1])
            rows = self._get(h, "Array", "Number of Rows", op="resize_object")
            vert = ndim > 1 or rows > 1 or self._get(h, "Array", "Number of Columns", op="resize_object") == 1
            if vert:
                n = max(1, rows + round((int(height) - cur[1]) / max(eh, 1)))
                self._set(h, "Array", "Number of Rows", n, op="resize_object")
        return self._geometry(h)

    def add_label(self, vi: int, text: str, x: int, y: int, size: int | None = None, bold: bool = False,
                  owner: int | None = None) -> dict:
        """A free label (text) on the front panel, e.g. a title or an instruction. size: font size (LabVIEW units:
        normal text is 13-15, a title ~24).
        owner: a tab control page to put it on. Returns {"h", "pos", "size"}."""
        self._as_vi(vi)
        p = self._get(vi, "VI", "Front Panel", op="add_label")
        self._reg(vi, p)
        try:
            try:
                style = self._style("Label (modern)")
            except OpError:
                style = self._style("Label (system)")
            t = self.S.new_object(p, style, int(x), int(y), 0, 0)
        except LVError as e:
            raise OpError(e.code, e.source, "add_label") from None
        self._reg(vi, t)
        self._remember(vi, t)
        self._cls[t] = "Text"
        self._set(t, "Text", "Text", str(text), op="add_label")
        for prop in ("Colors:BG Color", "Colors:FG Color", "Text Colors:BG Color"):   # no frame: transparent
            self._set(t, "Text", prop, 0x1000000, op="add_label")
        if size:
            self._set(t, "Text", "Font:Size", int(size), op="add_label")
        if bold:
            self._set(t, "Text", "Font:Bold", True, op="add_label")
        if owner is not None:
            self.move_object(t, x, y, owner)
        else:
            self._place(t, x, y)
        return {"h": t} | self._geometry(t)

    def decorations(self, vi: int, pages=()) -> list[dict]:
        """Front-panel decorations and free labels (also those on the given tab pages): [{"h", "class"
        (Decoration/Text), "text"?, "pos", "size", "page"?}]."""
        p = self._get(vi, "VI", "Front Panel", op="panel")
        self._reg(vi, p)
        decos = list(self._get(p, "Panel", "Decorations[]", op="panel") or [])
        page_of = {}
        for pg in pages:
            for d in self._page_list(pg, "Decorations", vi):
                page_of[d] = pg
                decos.append(d)
        self._reg(vi, *decos)
        out = []
        for d, r in zip(decos, self._batch("GObject", ["UID", "Class Name", "Position", "Bounds"], decos,
                                           op="panel")):
            if r["error"][0]:
                continue
            item = {"h": self._canon.get((vi, r["UID"].value), d), "class": r["Class Name"].value} | \
                ({"page": page_of[d]} if d in page_of else {})
            self._cls[d] = item["class"]
            if item["class"] == "Text":
                item["text"] = self._get(d, "Text", "Text", op="panel")
            out.append(item | {"pos": r["Position"].value, "size": r["Bounds"].value})
        return out

    @staticmethod
    def _name(it) -> str:
        return it.get("label") or it.get("text") or f"{it['class']} {it['h']}"

    @classmethod
    def panel_overlaps(cls, items) -> list[list[str]]:
        """Pairs of front-panel objects whose boxes overlap (decorations are meant to: not counted)."""
        rects = [(cls._name(i), i["pos"][0], i["pos"][1], i["pos"][0] + i["size"][0], i["pos"][1] + i["size"][1],
                  i.get("page")) for i in items if i.get("class") != "Decoration" and i.get("pos")]
        out = []
        for k, a in enumerate(rects):
            for b in rects[k + 1:]:
                if a[5] != b[5]:
                    continue                      # not on the same tab page / not both on the panel itself
                if a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]:
                    out.append([a[0], b[0]])
        return out

    def check_panel(self, vi: int) -> list[list[str]]:
        """Overlapping front-panel objects of a VI (see panel_overlaps)."""
        ctls = self.panel(vi, values=False)
        return self.panel_overlaps(ctls + self.decorations(vi, [pg["h"] for c in ctls for pg in c.get("pages", [])]))

    def arrange_panel(self, vi: int, rows: list | None = None, columns: list | None = None,
                      fit_window: bool = True, page: int | None = None) -> dict:
        """Lay out the front panel. Default (no rows/columns): free labels on top as titles, then controls in a
        column on the left, indicators in a column to their right, graphs/charts/tables (and other big
        indicators) and tab controls in a column on the right; each column in block-diagram order (top to bottom), Stop buttons last; tall
        columns wrap. rows: [[label, label], [label], ...] puts each list in a row, rows top to bottom; columns:
        the same with columns left to right. Items are control/indicator labels, free-label texts or handles;
        free labels not listed stay on top as titles, other objects not listed are laid out the default way after
        the listed ones. Decorations move only when listed.
        fit_window: size the panel window to the result (saved with the VI). page: lay out the objects on this
        tab control page instead (objects on pages are not moved otherwise). Returns {"objects": [{"label",
        "pos", "size"}], "window": [w, h], "overlaps"}."""
        self._as_vi(vi)
        if rows and columns:
            raise OpError(None, "give rows or columns, not both", "arrange_panel")
        ctls = self.panel(vi, values=False)
        pages = [pg["h"] for c in ctls for pg in c.get("pages", [])]
        if page is not None and page not in pages:
            uids = {self._get(pg, "GObject", "UID", op="arrange_panel"): pg for pg in pages}
            page = uids.get(self._get(page, "GObject", "UID", op="arrange_panel"))
            if page is None:
                raise OpError(None, "page is not a tab control page of this VI (see 'pages' in describe_vi)",
                              "arrange_panel")
        items = [i for i in ctls + self.decorations(vi, pages) if i.get("page") == page]
        by_h = {i["h"]: i for i in items}
        by_name = {}
        for i in items:
            if i.get("label") or i.get("text"):
                by_name.setdefault(self._name(i), i)
        by_uid = {}

        def pick(k):
            if isinstance(k, int):
                if k in by_h:
                    return by_h[k]
                if not by_uid:
                    for i in items:
                        by_uid[i.get("uid") or self._get(i["h"], "GObject", "UID", op="arrange_panel")] = i
                try:
                    u = self._get(k, "GObject", "UID", op="arrange_panel")
                except OpError:
                    u = None
                if u in by_uid:
                    return by_uid[u]
            elif str(k) in by_name:
                return by_name[str(k)]
            raise OpError(None, f"no front-panel object {k!r}; there are: {sorted(by_name)}", "arrange_panel")

        M, moves = self._MARGIN, []
        bx, by = self._get(page, "GObject", "Position", op="arrange_panel") if page is not None else (0, 0)

        def line(group, x, y, horizontal):
            """Put items one after another from (x, y); returns the (width, height) used."""
            cx, cy, w, h = x, y, 0, 0
            for it in group:
                moves.append((it, cx, cy))
                iw, ih = it["size"]
                if horizontal:
                    cx += iw + self._ITEM_GAP
                    w, h = cx - x - self._ITEM_GAP, max(h, ih)
                else:
                    cy += ih + self._ROW_GAP
                    w, h = max(w, iw), cy - y - self._ROW_GAP
            return w, h

        groups = [[pick(k) for k in ([g] if isinstance(g, (str, int)) else g)] for g in rows or columns or []]
        seen = set()
        for its in groups:
            for it in its:
                if it["h"] in seen:
                    raise OpError(None, f"{self._name(it)!r} is listed twice", "arrange_panel")
                seen.add(it["h"])
        x0, y0 = bx + M, by + M
        for t in [i for i in items if i["h"] not in seen and i["class"] == "Text"]:   # titles on top
            y0 += line([t], x0, y0, False)[1] + self._ROW_GAP
            seen.add(t["h"])
        if y0 > by + M:
            y0 += self._ROW_GAP
        for its in groups:
            if rows:
                y0 += line(its, bx + M, y0, True)[1] + self._ROW_GAP * 2
            else:
                x0 += line(its, x0, y0, False)[0] + self._COL_GAP
        rest = [i for i in items if i["h"] not in seen and "kind" in i]
        if rest:
            self._auto_layout(rest, x0, y0, line)
        for it, x, y in moves:
            if it["pos"] != [x, y]:
                self._set(it["h"], "GObject", "Position", [int(x), int(y)], op="arrange_panel")
                it["pos"] = [x, y]
        out = {"objects": [{"label": self._name(i), "pos": i["pos"], "size": i["size"]} for i in items]}
        if fit_window and page is None:
            out["window"] = self._fit_window(vi, items)
        if page is not None and items:
            out["tab_size"] = self._fit_tab(page, items, ctls)
        out["overlaps"] = self.panel_overlaps(items)
        return out

    def _auto_layout(self, ctls, x, y, line):
        terms = [c["terminal"] for c in ctls]
        tpos = {}
        for c, r in zip(ctls, self._batch("GObject", ["Position"], terms, op="arrange_panel")):
            tpos[c["h"]] = tuple(r["Position"].value) if not r["error"][0] else (1 << 30, 0)

        def order(c):
            stop = c["class"] == "Boolean" and "stop" in (c.get("label") or "").lower()
            return stop, tpos[c["h"]][1], tpos[c["h"]][0]

        def big(c):
            return c["class"] in self._BIG or c["size"][0] > 300 or c["size"][1] > 160

        cols = [[c for c in ctls if c["kind"] == "control" and c["class"] != "TabControl"],
                [c for c in ctls if c["kind"] == "indicator" and not big(c)],
                [c for c in ctls if c["kind"] == "indicator" and big(c) or c["class"] == "TabControl"]]
        max_h = 600
        for col in cols:
            col.sort(key=order)
            while col:                                   # wrap a tall column
                n, h = 0, 0
                while n < len(col) and (n == 0 or h + col[n]["size"][1] <= max_h):
                    h += col[n]["size"][1] + self._ROW_GAP
                    n += 1
                x += line(col[:n], x, y, False)[0] + self._COL_GAP
                col = col[n:]

    def _fit_tab(self, page, items, ctls) -> list[int]:
        """Grow/shrink the tab control so that its page area just holds the objects laid out on `page`."""
        tab = next(c for c in ctls if any(pg["h"] == page for pg in c.get("pages", [])))
        (px, py), (pw, ph) = self._get(page, "GObject", ["Position", "Bounds"], op="arrange_panel")
        x1 = max(i["pos"][0] + i["size"][0] for i in items) + self._MARGIN
        y1 = max(i["pos"][1] + i["size"][1] for i in items) + self._MARGIN
        tw, th = tab["size"]
        return self.resize_object(tab["h"], max(tw + x1 - px - pw, 120), max(th + y1 - py - ph, 80))["size"]

    def _fit_window(self, vi, items) -> list[int]:
        """Size the front-panel window to the objects (+ margin) and scroll it to them."""
        M = self._MARGIN
        if items:
            x0 = min(i["pos"][0] for i in items)
            y0 = min(i["pos"][1] for i in items)
            x1 = max(i["pos"][0] + i["size"][0] for i in items)
            y1 = max(i["pos"][1] + i["size"][1] for i in items)
        else:
            x0 = y0 = x1 = y1 = 0
        ox, oy = min(0, x0 - M), min(0, y0 - M)
        w, h = max(x1 + M - ox, 360), max(y1 + M - oy, 160)
        pb = self._get(vi, "VI", "Front Panel Window:Panel Bounds", op="fit window")
        left, top = (pb[0], pb[1]) if pb[2] > pb[0] else (100, 100)
        self._set(vi, "VI", "Front Panel Window:Panel Bounds", [left, top, left + w, top + h], op="fit window")
        try:
            p = self._get(vi, "VI", "Front Panel", op="fit window")
            self._reg(vi, p)
            panes = self._get(p, "Panel", "Panes[]", op="fit window") or []
            self._reg(vi, *panes)
            if panes:
                self._set(panes[0], "Pane", "Origin", [ox, oy], op="fit window")
        except OpError:
            pass
        return [w, h]

    # ---------------------------------------------------------------- values / running
    def _ctl_get(self, vi, label) -> Variant:
        w = self._wrap("meth", "VI", "Control Value:Get")
        r = self._run(w, {"reference": vi, "Control Name": label}, [w.item], op=f"get_value {label!r}")
        return r[w.item]

    def get_value(self, vi: int, label: str):
        """Value of a front-panel control/indicator (by label)."""
        self._as_vi(vi)
        return variant_py(self._ctl_get(vi, label))

    def set_value(self, vi: int, label: str, value, default: bool = False):
        """Set a front-panel control (by label); value converted to the control's type (enums by name,
        clusters from list or dict). default: also make it the default value (saved with the VI; LabVIEW does
        this for all controls at once: their current values become their defaults)."""
        self._as_vi(vi)
        cur = self._ctl_get(vi, label)
        try:
            v = cur.with_value(to_lv(cur.td, value))
        except (ValueError, TypeError, OverflowError) as e:
            raise OpError(None, f"cannot convert {value!r} to {type_str(cur.td)}: {e}", "set_value") from None
        w = self._wrap("meth", "VI", "Control Value:Set")
        self._run(w, {"reference": vi, "Control Name": label, "Value": v}, op=f"set_value {label!r}")
        if default:
            self._meth(vi, "VI", "Default Values:Make Current Default", op="set_value default")

    def panel(self, vi: int, values=True) -> list[dict]:
        """Front-panel controls/indicators, also those on tab control pages: [{"h", "label", "kind", "class",
        "type", "value", "terminal", "pos", "size", "pages"? (tab controls), "page"? (the page it is on)}]."""
        self._as_vi(vi)
        p = self._get(vi, "VI", "Front Panel", op="panel")
        self._reg(vi, p)
        todo = list(self._get(p, "Panel", "Controls[]", op="panel") or [])
        props = ["UID", "Class Name", "Indicator", "Label", "Terminal", "Value", "Position", "Bounds"]
        out, page_of = [], {}
        while todo:
            ctls, todo = todo, []
            for ctl, r in zip(ctls, self._batch("Control", props, ctls, op="panel")):
                lbl = r["Label"].value
                item = {"h": self._canon.get((vi, r["UID"].value), ctl), "uid": r["UID"].value,
                        "label": self._get(lbl, "Text", "Text") if lbl else "",
                        "kind": "indicator" if r["Indicator"].value else "control", "class": r["Class Name"].value,
                        "terminal": r["Terminal"].value, "pos": r["Position"].value, "size": r["Bounds"].value}
                if item["h"] in self._term_of:
                    item["terminal"] = self._term_of[item["h"]]
                if ctl in page_of:
                    item["page"] = page_of[ctl]
                if item["class"] == "TabControl":
                    item["pages"] = self._pages(ctl, vi)
                    for pg in item["pages"]:
                        for c in self._page_list(pg["h"], "Controls", vi):
                            page_of[c] = pg["h"]
                            todo.append(c)
                self._cls[ctl] = item["class"]
                self._cls[item["terminal"]] = "ControlTerminal"
                self._reg(vi, ctl, lbl, item["terminal"])
                v = r["Value"].value
                if isinstance(v, Variant) and not r["error"][0]:
                    item["type"] = type_str(v.td)
                    if values:
                        item["value"] = short(variant_py(v))
                else:
                    item["type"] = "?"
                out.append(item)
        return out

    def state(self, vi: int) -> str:
        """"bad" (broken), "idle", "running" or "running as subVI"."""
        self._as_vi(vi)
        return STATE.get(self._get(vi, "VI", "Execution:State"), "?")

    def run_vi(self, vi: int, inputs: dict | None = None, outputs: list | None = None,
               timeout: float = 10.0) -> dict:
        """Set controls, run the VI (as a top-level VI, automatic error handling off), wait until it stops,
        return {label: value} of `outputs` (default: all indicators). A VI still running after `timeout`
        seconds is aborted (OpError). A broken VI raises OpError with the error list."""
        self._as_vi(vi)
        self._set(vi, "VI", "Automatic Error Handling", False, op="run_vi")
        st = self._get(vi, "VI", "Execution:State")
        if st == 0:
            errs = self.errors(vi)
            raise OpError(1003, "VI is broken: " + "; ".join(e["message"] for e in errs["errors"]), "run_vi")
        if st in (2, 3):
            raise OpError(1000, "VI is already running", "run_vi")
        for k, v in (inputs or {}).items():
            self.set_value(vi, k, v)
        self._meth(vi, "VI", "Run VI", {"Wait Until Done": False, "Auto Dispose Reference": False}, op="run_vi")
        t0 = time.time()
        while True:
            st = self._get(vi, "VI", "Execution:State")
            if st not in (2, 3):
                break
            if time.time() - t0 > timeout:
                self._meth(vi, "VI", "Abort VI", op="run_vi abort")
                raise OpError(None, f"VI still running after {timeout} s: aborted", "run_vi")
            time.sleep(0.02)
        if outputs is None:
            outputs = [c["label"] for c in self.panel(vi, values=False) if c["kind"] == "indicator"]
        return {k: self.get_value(vi, k) for k in outputs}

    def errors(self, vi: int) -> dict:
        """{"broken": bool, "state", "errors": [{"message", "details"}]} (error list via the private VI method
        Get Errors when available)."""
        self._as_vi(vi)
        st = self._get(vi, "VI", "Execution:State")
        out = {"broken": st == 0, "state": STATE.get(st, st), "errors": []}
        if st == 0:
            try:
                r = self._meth(vi, "VI", "Get Errors", op="errors")
                det = r.get("Details") or []
                out["errors"] = [{"message": m, "details": det[i] if i < len(det) else ""}
                                 for i, m in enumerate(r.get("Errors") or [])]
            except OpError as e:
                out["errors"] = [{"message": "(error list not available in this LabVIEW)", "details": str(e)}]
        return out

    # ---------------------------------------------------------------- images
    def get_image(self, h: int, what: str = "diagram") -> bytes:
        """PNG bytes. h = VI handle with what "diagram" or "panel", or a node/structure handle (its image)."""
        is_vi = self._class(h) == "VI"
        if is_vi and what == "panel":
            r = self._meth(h, "VI", "Front Panel:Get Image", {"Visible Area Only": False, "Image Depth": 24},
                           op="get_image panel")
            return image_png(r["Image Data"])
        if is_vi:
            try:
                r = self._meth(h, "VI", "Block Diagram:Get Image Scaled",
                               {"Image Depth": 24, "Maximum Width": 0, "Maximum Height": 0}, op="get_image diagram")
                return image_png(r["Image Data"])
            except OpError as e:
                if e.code not in (1077, 1113):
                    raise
                return self._composite_image(h)
        r = self._meth(h, "Node", "Get Image", {"Image Depth": 24}, op="get_image node")
        return image_png(r["Image Data"])

    def _composite_image(self, vi) -> bytes:
        """Fallback diagram image (LabVIEW without BD Get Image Scaled): top-level nodes pasted at their
        positions (wires not drawn)."""
        from PIL import Image
        bd = self.block_diagram(vi)
        parts = []
        for o in self._get(bd, "Diagram", "Nodes[]"):
            self._reg(vi, o)
            pos = self._get(o, "GObject", "Position")
            try:
                png = self._meth(o, "Node", "Get Image", {"Image Depth": 24})["Image Data"]
            except OpError:
                continue
            parts.append((pos, Image.open(io.BytesIO(image_png(png)))))
        if not parts:
            return image_png([0, 24, [], [], [], [0, 0, 0, 0]])
        x0 = min(p[0] for p, _ in parts); y0 = min(p[1] for p, _ in parts)
        x1 = max(p[0] + im.width for p, im in parts); y1 = max(p[1] + im.height for p, im in parts)
        canvas = Image.new("RGB", (x1 - x0 + 10, y1 - y0 + 10), "white")
        for (x, y), im in parts:
            canvas.paste(im, (x - x0 + 5, y - y0 + 5))
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        return buf.getvalue()

    # ---------------------------------------------------------------- describe
    def describe_vi(self, vi: int, values: bool = True) -> dict:
        """Compact description of a VI for an LLM:
        {"vi", "name", "path", "state", "panel": [{"h", "label", "kind", "class", "type", "value", "terminal",
         "pos", "size", "pages"?: [{"h", "label", "pos", "size"}]}], "decorations"?: [{"h", "class", "text"?, "pos", "size"}], "diagram": h,
         "nodes": [{"h", "class", "name"?, "label"?, "value"?, "pos", "size", "in" (diagram h),
                    "terms": [[name, dir, type, wired]], "diagrams"?, "tunnels"?, "shift_registers"?}],
         "wires": [{"h", "from": [h, term], "to": [[h, term], ...]}], "errors"?}
        Terminal names are what wire()/create_* accept; wire ends are node, tunnel or shift-register handles.
        Positions are absolute block-diagram coordinates (as add_node/move_object take them)."""
        self._as_vi(vi)
        info = self.vi_info(vi)
        out = {"vi": vi, **info}
        out["panel"] = self.panel(vi, values)
        decos = self.decorations(vi, [pg["h"] for c in out["panel"] for pg in c.get("pages", [])])
        if decos:
            out["decorations"] = decos
        ctx = {"vi": vi, "values": values, "nodes": [], "wires": [], "groups": [], "remap": {},
               "ctl_by_term": {c["terminal"]: c for c in out["panel"]},
               "ctl_by_uid": {c.pop("uid"): c for c in out["panel"]}}
        bd = self.block_diagram(vi)
        out["diagram"] = bd
        self._walk(bd, ctx)
        # all terminals of the VI in one batch, then name them per object
        refs = [t for g in ctx["groups"] for t in g[1]]
        info_all = self._term_info(refs)
        termmap, k = {}, 0
        for owner, trefs, names, target in ctx["groups"]:
            rows = self._term_rows(trefs, names, info_all[k:k + len(trefs)], subvi=self._is_subvi(owner))
            k += len(trefs)
            self._reg(vi, *trefs)
            target["terms"] = [[t["name"], t["dir"], t["type"], t["wired"]] for t in rows]
            for t in rows:
                termmap[t["h"]] = (owner, t["name"], t["dir"])
                termmap[("uid", t["uid"])] = (owner, t["name"], t["dir"])
        for n in ctx["nodes"]:
            for srd in n.get("shift_registers", []):
                for key in ("right_terms", "left_terms"):
                    if key in srd:
                        srd[key] = srd[key].get("terms", [])
        out["nodes"] = ctx["nodes"]
        out["wires"] = self._wires(vi, ctx["wires"], termmap)
        rm = ctx["remap"]
        for w in out["wires"]:                       # wire ends by the handles the nodes are reported with
            if isinstance(w.get("from"), list) and w["from"]:
                w["from"][0] = rm.get(w["from"][0], w["from"][0])
            for end in w.get("to") or []:
                if isinstance(end, list) and end:
                    end[0] = rm.get(end[0], end[0])
        if info["state"] == "bad":
            out["errors"] = [e["message"] for e in self.errors(vi)["errors"]]
        return out

    _STRUCTS = ("WhileLoop", "ForLoop", "TimedLoop", "CaseStructure", "FlatSequence", "StackedSequence",
                "EventStructure", "DisableStructure", "ConditionalDisableStructure", "InPlaceElementStructure")

    def _walk(self, d, ctx):
        vi = ctx["vi"]
        objs, nodes, wires = self._get(d, "Diagram", ["All Objects[]", "Nodes[]", "Wires[]"], op="describe")
        ctx["wires"].extend(wires)
        self._reg(vi, *objs, *wires)
        gob = dict(zip(objs, self._batch("GObject", ["UID", "Class Name", "Position", "Bounds"], objs, op="describe")))
        nodes = [n for n in nodes if n in gob] if all(n in gob for n in nodes) else nodes
        nb = dict(zip(nodes, self._batch("Node", ["Terminals[]", "Style"], nodes, op="describe")))
        for o in objs:
            r = gob[o]
            if r["error"][0] and not r["Class Name"].value:
                continue
            cls = r["Class Name"].value
            self._cls[o] = cls
            if cls in ("Wire", "Terminal") or (cls.endswith("Label") and cls != "Label"):
                continue                     # wires: separately; loop i/N/stop terminals: with their loop
            c0 = self._canon.get((vi, r["UID"].value))
            if c0 and c0 != o:
                ctx["remap"][o] = c0
            n = {"h": c0 or o, "class": cls, "pos": r["Position"].value, "size": r["Bounds"].value, "in": d}
            ctx["nodes"].append(n)
            try:
                if cls.endswith("ControlTerminal"):
                    c = ctx["ctl_by_term"].get(o)
                    if c is None:
                        ctl = self._get(o, "ControlTerminal", "Control")
                        c = ctx["ctl_by_uid"].get(self._get(ctl, "GObject", "UID"))
                    if c:
                        n["label"], n["kind"], n["control"] = c["label"], c["kind"], c["h"]
                    ctx["groups"].append((o, [o], ["value"], n))
                elif "Constant" in cls:
                    kv = self._getv(o, "Constant", ["Terminal", "Value"], check=False, op="describe")
                    if ctx["values"] and not kv["error out"][0]:
                        n["value"] = short(variant_py(kv["Value"]))
                    if kv["Terminal"].value:
                        ctx["groups"].append((o, [kv["Terminal"].value], ["value"], n))
                elif o in nb and not (cls in self._STRUCTS or "Structure" in cls or "Sequence" in cls):
                    b = nb[o]
                    n["name"] = b["Style"].value
                    if cls == "SubVI":
                        try:
                            n["name"] = self._get(o, "SubVI", "VI Name")
                        except OpError:
                            pass
                    trefs = [t for t in (b["Terminals[]"].value or []) if t]
                    ctx["groups"].append((o, trefs, None, n))
                else:
                    if o in nb:
                        n["name"] = nb[o]["Style"].value
                    trefs, names = self._term_refs(o)
                    ctx["groups"].append((o, trefs, names, n))
            except OpError as e:
                if e.code not in (1058, 1077, 1055) and e.code is not None:
                    raise
            if cls in self._STRUCTS or "Structure" in cls or cls == "StackedSequence":
                self._walk_struct(o, cls, n, ctx)

    def _walk_struct(self, s, cls, n, ctx):
        vi = ctx["vi"]
        try:
            n["diagrams"] = self._diagrams(s, cls)
        except OpError:
            n["diagrams"] = []
        tuns = []
        if cls != "FlatSequence":
            try:
                tuns += self._get(s, "Structure", "Tunnels[]")
            except OpError:
                pass
        if cls in ("WhileLoop", "ForLoop", "TimedLoop"):
            n["shift_registers"] = []
            for sr in self._get(s, "Loop", "Shift Registers[]"):
                left = self._get(sr, "RightShiftRegister", "Left Registers[]")
                self._reg(vi, sr, *left)
                self._cls[sr] = "RightShiftRegister"
                srd = {"right": sr, "left": left[0] if len(left) == 1 else left}
                n["shift_registers"].append(srd)
                for h, side in [(sr, "right_terms")] + [(x, "left_terms") for x in left[:1]]:
                    self._cls.setdefault(h, "LeftShiftRegister" if side == "left_terms" else "RightShiftRegister")
                    trefs, names = self._term_refs(h)
                    holder = {}
                    ctx["groups"].append((h, trefs, names, holder))
                    srd[side] = holder        # filled with {"terms": ...} after the batch
            if not n["shift_registers"]:
                del n["shift_registers"]
        tl = []
        for t in tuns:
            self._reg(vi, t)
            c = self._cls.get(t) or self._get(t, "GObject", "Class Name")
            self._cls[t] = c
            if "ShiftRegister" in c:
                continue
            td = {"h": t, "class": c}
            trefs, names = self._term_refs(t)
            ctx["groups"].append((t, trefs, names, td))
            tl.append(td)
        if tl:
            n["tunnels"] = tl
        for dd in n["diagrams"]:
            self._walk(dd["h"], ctx)

    def _wires(self, vi, wires, termmap) -> list[dict]:
        wires = list(dict.fromkeys(wires))
        out = []
        for w, r in zip(wires, self._batch("Wire", ["Terminals[]"], wires, op="describe wires")):
            ts = [t for t in (r["Terminals[]"].value or []) if t]
            src, dst = None, []
            for t in ts:
                self._reg(vi, t)
                end = termmap.get(t)
                if end is None:
                    q = self._getv(t, "Terminal", ["UID", "Is Source?"], check=False, op="wire end")
                    end = termmap.get(("uid", q["UID"].value)) or (t, "?", "out" if q["Is Source?"].value else "in")
                if end[2] == "out":
                    src = [end[0], end[1]]
                else:
                    dst.append([end[0], end[1]])
            out.append({"h": w, "from": src, "to": dst})
        return out
