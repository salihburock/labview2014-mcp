#!/usr/bin/env python3
"""Two MCP sessions on ONE server VI at the same time, sharing one (initially empty) wrapper cache: both build
and run a VI with interleaved tool calls. Checks that neither hangs and both get the right results.

  .venv/bin/python dev/test_shared.py [--port 5581] [--gen build/genS2]
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp.ops import private_boot_copy  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=5581)
ap.add_argument("--gen", default=str(ROOT / f"build/genSh{int(time.time()) % 100000}"))
ap.add_argument("--prefix", default="lvSh_")
ap.add_argument("--boot-src", default=str(ROOT / "build/lv2022/boot"))
a = ap.parse_args()
boot = private_boot_copy(a.boot_src, Path(a.gen) / "boot", a.prefix)
T0 = time.time()


def params():
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "lvmcp/mcp_server.py"), "--port", str(a.port), "--boot-dir", str(boot),
              "--boot-prefix", a.prefix, "--gen-dir", a.gen, "--prefix", a.prefix, "--palette", "off"],
        cwd=str(ROOT))


async def session(tag, fn, x, y, expect):
    async with stdio_client(params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        async def call(tool, **args):
            res = await s.call_tool(tool, args, read_timeout_seconds=300)
            text = [c.text for c in res.content if c.type == "text"]
            if res.is_error:
                raise RuntimeError(f"{tag} {tool}: {text}")
            await asyncio.sleep(0)
            return json.loads(text[0])

        vi = (await call("new_vi"))["vi"]
        n = await call("add_node", owner=vi, name=fn, x=200, y=100)
        await call("create_constant", node=n["h"], terminal="x", value=x)
        await call("create_constant", node=n["h"], terminal="y", value=y)
        out = [t[0] for t in n["terms"] if t[1] == "out"][0]
        await call("create_indicator", node=n["h"], terminal=out, label="r")
        got = (await call("run_vi", vi=vi, outputs=["r"]))["r"]
        await call("close_vi", vi=vi)
        print(f"{tag}: {fn}({x}, {y}) = {got} ({time.time() - T0:.1f} s)", flush=True)
        return got == expect


async def killed_session():
    """A session that saves a VI and is then killed (no cleanup): the next session must free that VI's name."""
    import os
    import signal
    name = f"lvSh_killed_{int(time.time())}.vi"
    path = str(Path(a.gen) / name)
    async with stdio_client(params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        vi = json.loads((await s.call_tool("new_vi", {})).content[0].text)["vi"]
        await s.call_tool("save_vi", {"vi": vi, "path": path})
        pids = [int(f.stem) for f in (Path(a.gen) / "sessions").glob("*.json")]
        for pid in pids:
            os.kill(pid, signal.SIGKILL)
    await asyncio.sleep(1)
    async with stdio_client(params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        vi = json.loads((await s.call_tool("new_vi", {})).content[0].text)["vi"]   # first call reaps
        res = await s.call_tool("save_vi", {"vi": vi, "path": path})              # same name: must work
        ok = not res.is_error
        print(f"killed session: its VI name {'freed' if ok else 'STILL HELD'} ({res.content[0].text[:120]})")
        await s.call_tool("close_vi", {"vi": vi})
        return ok


async def selftest():
    """A first-run selftest (warm-up: ~100 helper VIs) on the same server while the sessions work."""
    g = Path(a.gen + "_st")
    b = private_boot_copy(a.boot_src, g / "boot", "lvSt_")
    p = await asyncio.create_subprocess_exec(sys.executable, "-m", "lvmcp.selftest", "--port", str(a.port),
                                             "--boot-dir", str(b), "--boot-prefix", "lvSt_", "--gen-dir", str(g),
                                             "--prefix", "lvSt_", cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    out = (await p.communicate())[0].decode()
    ok = p.returncode == 0 and "OK:" in out
    print(f"selftest alongside: {'OK' if ok else 'FAIL'} ({time.time() - T0:.1f} s) {out.strip().splitlines()[-1]}")
    return ok


async def main():
    res = await asyncio.wait_for(asyncio.gather(session("A", "Multiply", 3, 4, 12), session("B", "Add", 5, 6, 11),
                                                session("C", "Subtract", 9, 2, 7), selftest()), 900)
    res.append(await asyncio.wait_for(killed_session(), 300))
    print("PASS" if all(res) else "FAIL", res)
    return all(res)


sys.exit(0 if asyncio.run(main()) else 1)
