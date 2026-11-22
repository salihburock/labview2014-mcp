#!/usr/bin/env python3
"""End-to-end test of lvmcp/mcp_server.py: starts it over stdio with the MCP SDK client and builds VIs by tool calls.

Needs a running lvmcp server VI (default port 5582 of the headless LabVIEW 2022). Uses its own renamed copy of the
boot wrappers and its own gen dir/prefix (lvC_), saves only lvC_* VIs under build/testC.

  .venv/bin/python dev/test_mcp.py [--port 5582] [--out build/testC] [--no-palette]
"""
import argparse
import asyncio
import base64
import json
import shutil
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5582)
ap.add_argument("--token", default="")
ap.add_argument("--boot-src", default=str(ROOT / "build/lv2022/boot"))
ap.add_argument("--gen", default=str(ROOT / "build/genC"))
ap.add_argument("--prefix", default="lvC_")
ap.add_argument("--out", default=str(ROOT / "build/testC"))
ap.add_argument("--dead-port", type=int, default=5599, help="a port where no server listens")
ap.add_argument("--palette-src", default=str(ROOT / "build/genB"),
                help="folder with an already built palette table (copied to <gen>/palette)")
ap.add_argument("--no-palette", action="store_true")
ap.add_argument("--map", default="", help="HOSTDIR=LVDIR: LabVIEW (e.g. in a VM) sees local HOSTDIR as LVDIR; "
                "then the boot wrappers are used in place from --boot-src (no private copies)")
a = ap.parse_args()

OUT = Path(a.out)
(OUT / "lv14").mkdir(parents=True, exist_ok=True)
T0 = time.time()
ok = []


def check(name, cond, info=""):
    info = str(info)
    print(f"[{'ok' if cond else 'FAIL'}] {name} {info[:300]}", flush=True)
    ok.append(bool(cond))


def step(msg):
    print(f"--- {msg} ({time.time() - T0:.1f} s)", flush=True)


def lvp(p) -> str:
    """Local path -> path as the LabVIEW machine sees it (--map)."""
    p = str(p)
    if a.map:
        host, lvdir = a.map.split("=", 1)
        if p.startswith(host):
            return lvdir + p[len(host):].replace("/", "\\")
    return p


def server_params(port, extra=()):
    if a.map:
        boot = ["--boot-dir", lvp(a.boot_src)]
    else:
        boot = ["--boot-copy-from", a.boot_src, "--boot-dir", str(Path(a.gen) / "boot"), "--boot-prefix", a.prefix]
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "lvmcp/mcp_server.py"), "--host", a.host, "--port", str(port), "--token", a.token,
              *boot, "--gen-dir", lvp(a.gen), "--cache-dir", a.gen, "--prefix", a.prefix, *extra],
        cwd=str(ROOT))


class Tools:
    def __init__(self, session):
        self.s = session

    async def raw(self, _tool, **args):
        t = time.time()
        r = await self.s.call_tool(_tool, args, read_timeout_seconds=300)
        if time.time() - t > 5:
            print(f"    (slow: {_tool} took {time.time() - t:.1f} s)", flush=True)
        texts = [c.text for c in r.content if c.type == "text"]
        imgs = [base64.b64decode(c.data) for c in r.content if c.type == "image"]
        return r.is_error, texts, imgs

    async def __call__(self, _tool, **args):
        err, texts, imgs = await self.raw(_tool, **args)
        if err:
            raise RuntimeError(f"{_tool} failed: {texts}")
        try:
            data = json.loads(texts[0]) if texts else None
        except json.JSONDecodeError:
            data = texts[0]
        return (data, imgs) if imgs else data


def term(node, direction):
    return [t[0] for t in node["terms"] if t[1] == direction]


def is_png(b):
    return b[:8] == b"\x89PNG\r\n\x1a\n"


async def unreachable_test():
    step(f"server not reachable (port {a.dead_port})")
    async with stdio_client(server_params(a.dead_port, ["--palette", "off", "--autostart", "off"])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            T = Tools(s)
            st = await T("status")
            check("status says unreachable", st["reachable"] is False and "lvmcp_server.vi" in st["error"], st)
            err, texts, _ = await T.raw("new_vi")
            check("new_vi -> tool error mentioning lvmcp_server.vi", err and "lvmcp_server.vi" in texts[0], texts)


async def main_test():
    extra = []
    if not a.no_palette:
        pal = sorted(Path(a.palette_src).glob("palette_*.json"))
        if pal:
            dst = Path(a.gen) / "palette"
            dst.mkdir(parents=True, exist_ok=True)
            for f in pal:
                shutil.copy2(f, dst / f.name)
            extra = ["--palette", "auto", "--palette-cache", str(dst)]
        else:
            extra = ["--palette", "off"]
    else:
        extra = ["--palette", "off"]
    async with stdio_client(server_params(a.port, extra)) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            T = Tools(s)
            step("list tools")
            tools = (await s.list_tools()).tools
            names = [t.name for t in tools]
            print(names)
            need = {"status", "new_vi", "open_vi", "describe_vi", "find_object", "add_node", "add_subvi",
                    "add_structure", "add_frame", "set_case_names", "wire", "create_control", "create_indicator",
                    "create_constant", "set_value", "get_value", "run_vi", "errors", "cleanup_diagram",
                    "get_image", "save_vi", "save_for_previous", "close_vi", "delete_object", "move_object",
                    "add_panel_object", "set_elements", "class_members", "get_property", "set_property",
                    "invoke_method", "arrange_panel", "resize_object", "add_label"}
            check("all tools listed", need <= set(names), sorted(need - set(names)))
            check("instructions present", bool(getattr(init, "instructions", None)))

            step("status")
            st = await T("status")
            print(st)
            check("status reachable", st.get("reachable") and st.get("version"), st)

            step("find_object")
            f = await T("find_object", query="multiply", n=5)
            print(f["source"], [x["name"] for x in f["results"]])
            check("find_object multiply", f["results"] and f["results"][0]["name"] == "Multiply", f)
            f = await T("find_object", query="while loop", n=5)
            check("find_object while loop", any("While Loop" in x["name"] for x in f["results"][:3]),
                  [x["name"] for x in f["results"]])
            f = await T("find_object", query="greater or equal", n=5)
            check("find_object greater or equal", any(x["name"] == "Greater Or Equal?" for x in f["results"][:3]),
                  [x["name"] for x in f["results"]])

            # ---------------------------------------------------------- scenario 1: 3 * 4 = 12
            step("scenario 1: Multiply of two constants")
            v = await T("new_vi")
            vi = v["vi"]
            mul = await T("add_node", owner=vi, name="Multiply", x=250, y=120)
            check("Multiply terms", sorted(term(mul, "in")) == ["x", "y"] and term(mul, "out") == ["x*y"], mul["terms"])
            await T("create_constant", node=mul["h"], terminal="x", value=3)
            await T("create_constant", node=mul["h"], terminal="y", value=4)
            ind = await T("create_indicator", node=mul["h"], terminal="x*y", label="product")
            check("indicator label", ind["label"] == "product", ind)
            err, texts, _ = await T.raw("wire", src=mul["h"], src_terminal="nope", dst=ind["terminal"])
            check("bad terminal -> tool error with terminal list", err and "x*y" in texts[0], texts)
            res = await T("run_vi", vi=vi)
            check("run -> product 12", res.get("product") == 12, res)
            img = await T("get_image", handle=vi, what="diagram")
            png = img[1][0] if isinstance(img, tuple) else None
            check("diagram image is PNG", png and is_png(png), len(png or b""))
            if png:
                (OUT / "lvC_multiply_bd.png").write_bytes(png)
            res, imgs = await T("run_vi", vi=vi, image=True)
            check("run_vi image=true gives panel PNG", imgs and is_png(imgs[0]), res)
            p = lvp(OUT / "lvC_multiply.vi")
            sv = await T("save_vi", vi=vi, path=p)
            check("saved", (OUT / "lvC_multiply.vi").exists(), sv)
            sp = await T("save_for_previous", vi=vi, path=lvp(OUT / "lv14" / "lvC_multiply.vi"), version="14.0")
            check("saved for 14.0", (OUT / "lv14" / "lvC_multiply.vi").exists(), sp)
            d = await T("describe_vi", vi=vi)
            check("describe: 3 wires, idle", len(d["wires"]) == 3 and d["state"] == "idle",
                  [(w["from"], w["to"]) for w in d["wires"]])
            await T("close_vi", vi=vi)

            step("open the saved VI again and run it")
            o = await T("open_vi", path=p)
            res = await T("run_vi", vi=o["vi"], outputs=["product"])
            check("reopened run -> 12", res.get("product") == 12, res)
            await T("close_vi", vi=o["vi"])

            # ---------------------------------------------------------- scenario 2: while loop counter
            step("scenario 2: While Loop, shift register counter, stop when i >= 9")
            vi = (await T("new_vi"))["vi"]
            loop = await T("add_structure", owner=vi, kind="while", x=150, y=60, width=420, height=240)
            check("while loop i/stop", [t[0] for t in loop["terms"]] == ["i", "stop"], loop["terms"])
            inner = loop["diagrams"][0]["h"]
            sr = await T("add_shift_register", loop=loop["h"])
            inc = await T("add_node", owner=inner, name="Increment", x=300, y=100)
            await T("wire", src=sr["left"]["h"], src_terminal="inside", dst=inc["h"], dst_terminal="x")
            await T("wire", src=inc["h"], src_terminal=term(inc, "out")[0], dst=sr["right"]["h"],
                    dst_terminal="inside")
            await T("create_constant", node=sr["left"]["h"], terminal="outside", value=0, type="I32")
            await T("create_indicator", node=sr["right"]["h"], terminal="outside", label="count")
            ge = await T("add_node", owner=inner, name="Greater Or Equal?", x=380, y=210)
            await T("wire", src=loop["h"], src_terminal="i", dst=ge["h"], dst_terminal="x")
            await T("create_constant", node=ge["h"], terminal="y", value=9, type="I32")
            wr, imgs = await T("wire", src=ge["h"], src_terminal=term(ge, "out")[0], dst=loop["h"],
                               dst_terminal="stop", image=True)
            check("wire image=true gives diagram PNG", imgs and is_png(imgs[0]), wr)
            e = await T("errors", vi=vi)
            check("loop VI not broken", not e["broken"], e)
            res = await T("run_vi", vi=vi, timeout=20)
            check("loop count = 10", res.get("count") == 10, res)
            d = await T("describe_vi", vi=vi)
            ld = [n for n in d["nodes"] if n["class"] == "WhileLoop"]
            check("describe: loop with shift register", ld and ld[0].get("shift_registers"), ld)

            step("case structure inside: count even? -> string")
            cs = await T("add_structure", owner=vi, kind="case", x=650, y=60, width=260, height=160)
            check("case frames False/True", [x.get("name") for x in cs["diagrams"]] == ["False", "True"],
                  cs["diagrams"])
            ctl = await T("create_control", node=cs["h"], terminal="selector", label="double it")
            mul2 = await T("add_node", owner=cs["diagrams"][1]["h"], name="Multiply", x=740, y=120)
            await T("wire", src=sr["right"]["h"], src_terminal="outside", dst=mul2["h"], dst_terminal="x")
            await T("create_constant", node=mul2["h"], terminal="y", value=2, type="I32")
            await T("create_indicator", node=mul2["h"], terminal="x*y", label="doubled", owner=vi, x=960, y=120)
            e = await T("errors", vi=vi)
            check("case VI broken (output tunnel unwired in False case)", e["broken"], e)
            d = await T("describe_vi", vi=vi)
            csd = [n for n in d["nodes"] if n["class"] == "CaseStructure"][0]
            out_t = [t for t in csd.get("tunnels", []) if any(x[0] == "inside" and x[1] == "in" for x in t["terms"])]
            check("case output tunnel found", out_t, csd.get("tunnels"))
            if out_t:
                # False frame: pass the count through (inside #1 belongs to frame 0 = False)
                in_t = [t for t in csd["tunnels"] if t not in out_t and t["terms"][0][1:3] == ["in", "I32"]][0]
                await T("wire", src=in_t["h"], src_terminal="inside", dst=out_t[0]["h"], dst_terminal="inside")
            e = await T("errors", vi=vi)
            check("case VI fixed", not e["broken"], e)
            res = await T("run_vi", vi=vi, inputs={"double it": True}, timeout=20)
            check("doubled = 20", res.get("doubled") == 20 and res.get("count") == 10, res)
            res = await T("run_vi", vi=vi, inputs={"double it": False}, timeout=20)
            check("not doubled = 10", res.get("doubled") == 10, res)
            await T("cleanup_diagram", vi=vi)
            img = await T("get_image", handle=vi)
            if isinstance(img, tuple):
                (OUT / "lvC_loop_bd.png").write_bytes(img[1][0])
            p2 = lvp(OUT / "lvC_loop.vi")
            await T("save_vi", vi=vi, path=p2)
            await T("save_for_previous", vi=vi, path=lvp(OUT / "lv14" / "lvC_loop.vi"))
            check("loop VI saved (2022 + 14.0)", (OUT / "lvC_loop.vi").exists() and (OUT / "lv14" / "lvC_loop.vi").exists())
            await T("close_vi", vi=vi)

            step("front-panel graph (add_panel_object) fed by a vi.lib subVI")
            vi = (await T("new_vi"))["vi"]
            g = await T("add_panel_object", vi=vi, name="Waveform Graph (modern)", x=300, y=40, label="signal")
            check("graph is an indicator with a terminal", g.get("kind") == "indicator" and g.get("terminal"), g)
            fo = await T("find_object", query="Sine Waveform")
            sw_item = [r for r in fo["results"] if r.get("path", "").endswith("Sine Waveform.vi")]
            if sw_item:
                sw = await T("add_subvi", owner=vi, path=sw_item[0]["path"], x=80, y=80)
                await T("create_constant", node=sw["h"], terminal="sampling info", value=[16.0, 16.0])
                await T("create_constant", node=sw["h"], terminal="frequency", value=2.0)   # < Fs/2
                await T("wire", src=sw["h"], src_terminal="signal out", dst=g["terminal"], dst_terminal="value")
                res = await T("run_vi", vi=vi, outputs=["signal"])
                check("graph gets 16 samples", len(res["signal"]["Y"]) == 16, str(res)[:120])
            else:
                check("find_object finds Sine Waveform.vi (needs the palette table)", a.no_palette, fo)
            gwc = await T("add_node", owner=vi, name="Get Waveform Components", x=80, y=300)
            terms = await T("set_elements", node=gwc["h"], names=["Y", "dt"])
            check("set_elements shows dt", [t["name"] for t in terms][-2:] == ["Y", "dt"], terms)
            cm = await T("class_members", class_name="WhileLoop")
            check("class_members lists properties and methods", cm["properties"] and cm["methods"], str(cm)[:100])
            gp = await T("get_property", handle=gwc["h"], prop="Element Names[]")
            check("get_property", gp["value"] == ["Y", "dt"], gp)
            await T("add_label", vi=vi, text="Sine demo", x=0, y=0, size=24)
            await T("resize_object", handle=g["h"], width=420, height=260)
            ar, imgs = await T("arrange_panel", vi=vi)
            check("arrange_panel: no overlaps, panel image", not ar["overlaps"] and imgs and is_png(imgs[0]), ar)
            sv = await T("save_vi", vi=vi, path=lvp(OUT / "lvC_panel.vi"))
            check("save_vi: no overlap warning after arrange", "warning" not in sv, sv)
            await T("close_vi", vi=vi)

            step("errors come back as tool errors")
            err, texts, _ = await T.raw("add_node", owner=12345, name="Add", x=0, y=0)
            check("invalid handle -> tool error", err and "LabVIEW error" in texts[0], texts)
            err, texts, _ = await T.raw("add_node", owner=1, name="Multiplyy", x=0, y=0)
            check("tool error text", err, texts)


async def main():
    await unreachable_test()
    await main_test()


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} checks passed in {time.time() - T0:.1f} s")
sys.exit(0 if all(ok) else 1)
