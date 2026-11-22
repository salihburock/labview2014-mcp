#!/usr/bin/env python3
"""Export the LabVIEW 2022 sources in build/lv2022/ to LabVIEW 14.0 files in labview/ (Save for Previous).

Runs through lvmcp_server.vi (which must be running in LabVIEW 2022). The running server VI cannot be saved
for previous (error 1507), so a copy of its file is exported instead.
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp.lvclient import Client  # noqa: E402
from lvmcp.scripting import Scripting  # noqa: E402

SRC, DST = ROOT / "build/lv2022", ROOT / "labview"
c = Client().connect()
S = Scripting(c, str(SRC / "boot"), str(ROOT / "build/gen2022e"))
ovr = S.wrapper("prim", "", "Open VI Reference",
                skip=("application reference (local)", "type specifier VI Refnum (for type only)"))
sfp = S.wrapper("meth", "VI", "Save:For Previous")

tmp = Path(tempfile.mkdtemp())
server_copy = tmp / "lvmcp_server_copy.vi"
shutil.copy(SRC / "lvmcp_server.vi", server_copy)
jobs = [(server_copy, DST / "lvmcp_server.vi"), (SRC / "lvmcp_empty.vi", DST / "lvmcp_empty.vi")]
jobs += [(p, DST / "boot" / p.name) for p in sorted((SRC / "boot").glob("*.vi"))]
(DST / "boot").mkdir(parents=True, exist_ok=True)
for src, dst in jobs:
    ref = ovr.run({"vi path": str(src)}, ["vi reference"])["vi reference"]
    try:
        r = sfp.run({"reference": ref, "Path to Saved File": str(dst), "Target Version": "14.0"},
                    ["Warnings"], check=False)
        print(f"{dst.relative_to(ROOT)}: error {r['error out'][1]} {r['Warnings'] or ''}")
    finally:
        S.close(ref)
