#!/usr/bin/env python3
"""Exercise lvmcp/ops.py against a running lvmcp server (default: port 5580, the headless LabVIEW 2022).

Builds VIs only by scripting, runs them, reads results, takes images, saves (2022 + 14.0 copies) under
build/testA. Every VI saved here is named lvA_* (VI names must be unique in the LabVIEW instance).

  python3 dev/test_ops.py [--port 5580] [--out build/testA]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp.ops import LabVIEW, OpError, private_boot_copy  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5580)
ap.add_argument("--token", default="")
ap.add_argument("--boot", default=str(ROOT / "build/lv2022/boot"))
ap.add_argument("--gen", default=str(ROOT / "build/genA"))
ap.add_argument("--cache", default=str(ROOT / "build/genA"))
ap.add_argument("--prefix", default="lvA_")
ap.add_argument("--out", default=str(ROOT / "build/testA"))
ap.add_argument("--vilib", default="/usr/local/natinst/LabVIEW-2022-64/vi.lib")
ap.add_argument("--map", default="", help="HOSTDIR=LVDIR: LabVIEW sees local HOSTDIR as LVDIR (e.g. a VM share)")
a = ap.parse_args()

OUT = Path(a.out)


def lvp(p) -> str:
    """Local path -> path as the LabVIEW machine sees it (--map)."""
    p = str(p)
    if a.map:
        host, lvdir = a.map.split("=", 1)
        if p.startswith(host):
            return lvdir + p[len(host):].replace("/", "\\")
    return p

(OUT / "lv14").mkdir(parents=True, exist_ok=True)
# own renamed copy of the boot wrappers: other clients use the same LabVIEW through other servers
boot = private_boot_copy(a.boot, Path(a.gen) / "boot", a.prefix)
lv = LabVIEW(a.host, a.port, a.token, lvp(boot), lvp(a.gen), a.cache, prefix=a.prefix, boot_prefix=a.prefix)
T0 = time.time()
ok = []


def check(name, cond, info=""):
    print(f"[{'ok' if cond else 'FAIL'}] {name} {info}")
    ok.append(bool(cond))


def step(msg):
    print(f"--- {msg} ({time.time() - T0:.1f} s)")


step("status")
st = lv.status()
print(st)
check("status reachable", st["reachable"] and st["version"])

# ------------------------------------------------------------------ 1. subVI: lvA_t_add.vi (a, b -> a+b)
step("subVI with connector pane")
sub = lv.new_vi()
n = lv.add_node(sub, "Add", 200, 100)
lv.create_control(n["h"], "x", "a")
lv.create_control(n["h"], "y", "b")
lv.create_indicator(n["h"], "x+y", "a+b")
print(lv.set_connector(sub, ["a", "b"], ["a+b"]))
sub_path = lvp(OUT / f"{a.prefix}t_add.vi")
lv.save_vi(sub, sub_path)
lv.close_vi(sub)

# ------------------------------------------------------------------ 2. main VI
step("main VI: while loop with Add, constants, indicator, shift register")
vi = lv.new_vi()
loop = lv.add_structure(vi, "while", 150, 40, 420, 230)
check("while loop has i/stop", [t[0] for t in loop["terms"]] == ["i", "stop"], loop["terms"])
inner = loop["diagrams"][0]["h"]
add = lv.add_node(inner, "Add", 260, 90)
two = lv.add_node(vi, "Numeric Constant", 40, 120)               # outside the loop -> tunnel
lv.set_constant(two["h"], 2)
lv.wire(two["h"], None, add["h"], "x")
lv.create_constant(add["h"], "y", 10, type="I32")
lv.create_indicator(add["h"], "x+y", label="result")
lv.create_constant(loop["h"], "stop", False)                       # never stops by itself -> errors / timeout test

step("broken / errors")
e = lv.errors(vi)
check("not broken yet", not e["broken"], e)

step("run with timeout (loop never stops: must be aborted)")
try:
    lv.run_vi(vi, timeout=1.0)
    check("timeout abort", False)
except OpError as ex:
    check("timeout abort", "aborted" in str(ex), str(ex))
check("idle after abort", lv.state(vi) == "idle", lv.state(vi))

step("stop after 5 iterations: i >= 4 -> stop, shift register counts")
d0 = lv.describe_vi(vi)
stop_const = [x for x in d0["nodes"] if x["class"] == "BooleanConstant"][0]["h"]
lv.delete_object(stop_const)
ge = lv.add_node(inner, "Greater Or Equal?", 300, 180)
lv.wire(loop["h"], "i", ge["h"], "x")
lv.create_constant(ge["h"], "y", 4, type="I32")
lv.wire(ge["h"], "x >= y?", loop["h"], "stop")
sr = lv.add_shift_register(loop["h"])
inc = lv.add_node(inner, "Increment", 200, 40)
lv.wire(sr["left"]["h"], "inside", inc["h"], "x")
lv.wire(inc["h"], "x+1", sr["right"]["h"], "inside")
lv.create_constant(sr["left"]["h"], "outside", 0, type="I32")
lv.create_indicator(sr["right"]["h"], "outside", label="count")
e = lv.errors(vi)
check("loop VI not broken", not e["broken"], e)

step("case structure with a bool control, Multiply in the True case")
cs = lv.add_structure(vi, "case", 150, 320, 300, 160)
check("case frames", [x.get("name") for x in cs["diagrams"]] == ["False", "True"], cs["diagrams"])
sel = lv.add_node(vi, "Numeric Constant", 40, 380)   # replaced below by a control
lv.delete_object(sel["h"])
ctl = lv.create_control(cs["h"], "selector", label="use mul")
true_d = cs["diagrams"][1]["h"]
mul = lv.add_node(true_d, "Multiply", 120, 60)
lv.create_constant(mul["h"], "x", 2.5)
lv.create_constant(mul["h"], "y", 4)
lv.create_indicator(mul["h"], "x*y", label="product")

step("subVI from file + vi.lib subVI")
sv = lv.add_subvi(vi, sub_path, 650, 60)
check("subVI terminals", sorted(t[0] for t in sv["terms"]) == ["a", "a+b", "b"], sv["terms"])
lv.create_constant(sv["h"], "a", 1.25)
lv.create_constant(sv["h"], "b", 2)
lv.create_indicator(sv["h"], "a+b", label="sum")
tw_path = f"{a.vilib}/Utility/error.llb/Trim Whitespace.vi"
tw = lv.add_subvi(vi, tw_path, 650, 200)
lv.create_control(tw["h"], "string", label="text")
lv.create_indicator(tw["h"], "trimmed string", label="trimmed")

step("array constant + Array Size, Index Array")
asz = lv.add_node(vi, "Array Size", 650, 320)
lv.create_constant(asz["h"], "array", [1.5, 2.5, 3.5, 4.5])
lv.create_indicator(asz["h"], "size(s)", label="n")

step("errors / cleanup / run")
e = lv.errors(vi)
check("main VI not broken", not e["broken"], e)
lv.cleanup(vi)
r = lv.run_vi(vi, inputs={"use mul": True, "text": "   hi there  "}, timeout=10)
print(r)
check("run result", r["result"] == 12, r["result"])
check("run count", r["count"] == 5, r["count"])
check("run product", r["product"] == 10.0, r["product"])
check("run subVI sum", r["sum"] == 3.25, r["sum"])
check("run trimmed", r["trimmed"] == "hi there", repr(r["trimmed"]))
check("run array size", r["n"] == 4, r["n"])
lv.set_value(vi, "use mul", False)
check("get/set value", lv.get_value(vi, "use mul") is False)

step("describe")
t = time.time()
d = lv.describe_vi(vi)
js = json.dumps(d)
print(f"describe: {len(d['nodes'])} nodes, {len(d['wires'])} wires, {len(js)} bytes, {time.time() - t:.2f} s")
(OUT / f"{a.prefix}test_ops_describe.json").write_text(json.dumps(d, indent=1))
classes = {x["class"] for x in d["nodes"]}
check("describe classes", {"WhileLoop", "CaseStructure", "Function", "SubVI", "ControlTerminal"} <= classes, classes)
check("describe wires", all(w["from"] and w["to"] for w in d["wires"]), [w for w in d["wires"] if not (w["from"] and w["to"])])
loopd = [x for x in d["nodes"] if x["class"] == "WhileLoop"][0]
check("describe shift register + tunnel", loopd.get("shift_registers") and loopd.get("tunnels"), loopd)

step("for loop count")
vf = lv.new_vi()
fl = lv.add_structure(vf, "For Loop", 150, 40, 300, 200)
lv.set_label(lv.create_control(fl["h"], "N")["h"], "N")       # N is a tunnel: control on its outside terminal
lv.set_label(lv.create_indicator(fl["h"], "N.inside")["h"], "count")
check("for loop N wired", [t["wired"] for t in lv.terminals(fl["h"]) if t["name"] == "N"] == [True])
check("for loop N run", not lv.errors(vf)["broken"] and lv.run_vi(vf, {"N": 7}, ["count"])["count"] == 7)
lv.close_vi(vf)

step("bug bash r1 regressions")
vf = lv.new_vi()
fl = lv.add_structure(vf, "For Loop", 150, 40, 300, 200)
lv.set_label(lv.create_control(fl["h"], "N")["h"], "Count")
m = lv.add_node(fl["diagrams"][0]["h"], "Multiply", 80, 60)
lv.wire(fl["h"], "i", m["h"], "x")
lv.wire(fl["h"], "i", m["h"], "y")
neg = lv.add_node(fl["diagrams"][0]["h"], "Negate", 180, 60)
lv.wire(m["h"], "x*y", neg["h"], "x")                  # an existing branch that must survive
sq = lv.create_indicator(m["h"], "x*y", "Squares", owner=vf, x=520, y=100)
check("indicator out of a loop is an array", sq["type"] == "I32[]", sq)
check("existing branch kept", [t["wired"] for t in lv.terminals(neg["h"]) if t["name"] == "x"] == [True])
check("loop indicator run", lv.run_vi(vf, {"Count": 4}, ["Squares"])["Squares"] == [0, 1, 4, 9])
lv.delete_object(neg["h"])
check("delete leaves no loose wires", not lv.errors(vf)["broken"], lv.errors(vf))
lv.close_vi(vf)
vf = lv.new_vi()
m = lv.add_node(vf, "Multiply", 250, 100)
a_ = lv.create_control(m["h"], "x", "A", type="DBL[]")
lv.create_control(m["h"], "y", "B", type="DBL[]")
check("typed control", a_["type"] == "DBL[]", a_)
c = lv.add_node(vf, "Concatenate Strings", 250, 250)
check("resize_node", [t["name"] for t in lv.resize_node(c["h"], 3)][-1] == "string#3")
lv.set_value(vf, "A", [1.0, 2.0], default=True)
check("set_value default", lv.get_value(vf, "A") == [1.0, 2.0])
u = lv.add_node(vf, "Unbundle By Name", 250, 400)
pc = lv.create_control(u["h"], "input cluster", "Point", type="cluster{x:DBL, y:DBL}")
check("cluster control from a type", pc["type"] == "cluster{x:DBL,y:DBL}", pc)
bd_ = lv.add_node(vf, "Bundle", 250, 500)
check("Bundle element inputs listed", [t["name"] for t in lv.resize_node(bd_["h"], 2)][-2:] == ["element", "element#2"])
bca = lv.add_node(vf, "Build Cluster Array", 250, 600)
check("resize Build Cluster Array", len([t for t in lv.resize_node(bca["h"], 3) if t["dir"] == "in"]) == 3)
try:
    lv.invoke_method(vf, "Save:Instrument", {"bogus": 1})
    check("invoke_method bad input -> error listing inputs", False)
except Exception as e:  # noqa: BLE001
    check("invoke_method bad input -> error listing inputs", "inputs are" in str(e), e)
try:
    lv.class_members("NoSuchClass")
    check("class_members unknown class -> error", False)
except Exception as e:  # noqa: BLE001
    check("class_members unknown class -> error", "unknown" in str(e), e)
lv.close_vi(vf)

step("front-panel layout")
vf = lv.new_vi()
m = lv.add_node(vf, "Add", 250, 100)
first = lv.create_control(m["h"], "x", "first")
lv.create_control(m["h"], "y", "second")
lv.create_indicator(m["h"], "x+y", "total")
g = lv.add_panel_object(vf, "Waveform Graph (modern)", 0, 0, "plot")
t = lv.add_label(vf, "Adder", 0, 0, size=24, bold=True)
check("default placement overlaps (the reason for arrange_panel)", lv.check_panel(vf))
rg = lv.resize_object(g["h"], 400, 250)
check("resize graph", abs(rg["size"][0] - 400) <= 2 and abs(rg["size"][1] - 250) <= 2, rg)
ar = lv.arrange_panel(vf)
pos = {o["label"]: o["pos"] for o in ar["objects"]}
check("arrange: no overlaps, title on top, inputs left of outputs, graph right",
      not ar["overlaps"] and pos["Adder"][1] < pos["first"][1] < pos["second"][1]
      and pos["first"][0] < pos["total"][0] < pos["plot"][0], ar)
ar = lv.arrange_panel(vf, rows=[["first", "second", "total"], ["plot"]])
pos = {o["label"]: o["pos"] for o in ar["objects"]}
check("arrange rows", not ar["overlaps"] and pos["first"][1] == pos["second"][1] == pos["total"][1] < pos["plot"][1],
      ar)
tab = lv.add_panel_object(vf, "Tab Control (modern)", 600, 400, "tabs")
pg = tab["pages"][1]
lv.move_object(lv.panel(vf, False)[0]["h"], pg["pos"][0] + 10, pg["pos"][1] + 10, pg["h"])
pnl = lv.panel(vf, False)
check("control on a tab page listed with its page", [c["label"] for c in pnl if c.get("page") == pg["h"]] == ["first"],
      [(c["label"], c.get("page")) for c in pnl])
ar = lv.arrange_panel(vf, page=pg["h"])
check("arrange a tab page", not ar["overlaps"] and ar.get("tab_size"), ar)
ar = lv.arrange_panel(vf)
check("run with a control on a tab page", lv.run_vi(vf, {"first": 2, "second": 3}, ["total"])["total"] == 5)
pb = lv._get(vf, "VI", "Front Panel Window:Panel Bounds")
d_ = lv.describe_vi(vf)
check("describe_vi reports the creation handles", g["h"] in [c["h"] for c in d_["panel"]]
      and t["h"] in [x["h"] for x in d_.get("decorations", [])] and m["h"] in [n["h"] for n in d_["nodes"]]
      and first["terminal"] in [c["terminal"] for c in d_["panel"]] and first["terminal"] in [n["h"] for n in d_["nodes"]])
check("window fitted", pb[2] - pb[0] == ar["window"][0] and pb[3] - pb[1] == ar["window"][1], (pb, ar["window"]))
(OUT / f"{a.prefix}test_ops_layout.png").write_bytes(lv.get_image(vf, "panel"))
lv.close_vi(vf)

step("images")
png = lv.get_image(vi, "diagram")
(OUT / f"{a.prefix}test_ops_bd.png").write_bytes(png)
png2 = lv.get_image(vi, "panel")
(OUT / f"{a.prefix}test_ops_fp.png").write_bytes(png2)
png3 = lv.get_image(loop["h"])
check("images are PNG", png[:4] == b"\x89PNG" and png2[:4] == b"\x89PNG" and png3[:4] == b"\x89PNG")

step("move, save, save for previous")
lv.move_object(asz["h"], 700, 420)
main_path = lvp(OUT / f"{a.prefix}test_ops.vi")
lv.save_vi(vi, main_path)
sfp = lv.save_for_previous(vi, lvp(OUT / "lv14" / f"{a.prefix}test_ops.vi"), "14.0")
print(sfp)
head = (OUT / "lv14" / f"{a.prefix}test_ops.vi").read_bytes()[:4096]
vers = re.findall(rb"[\x03-\x0c](\d{1,2}\.\d[.\w]*)", head)
check("saved as 14.0", vers and vers[0] == b"14.0", vers[:2])
info = lv.vi_info(vi)
check("VI renamed after save", info["name"] == f"{a.prefix}test_ops.vi", info)
lv.close_vi(vi)

step("reopen saved VI and run it")
vi = lv.open_vi(main_path)
r = lv.run_vi(vi, inputs={"use mul": True}, outputs=["product", "count"])
check("reopened run", r == {"product": 10.0, "count": 5}, r)
lv.close_vi(vi)

print(f"\n{sum(ok)}/{len(ok)} checks passed in {time.time() - T0:.1f} s")
sys.exit(0 if all(ok) else 1)
