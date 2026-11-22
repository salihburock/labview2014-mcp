#!/usr/bin/env python3
"""Milestone 1 proof: build a VI by scripting through lvmcp_server.vi, run it and read the result.

  new VI -> drop "Multiply" by name -> constants 3 and 4 on x and y -> indicator on x*y -> Run VI -> read x*y

Usage (LabVIEW must be running lvmcp_server.vi):
  python3 dev/demo_multiply.py --boot <boot dir as LabVIEW sees it> --gen <folder for generated VIs> \
      [--cache <local cache dir>] [--port 5577] [--save <path for the demo VI>]
Defaults are for the headless LabVIEW 2022 on this Linux machine.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp.lvclient import DBL, Client, make_variant  # noqa: E402
from lvmcp.scripting import Scripting  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5577)
ap.add_argument("--token", default="")
ap.add_argument("--boot", default=str(ROOT / "build/lv2022/boot"))
ap.add_argument("--gen", default=str(ROOT / "build/gen2022e"))
ap.add_argument("--cache", default=None)
ap.add_argument("--save", default=str(ROOT / "build/demo_multiply.vi"))
ap.add_argument("-x", type=float, default=3.0)
ap.add_argument("-y", type=float, default=4.0)
a = ap.parse_args()

t0 = time.time()
c = Client(a.host, a.port, a.token).connect()
S = Scripting(c, a.boot, a.gen, a.cache)
styles = S.styles()
print(f"style table: {len(styles)} objects, Multiply = {styles['Multiply']}")

vi = S.new_vi()
bd = S.block_diagram(vi)
mul = S.new_object(bd, styles["Multiply"], 250, 120)
terms = {S.term_name(t): t for t in S.node_info(mul)[0]}
print("Multiply terminals:", list(terms))
S.create_constant(terms["x"], make_variant(DBL, a.x, "x"))
S.create_constant(terms["y"], make_variant(DBL, a.y, "y"))
S.create_indicator(terms["x*y"])

S.wrapper("meth", "VI", "Run VI").run({"reference": vi, "Wait Until Done": True, "Auto Dispose Reference": False})
get = S.wrapper("meth", "VI", "Control Value:Get")
result = get.run({"reference": vi, "Control Name": "x*y"}, [get.item])[get.item]
print(f"x*y = {result}   ({time.time() - t0:.1f} s)")
if a.save:
    S.save(vi, a.save)
    print("saved", a.save)
S.close(vi)
assert result == a.x * a.y
