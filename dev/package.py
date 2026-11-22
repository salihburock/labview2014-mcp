#!/usr/bin/env python3
"""Build the release zip: dist/lvmcp-<version>.zip (LabVIEW 14.0 VIs, Python package, docs, Windows scripts)."""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lvmcp import __version__  # noqa: E402

files = ["README.md", "CHANGELOG.md", "requirements.txt", "install.bat", "selftest.bat"]
files += [f"docs/{n}" for n in ("INSTALL.md", "mcp.md", "ops.md", "palette.md", "bootstrap.md")]
files += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "docs/img").glob("*.png"))]
files += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "labview").rglob("*.vi"))]
files += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "lvmcp").glob("*.py"))]
files += ["lvmcp/warm.json", "LICENSE"]
files = [f for f in files if (ROOT / f).exists()]     # (a checkout without the docs builds a zip without them)
out = ROOT / "dist" / f"lvmcp-{__version__}.zip"
out.parent.mkdir(exist_ok=True)
top = f"lvmcp-{__version__}"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(ROOT / f, f"{top}/{f}")
print(f"{out} ({out.stat().st_size // 1024} KB, {len(files)} files)")
