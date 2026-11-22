#!/usr/bin/env python3
"""Start extra copies of lvmcp_server.vi on other ports in the running LabVIEW 2022 (for parallel clients).

  python3 dev/start_servers.py 5580 5581 ...

Each copy is a file copy with its own name (VI names are unique per LabVIEW instance), opened by reference,
given its port and started with Run VI (not waiting). The main server on 5577 must be running.
"""
import shutil
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp.lvclient import Client  # noqa: E402
from lvmcp.scripting import Scripting  # noqa: E402

c = Client().connect()
S = Scripting(c, str(ROOT / "build/lv2022/boot"), str(ROOT / "build/gen2022e"))
ovr = S.wrapper("prim", "", "Open VI Reference",
                skip=("application reference (local)", "type specifier VI Refnum (for type only)"))
setv = S.wrapper("meth", "VI", "Control Value:Set")
run = S.wrapper("meth", "VI", "Run VI")
(ROOT / "build/servers").mkdir(exist_ok=True)
for port in map(int, sys.argv[1:]):
    p = ROOT / f"build/servers/lvmcp_server_p{port}.vi"
    shutil.copy(ROOT / "build/lv2022/lvmcp_server.vi", p)
    ref = ovr.run({"vi path": str(p)}, ["vi reference"])["vi reference"]
    tmpl = S.peek(str(p), ["port"])["port"]
    setv.run({"reference": ref, "Control Name": "port", "Value": tmpl.with_value(port)})
    run.run({"reference": ref, "Wait Until Done": False, "Auto Dispose Reference": True})
    for _ in range(20):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            print(f"server on {port}: up")
            break
        except OSError:
            time.sleep(0.5)
    else:
        print(f"server on {port}: NOT reachable")
