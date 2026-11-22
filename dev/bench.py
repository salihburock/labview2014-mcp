#!/usr/bin/env python3
"""Time a typical build session through ops.LabVIEW: server round trips and seconds per operation.

  python3 dev/bench.py [--port 5580] [--gen DIR] [--prefix lvB_] [--map HOSTDIR=LVDIR]

Session: status, new VI, For Loop summing 1..N with a shift register, Multiply with constants, controls and
indicators, describe, run, diagram image, close. Run it twice to see the warm-cache numbers.
"""
import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp import lvclient  # noqa: E402
from lvmcp.ops import LabVIEW, private_boot_copy  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5580)
ap.add_argument("--token", default="")
ap.add_argument("--gen", default=str(ROOT / "build/genB"))
ap.add_argument("--prefix", default="lvB_")
ap.add_argument("--boot-src", default=str(ROOT / "build/lv2022/boot"))
ap.add_argument("--map", default="", help="HOSTDIR=LVDIR (LabVIEW in a VM sees HOSTDIR as LVDIR)")
a = ap.parse_args()

host_dir, _, lv_dir = a.map.partition("=")


def lvp(p):
    p = str(p)
    if host_dir and p.startswith(host_dir):
        return lv_dir + p[len(host_dir):].replace("/", "\\")
    return p


calls = [0]
_raw = lvclient.Client.raw


def raw(self, *args, **kw):
    calls[0] += 1
    return _raw(self, *args, **kw)


lvclient.Client.raw = raw
boot = private_boot_copy(a.boot_src, Path(a.gen) / "boot", a.prefix)
t0 = time.time()
lv = LabVIEW(a.host, a.port, a.token, lvp(boot), lvp(a.gen), a.gen, prefix=a.prefix, boot_prefix=a.prefix)
stats = defaultdict(lambda: [0, 0, 0.0])


def op(name, f, *args, **kw):
    c, t = calls[0], time.time()
    r = f(*args, **kw)
    s = stats[name]
    s[0] += 1
    s[1] += calls[0] - c
    s[2] += time.time() - t
    return r


op("status", lv.status)
vi = op("new_vi", lv.new_vi)
try:
    loop = op("add_structure", lv.add_structure, vi, "For Loop", 150, 40, 300, 200)
    inner = loop["diagrams"][0]["h"]
    ctl = op("create_control", lv.create_control, loop["h"], "N")
    op("set_label", lv.set_label, ctl["h"], "N")
    sr = op("add_shift_register", lv.add_shift_register, loop["h"])
    add = op("add_node", lv.add_node, inner, "Add", 120, 80)
    inc = op("add_node", lv.add_node, inner, "Increment", 40, 80)
    op("wire", lv.wire, loop["h"], "i", inc["h"], "x")
    op("wire", lv.wire, inc["h"], "x+1", add["h"], "x")
    op("wire", lv.wire, sr["left"]["h"], "inside", add["h"], "y")
    op("wire", lv.wire, add["h"], "x+y", sr["right"]["h"], "inside")
    op("create_constant", lv.create_constant, sr["left"]["h"], "outside")
    ind = op("create_indicator", lv.create_indicator, sr["right"]["h"], "outside")
    op("set_label", lv.set_label, ind["h"], "Sum")
    mul = op("add_node", lv.add_node, vi, "Multiply", 150, 300)
    op("create_constant", lv.create_constant, mul["h"], "x")
    op("create_constant", lv.create_constant, mul["h"], "y")
    p = op("create_indicator", lv.create_indicator, mul["h"], "x*y")
    op("set_label", lv.set_label, p["h"], "product")
    e = op("errors", lv.errors, vi)
    r = op("run_vi", lv.run_vi, vi, {"N": 10}, ["Sum", "product"])
    op("describe_vi", lv.describe_vi, vi)
    op("get_image", lv.get_image, vi, "diagram")
finally:
    op("close_vi", lv.close_vi, vi)
total = time.time() - t0
print(f"broken={e['broken']} result={r}")
print(f"{'op':18} {'n':>3} {'calls':>6} {'s':>7} {'s/op':>6}")
for k, (n, c, s) in sorted(stats.items(), key=lambda x: -x[1][2]):
    print(f"{k:18} {n:3d} {c:6d} {s:7.2f} {s / n:6.2f}")
print(f"total {total:.2f} s, {calls[0]} server calls, {total / max(calls[0], 1) * 1000:.0f} ms/call")
