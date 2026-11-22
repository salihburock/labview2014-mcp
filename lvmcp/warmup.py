"""Generate, once, the wrapper VIs a typical session needs (list: lvmcp/warm.json, recorded from the test suites
and real build sessions), so the first AI session does not wait for them. Wrappers already in the cache are
skipped; ones this LabVIEW cannot make (e.g. properties of newer versions) are left out.

  python -m lvmcp.warmup  (same options as python -m lvmcp.selftest; selftest runs it too)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from lvmcp.lvclient import LVError

SPECS = Path(__file__).with_name("warm.json")


def _name(lv, s) -> str:
    if s["kind"] == "batch":
        return lv.batch_wrapper_name(s["class"], list(s["item"]))
    if s["kind"] == "custom":
        return lv.S.prefix + lv.SUBVI_WRAPPER
    return lv.S.wrapper_name(s["kind"], s["class"], s["item"], s["write"], s["skip"])


def missing(lv) -> list[dict]:
    """Specs whose wrapper is not in the cache yet."""
    lv.S.reload_index()
    return [s for s in json.loads(SPECS.read_text()) if _name(lv, s) not in lv.S.index]


def warm(lv, progress=print) -> tuple[int, int]:
    """Generate the missing wrappers. Returns (generated or present, failed)."""
    todo = missing(lv)
    ok = bad = 0
    t0 = time.time()
    for i, s in enumerate(todo, 1):
        try:
            if s["kind"] == "batch":
                lv._batch_wrapper(s["class"], list(s["item"]))
            elif s["kind"] == "custom":
                lv._subvi_wrapper()
            else:
                lv.S.wrapper(s["kind"], s["class"], s["item"], s["write"], tuple(s["skip"]))
            ok += 1
        except (LVError, RuntimeError, KeyError, ValueError):
            bad += 1
        finally:
            lv.close()          # let other clients of this server VI in between (it serves one at a time)
        if progress and (i % 10 == 0 or i == len(todo)):
            progress(f"helper VIs: {i}/{len(todo)} ({time.time() - t0:.0f} s)")
    return ok, bad


def warm_palette(lv, progress=print) -> int:
    """Build (or load) the palette table that find_object uses. Returns its item count (0: not available)."""
    try:
        from lvmcp.palette import Palette
        P = Palette(lv.S)
        if P.cache_dir and not any(P.cache_dir.glob("palette_*.json")):
            progress and progress("reading the LabVIEW palettes (first run only)")
        return len(P.load().get("items", []))
    except Exception as e:  # noqa: BLE001  (find_object falls back to the style table)
        progress and progress(f"palette table not available: {e}")
        return 0


def main(argv=None) -> int:
    from lvmcp.selftest import connect
    lv, _ = connect(argv)
    if lv is None:
        return 1
    ok, bad = warm(lv)
    print(f"{ok} helper VIs ready" + (f", {bad} not available in this LabVIEW" if bad else ""))
    print(f"palette table: {warm_palette(lv)} items")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
