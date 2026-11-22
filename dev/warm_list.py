#!/usr/bin/env python3
"""Regenerate lvmcp/warm.json (wrappers selftest prepares) from wrapper caches of test runs / real sessions:

  python3 dev/warm_list.py build/genW1 build/genW2 build/bugbash/r1/gen-* ...

Keeps the entries already in warm.json; adds every prop/meth/prim/batch wrapper of the given caches (palette
helpers are left out: the palette table has its own cache)."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = ROOT / "lvmcp/warm.json"
specs = {}


def add(s):
    specs.setdefault(json.dumps([s["kind"], s["class"], s["item"], s["write"], sorted(s["skip"])]), s)


for s in json.loads(out.read_text()):
    add(s)
for d in sys.argv[1:]:
    f = Path(d) / "index.json"
    if not f.exists():
        continue
    for name, v in json.loads(f.read_text()).items():
        if not isinstance(v, dict) or (v["kind"] == "custom" and v.get("class") != "SubVI"):
            continue
        if "skip" not in v and re.search(r"_s[0-9a-f]{6}\.vi$", name):
            continue                                  # (old index entry: skip set unknown)
        add({"kind": v["kind"], "class": v["class"], "item": v["item"], "write": bool(v["write"]),
             "skip": sorted(v.get("skip", []))})
order = {"prop": 0, "meth": 1, "prim": 2, "batch": 3, "custom": 4}
lst = sorted(specs.values(), key=lambda s: (order[s["kind"]], s["class"], json.dumps(s["item"]), s["write"]))
out.write_text("[\n" + ",\n".join(json.dumps(x) for x in lst) + "\n]\n")
print(f"{out}: {len(lst)} wrappers")
