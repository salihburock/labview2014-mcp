"""Self-test of an lvmcp installation: talks to lvmcp_server.vi and builds, runs and checks a small VI.

  python -m lvmcp.selftest [--host 127.0.0.1] [--port 5577] [--token ""] [--boot-dir ...] [--gen-dir ...]
                            [--cache-dir ...] [--save <path for the test VI, as LabVIEW sees it>]

Same defaults as lvmcp/mcp_server.py (boot wrappers in <package>/labview/boot, generated wrappers in
<package>/build/gen). The first run also generates the helper VIs a typical session needs (lvmcp.warmup,
about a minute), so the AI client does not wait for them later.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lvmcp.ops import LabVIEW  # noqa: E402


def _args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5577)
    ap.add_argument("--token", default="")
    ap.add_argument("--boot-dir", default=str(ROOT / "labview" / "boot"))
    ap.add_argument("--gen-dir", default=str(ROOT / "build" / "gen"))
    ap.add_argument("--cache-dir", default=None, help="local folder (default: --gen-dir)")
    ap.add_argument("--prefix", default="lvmcp_")
    ap.add_argument("--boot-prefix", default="", help="file-name prefix of (renamed copies of) the boot wrappers")
    ap.add_argument("--save", default="", help="also save the test VI here (path as LabVIEW sees it)")
    ap.add_argument("--autostart", choices=("auto", "on", "off"), default="auto",
                    help="open lvmcp_server.vi in LabVIEW if it is not running (auto = on Windows, local server)")
    ap.add_argument("--labview", default="", help="LabVIEW executable for --autostart (default: .vi association)")
    ap.add_argument("--no-warmup", action="store_true", help="do not prepare the helper VIs of a typical session")
    return ap.parse_args(argv)


def connect(argv=None):
    """(LabVIEW ops object, status dict), or (None, None) after printing why the server is not reachable."""
    a = _args(argv)
    cache = a.cache_dir or a.gen_dir
    Path(cache).mkdir(parents=True, exist_ok=True)
    from lvmcp import autostart
    if not autostart.listening(a.host, a.port) and autostart.enabled(a.autostart, a.host):
        print(f"lvmcp_server.vi is not running: opening it in LabVIEW ({autostart.SERVER_VI}) ...", flush=True)
        autostart.start(a.host, a.port, autostart.SERVER_VI, a.labview)
    try:
        lv = LabVIEW(a.host, a.port, a.token, a.boot_dir, a.gen_dir, cache, prefix=a.prefix, boot_prefix=a.boot_prefix)
        return lv, lv.status()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: cannot reach lvmcp_server.vi at {a.host}:{a.port}: {e}\n"
              "Open labview/lvmcp_server.vi in LabVIEW (it starts when opened) and try again.")
        return None, None


def main(argv=None) -> int:
    a = _args(argv)
    t0 = time.time()

    lv = None

    def step(msg):
        print(f"[{time.time() - t0:5.1f} s] {msg}", flush=True)
        if lv is not None:
            lv.close()          # release the server VI between steps: an AI session may be using it too

    lv, st = connect(argv)
    if lv is None:
        return 1
    step(f"LabVIEW {st.get('version')} {st.get('app_kind', '')} on {st.get('os', '')}, "
         f"{st.get('styles')} objects")
    try:
        vi = lv.new_vi()
        step("new VI")
        n = lv.add_node(vi, "Multiply", 250, 120)
        lv.create_constant(n["h"], "x", 3.0)
        lv.create_constant(n["h"], "y", 4.0)
        lv.create_indicator(n["h"], "x*y", "result")
        step("Multiply with constants 3 and 4, indicator 'result'")
        err = lv.errors(vi)
        if err.get("broken"):
            print(f"FAIL: the test VI is broken: {err}")
            return 1
        r = lv.run_vi(vi, {}, ["result"], timeout=20)
        step(f"run: result = {r['result']}")
        png = lv.get_image(vi, "diagram")
        step(f"diagram image: {len(png)} bytes PNG")
        if a.save:
            lv.save_vi(vi, a.save)
            step(f"saved {a.save}")
        lv.close_vi(vi)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}")
        return 1
    if r["result"] != 12:
        print(f"FAIL: expected 12, got {r['result']}")
        return 1
    if not a.no_warmup:
        from lvmcp.warmup import missing, warm, warm_palette
        if missing(lv):
            step("preparing the helper VIs of a typical session (first run only, about a minute)")
            ok, bad = warm(lv, progress=step)
            step(f"{ok} helper VIs ready" + (f", {bad} not available in this LabVIEW" if bad else ""))
        step(f"palette table (find_object): {warm_palette(lv, progress=step)} items")
    print("OK: lvmcp works with this LabVIEW.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
