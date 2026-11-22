#!/usr/bin/env python3
"""Abort server copies started by dev/start_servers.py (frees every reference/VI their clients left open).

  python3 dev/stop_servers.py 5580 5581 ...      (talks to the main server on 5577)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp.lvclient import Client, LVError  # noqa: E402
from lvmcp.scripting import Scripting  # noqa: E402

c = Client().connect()
S = Scripting(c, str(ROOT / "build/lv2022/boot"), str(ROOT / "build/gen2022e"))
ovr = S.wrapper("prim", "", "Open VI Reference",
                skip=("application reference (local)", "type specifier VI Refnum (for type only)"))
abort = S.wrapper("meth", "VI", "Abort VI")
for port in sys.argv[1:]:
    p = ROOT / f"build/servers/lvmcp_server_p{port}.vi"
    ref = ovr.run({"vi path": str(p)}, ["vi reference"])["vi reference"]
    try:
        r = abort.run({"reference": ref}, check=False)
        print(f"server {port}: aborted {r['error out'][:2]}")
    finally:
        try:
            S.close(ref)
        except LVError:
            pass
