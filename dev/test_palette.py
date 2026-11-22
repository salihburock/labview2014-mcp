#!/usr/bin/env python3
"""Build the palette table (lvmcp/palette.py) through a running lvmcp server and show some lookups.

  python3 dev/test_palette.py [--port 5581] [--rebuild] [--terminals N|all] [query ...]

Defaults: the headless LabVIEW 2022 on this machine, server copy on port 5581, wrapper cache build/genB
(prefix lvB_). With --terminals, N palette items (or all) are dropped into scratch VIs (closed without saving)
to record their terminals; the table is cached, so later runs only scan what is missing.
"""
import argparse
import collections
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp.lvclient import Client  # noqa: E402
from lvmcp.palette import Palette, describe  # noqa: E402
from lvmcp.scripting import Scripting  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5581)
ap.add_argument("--token", default="")
ap.add_argument("--boot", default=str(ROOT / "build/lv2022/boot"))
ap.add_argument("--gen", default=str(ROOT / "build/genB"))
ap.add_argument("--cache", default=str(ROOT / "build/genB"))
ap.add_argument("--prefix", default="lvB_")
ap.add_argument("--rebuild", action="store_true")
ap.add_argument("--terminals", default="40", help="number of items to scan for terminals, 'all' or 0")
ap.add_argument("queries", nargs="*")
a = ap.parse_args()

c = Client(a.host, a.port, a.token).connect()
S = Scripting(c, a.boot, a.gen, a.cache, prefix=a.prefix)
P = Palette(S)

t0 = time.time()
tab = P.load(rebuild=a.rebuild)
items = tab["items"]
print(f"LabVIEW {tab['labview']['version']} in {tab['labview']['dir']}: table {P.file.name} "
      f"({len(items)} items, {len(tab['palettes'])} palettes), built in {tab['labview']['build_seconds']} s, "
      f"loaded in {time.time() - t0:.1f} s")
if P.fs.cache:
    print(f"LabVIEW file operations for the build: {len(P.fs.cache)} (reads + folder listings)")
print("kinds:", dict(collections.Counter(i["kind"] for i in items)))
print("flags:", dict(collections.Counter(f for i in items for f in i["flags"])))
print("with terminals:", sum(1 for i in items if "terminals" in i))
print("on a palette:", sum(1 for i in items if i.get("palette")),
      " shortcuts:", len(tab["shortcuts"]), " missing palette files:", len(tab["missing"]),
      dict(collections.Counter(m["why"] for m in tab["missing"])))

if a.terminals != "0":
    todo = [i for i in items if i.get("palette") and "terminals" not in i]
    if a.terminals != "all":
        # a sample: the first prims and VIs of the Programming palette
        prims = [i for i in todo if i["kind"] == "prim"]
        vis = [i for i in todo if i["kind"] != "prim"]
        n = int(a.terminals)
        todo = prims[: n - n // 4] + vis[: n // 4]
    t1 = time.time()

    def prog(k, n, name):
        if k % 50 == 0 or k == n:
            print(f"  terminals {k}/{n} {time.time() - t1:.0f} s  {name}", flush=True)
    done, failed = P.scan_terminals(todo, progress=prog, retry=True)
    print(f"terminal scan: {done} ok, {failed} failed, {time.time() - t1:.1f} s")
    print("with terminals:", sum(1 for i in items if "terminals" in i),
          " errors:", collections.Counter(i["terminals_error"][:40] for i in items if "terminals_error" in i))

queries = a.queries or ["Multiply", "add", "x", "While Loop", "Open/Create/Replace File", "Wait (ms)",
                        "Build Array", "ba", "Index Array", "Bundle By Name", "mean", "Read Key", "Simulate Signal",
                        "niUSRP", "Open Session", "Fetch Rx Data"]
for q in queries:
    hits = P.find(q, 3)
    print(f"\n{q!r}:" + ("  (no match)" if not hits else ""))
    for k, (score, it) in enumerate(hits):
        print(f"  {score:5.1f}  {describe(it)}")
        if k == 0 and score >= 90:
            if "terminals" not in it and "dialog" not in it["flags"]:
                P.terminals(it)
            for t in it.get("terminals") or []:
                print(f"         {t['dir']:3} {t['name']!r}: {t.get('type')}")
            if it.get("terminals_error"):
                print("         terminals:", it["terminals_error"][:100])
P.save()
