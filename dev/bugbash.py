#!/usr/bin/env python3
"""Bug bash: fresh Claude Code sessions (only the lvmcp MCP tools) build small but varied VIs; each result is
then opened, run with fixed inputs and compared with expected outputs, and every tool error in the
transcripts is collected.

  python3 dev/bugbash.py run [B1 B2 ...] [--ports 5577,5580,5581,5582] [--tag r1]
  python3 dev/bugbash.py verify [--tag r1] [--port 5577]
  python3 dev/bugbash.py report [--tag r1]

Each session gets its own server port, wrapper prefix, private boot copies and a cache warmed by
`python -m lvmcp.selftest` (like a fresh install). Output: build/bugbash/<tag>/ (VIs, transcripts, report).
"""
import argparse
import json
import tempfile
import math
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# id: (VI file name, task text, [(inputs, expected outputs)])
TASKS = {
    "B1": ("C2F.vi", "Convert a temperature: DBL control `Celsius`, DBL indicator `Fahrenheit` = Celsius * 1.8 + 32.",
           [({"Celsius": 100.0}, {"Fahrenheit": 212.0}), ({"Celsius": -40.0}, {"Fahrenheit": -40.0})]),
    "B2": ("ArrayStats.vi", "Statistics of an array: control `Data` (1D array of DBL); DBL indicators `Mean`, `Max` "
           "and `Min` of the elements.",
           [({"Data": [1.0, 2.0, 3.0, 4.0]}, {"Mean": 2.5, "Max": 4.0, "Min": 1.0})]),
    "B3": ("Greeting.vi", "String controls `First Name` and `Last Name`; string indicator `Greeting` = "
           "\"Hello, <First Name> <Last Name>!\" and I32 indicator `Length` = number of characters of Greeting.",
           [({"First Name": "Ada", "Last Name": "Lovelace"}, {"Greeting": "Hello, Ada Lovelace!", "Length": 20})]),
    "B4": ("Calculator.vi", "DBL controls `x` and `y`, I32 control `Operation`; DBL indicator `Result`. A Case "
           "Structure on Operation: 0 = x + y, 1 = x - y, 2 = x * y, any other value (default case) = x / y.",
           [({"x": 6.0, "y": 3.0, "Operation": 0}, {"Result": 9.0}), ({"x": 6.0, "y": 3.0, "Operation": 1}, {"Result": 3.0}),
            ({"x": 6.0, "y": 3.0, "Operation": 2}, {"Result": 18.0}), ({"x": 6.0, "y": 3.0, "Operation": 7}, {"Result": 2.0})]),
    "B5": ("SumUntil.vi", "I32 control `Limit`. A While Loop adds 1, 2, 3, ... in a shift register and stops as soon "
           "as the total is >= Limit. I32 indicators `N` (the last number added) and `Total`.",
           [({"Limit": 50}, {"N": 10, "Total": 55}), ({"Limit": 1}, {"N": 1, "Total": 1})]),
    "B6": ("Squares.vi", "I32 control `Count`; indicator `Squares`: 1D array of I32 with i*i for i = 0 .. Count-1, "
           "built by a For Loop (auto-indexing output tunnel).",
           [({"Count": 5}, {"Squares": [0, 1, 4, 9, 16]})]),
    "B7": ("Point.vi", "Control `Point`: a cluster of two DBL named `x` and `y`. DBL indicator `Distance` = "
           "sqrt(x^2 + y^2) (use Unbundle By Name), and indicator `Swapped`: the same cluster type with x and y "
           "exchanged (use Bundle By Name).",
           [({"Point": [3.0, 4.0]}, {"Distance": 5.0, "Swapped": [4.0, 3.0]})]),
    "B8": ("SumOfSquares.vi", "First build a subVI `Square.vi` (DBL control `x`, DBL indicator `x^2`, both on its "
           "connector pane) and save it next to the main VI (same folder). Then the main VI: DBL controls `a` and "
           "`b`, DBL indicator `Result` = a^2 + b^2, using Square.vi twice as a subVI.",
           [({"a": 3.0, "b": 4.0}, {"Result": 25.0})]),
    "B9": ("FileRoundTrip.vi", "Path control `File Path` and string control `Text`: write Text to that file (Write "
           "to Text File, replacing the file), read the file back (Read from Text File) and show it in string "
           "indicator `Read Back`. Also an error out indicator `error out`.",
           [({"File Path": "@TMP@/bugbash_roundtrip.txt", "Text": "line one\nline two"},
             {"Read Back": "line one\nline two"})]),
    "B10": ("Range.vi", "DBL controls `Value`, `Low`, `High`; boolean indicator `In Range` (Low <= Value <= High) "
            "and DBL indicator `Coerced` (Value limited to Low..High). In Range and Coerce does both.",
            [({"Value": 5.0, "Low": 0.0, "High": 10.0}, {"In Range": True, "Coerced": 5.0}),
             ({"Value": 12.0, "Low": 0.0, "High": 10.0}, {"In Range": False, "Coerced": 10.0})]),
    "B11": ("SineTable.vi", "I32 control `Points` (default 100). A For Loop computes sin(2*pi*i/Points) for "
            "i = 0 .. Points-1 into a 1D DBL array shown on indicator `Samples` and on a Waveform Graph `Sine`.",
            [({"Points": 8}, {"Samples": [math.sin(2 * math.pi * i / 8) for i in range(8)]})]),
    "B12": ("Format.vi", "DBL control `Value`; string indicator `Text` = \"Value = \" followed by Value with two "
            "decimals (Format Into String, format %.2f), e.g. \"Value = 3.14\".",
            [({"Value": 3.14159}, {"Text": "Value = 3.14"})]),
    "B13": ("DotProduct.vi", "Controls `A` and `B` (1D arrays of DBL); DBL indicator `Dot` = sum of A[i]*B[i].",
            [({"A": [1.0, 2.0, 3.0], "B": [4.0, 5.0, 6.0]}, {"Dot": 32.0})]),
    "B14": ("C2F_K.vi", "Open the existing VI @SRC@ (it converts Celsius to Fahrenheit) and add a DBL indicator "
            "`Kelvin` = Celsius + 273.15, keeping everything else. Save it to the path below.",
            [({"Celsius": 100.0}, {"Fahrenheit": 212.0, "Kelvin": 373.15})]),
    "B15": ("Shape.vi", "String control `Shape` and DBL control `Size`; DBL indicator `Area`. A Case Structure on "
            "Shape: \"square\" = Size^2, \"circle\" = pi * Size^2, default case = -1.",
            [({"Shape": "square", "Size": 3.0}, {"Area": 9.0}), ({"Shape": "circle", "Size": 1.0}, {"Area": math.pi}),
             ({"Shape": "hexagon", "Size": 2.0}, {"Area": -1.0})]),
    "B16": ("TimesTable.vi", "I32 control `Size`; indicator `Table`: a 2D array of I32 with Table[i][j] = (i+1)*(j+1) "
            "for i, j = 0 .. Size-1, made with two nested For Loops.",
            [({"Size": 3}, {"Table": [[1, 2, 3], [2, 4, 6], [3, 6, 9]]})]),
    "B17": ("Fibonacci.vi", "I32 control `Count`; indicator `Sequence`: 1D array of I32 with the first Count "
            "Fibonacci numbers 0, 1, 1, 2, 3, 5, ... (a For Loop with two shift registers).",
            [({"Count": 8}, {"Sequence": [0, 1, 1, 2, 3, 5, 8, 13]})]),
    "B18": ("BestScore.vi", "Control `Players`: a 1D array of clusters, each with a string `Name` and a DBL `Score`. "
            "String indicator `Best` = the Name with the highest Score, DBL indicator `Top Score` = that score.",
            [({"Players": [["ann", 3.0], ["bob", 7.5], ["cy", 5.0]]}, {"Best": "bob", "Top Score": 7.5})]),
    "B19": ("CountTrue.vi", "Control `Flags` (1D array of booleans); I32 indicator `Count` = how many are TRUE.",
            [({"Flags": [True, False, True, True]}, {"Count": 3}), ({"Flags": []}, {"Count": 0})]),
    "B20": ("Elapsed.vi", "I32 control `Delay ms`. Read the millisecond Tick Count, wait Delay ms (Wait (ms)), read "
            "it again; U32 indicator `Elapsed` = the difference. Force the order with a Flat Sequence Structure "
            "(three frames) or with data flow.",
            [({"Delay ms": 120}, {"Elapsed": "range:110:400"})]),
    "B21": ("Averages.vi", "First build a subVI `Mean2.vi` (DBL controls `a`, `b`, DBL indicator `mean` = (a+b)/2, "
            "all on its connector pane) and save it in the same folder as the main VI. Main VI: controls `A` and "
            "`B` (1D DBL arrays), indicator `Means` (1D DBL array) = Mean2 of the elements pairwise, by calling "
            "Mean2.vi inside a For Loop with auto-indexing tunnels.",
            [({"A": [1.0, 2.0, 3.0], "B": [3.0, 4.0, 5.0]}, {"Means": [2.0, 3.0, 4.0]})]),
    "B22": ("Duration.vi", "Control `Y` (1D DBL array) and DBL control `dt`. Make a waveform from them (Build "
            "Waveform with Y and dt) shown on waveform indicator `Signal`; DBL indicator `Duration` = dt * number "
            "of samples, taken from the waveform (Get Waveform Components for Y and dt).",
            [({"Y": [1.0, 2.0, 3.0, 4.0], "dt": 0.5}, {"Duration": 2.0})]),
    # front-panel layout tasks (LAYOUT below adds panel checks)
    "B23": ("SignalPanel.vi", "A signal generator for an operator. DBL controls `Frequency` and `Amplitude`, I32 "
            "control `Samples`; a Waveform Graph `Signal` showing the 1D DBL array "
            "Amplitude * sin(2*pi*Frequency*i/Samples) for i = 0 .. Samples-1; DBL indicator `Peak` = the largest "
            "value of that array. Front panel: a title \"Signal Generator\" at the top, the three inputs together "
            "on the left, the graph large (about 500 x 300 pixels) to the right of them, Peak below the graph; "
            "nothing overlapping, the window sized to the panel.",
            [({"Frequency": 5.0, "Amplitude": 2.0, "Samples": 100}, {"Peak": 2.0})]),
    "B24": ("Thermostat.vi", "DBL controls `Temperature`, `Setpoint` and `Hysteresis`; boolean indicators "
            "`Heater On` (Temperature < Setpoint - Hysteresis) and `Too Hot` (Temperature > Setpoint + Hysteresis). "
            "Front panel: Temperature at the top; below it a tab control whose first page holds Setpoint and "
            "Hysteresis and whose second page holds the two indicators; nothing overlapping, window sized to the "
            "panel.",
            [({"Temperature": 18.0, "Setpoint": 21.0, "Hysteresis": 1.0}, {"Heater On": True, "Too Hot": False}),
             ({"Temperature": 25.0, "Setpoint": 21.0, "Hysteresis": 1.0}, {"Heater On": False, "Too Hot": True})]),
    "B25": ("C2F_Panel.vi", "Open the existing VI @SRC@ (it converts Celsius to Fahrenheit; do not change its "
            "block diagram logic) and make its front panel presentable for a student: a title \"Temperature "
            "Converter\", the input on the left and the output on the right, both wider than now (about 150 px), "
            "nothing overlapping, the window sized to the panel. Save it to the path below.",
            [({"Celsius": 100.0}, {"Fahrenheit": 212.0})]),
}

# front-panel checks per task: title text, a left of b, minimum size, objects on a tab page, wider than
LAYOUT = {
    "B23": {"title": "Signal Generator", "left_of": [("Frequency", "Signal"), ("Amplitude", "Signal"),
                                                     ("Samples", "Signal")],
            "above": [("Signal", "Peak")], "min_size": {"Signal": (430, 250)}},
    "B24": {"on_page": ["Setpoint", "Hysteresis", "Heater On", "Too Hot"], "top_level": ["Temperature"]},
    "B25": {"title": "Temperature Converter", "left_of": [("Celsius", "Fahrenheit")],
            "min_size": {"Celsius": (130, 0), "Fahrenheit": (130, 0)}},
}

PROMPT = """You have a LabVIEW instance available through the `labview` MCP tools (and no other tools).
Build the following VI from scratch with those tools, test it, and save it.

{task}

Requirements:
- Start with new_vi. Use find_object to look up node names.
- Front-panel labels must match the names above exactly.
- Check with errors() that the VI is not broken, run it with run_vi to test it, look at the diagram with get_image.
- Save it with save_vi to exactly this path: {target}
- Finish with a short summary. Then a section "TOOL PROBLEMS:" listing every case where an lvmcp tool failed,
  behaved unexpectedly, or where you needed a workaround (or "none")."""


def tag_dir(tag):
    return ROOT / "build/bugbash" / tag


def run_one(tid, port, tag):
    d = tag_dir(tag)
    name, text, _ = TASKS[tid]
    cache = d / f"gen-{tid}"
    prefix, bprefix = f"bb{tid}_", f"bb{tid}b_"
    t0 = time.time()
    from lvmcp.ops import private_boot_copy
    boot = private_boot_copy(BOOT_SRC, cache / "boot", bprefix)
    warm = subprocess.run([PY, "-m", "lvmcp.selftest", "--port", str(port), "--boot-dir", str(boot), "--boot-prefix",
                           bprefix, "--gen-dir", str(cache), "--prefix", prefix], cwd=ROOT, capture_output=True,
                          text=True, timeout=900)
    (d / f"{tid}.selftest.txt").write_text(warm.stdout + warm.stderr)
    work = Path(tempfile.gettempdir()) / "lvmcp-bugbash" / tag / tid   # outside the repo: no project instructions
    work.mkdir(parents=True, exist_ok=True)
    cfg = {"mcpServers": {"labview": {"type": "stdio", "command": PY, "args": [
        str(ROOT / "lvmcp/mcp_server.py"), "--port", str(port), "--boot-dir", str(boot), "--boot-prefix", bprefix,
        "--gen-dir", str(cache), "--prefix", prefix]}}}
    (work / "mcp.json").write_text(json.dumps(cfg, indent=1))
    target = d / "vis" / tid / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if "@SRC@" in text:                                # task edits an existing VI: start from round 1's result
        src = sorted(ROOT.glob("build/bugbash/*/vis/B1/C2F.vi"))[0]
        shutil.copy(src, target)
        text = text.replace("@SRC@", str(target))
    prompt = PROMPT.format(task=f"VI `{name}`: {text}".replace("@TMP@", str(d)), target=target)
    with open(d / f"{tid}.jsonl", "w") as log:
        p = subprocess.run([shutil.which("claude") or "claude", "-p", prompt, "--tools", "", "--mcp-config", str(work / "mcp.json"),
                            "--strict-mcp-config", "--allowedTools", "mcp__labview", "--no-session-persistence",
                            "--output-format", "stream-json", "--verbose"], cwd=work, stdout=log,
                           stderr=subprocess.STDOUT, timeout=1800)
    msg = f"{tid}: port {port} warm={warm.returncode} claude={p.returncode} {time.time() - t0:.0f} s saved={target.exists()}"
    print(msg, flush=True)
    return tid


def cmd_run(a):
    d = tag_dir(a.tag)
    d.mkdir(parents=True, exist_ok=True)
    ports = [int(x) for x in a.ports.split(",")]
    ids = a.ids or list(TASKS)
    free = list(ports)
    import threading
    lock = threading.Lock()

    def job(tid):
        with lock:
            port = free.pop(0)
        try:
            return run_one(tid, port, a.tag)
        finally:
            with lock:
                free.append(port)

    with ThreadPoolExecutor(len(ports)) as ex:
        list(ex.map(job, ids))


def close(x, y, tol=1e-6):
    if isinstance(y, str) and y.startswith("range:"):
        lo, hi = map(float, y.split(":")[1:])
        return isinstance(x, (int, float)) and lo <= x <= hi
    if isinstance(x, bool) or isinstance(y, bool):
        return x == y
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return abs(x - y) <= tol * max(1.0, abs(x), abs(y))
    if isinstance(x, (list, tuple)) and isinstance(y, (list, tuple)):
        return len(x) == len(y) and all(close(p, q, tol) for p, q in zip(x, y))
    if isinstance(x, str) and isinstance(y, str):
        return x.replace("\r\n", "\n") == y.replace("\r\n", "\n")
    return x == y


def cmd_verify(a):
    from lvmcp.ops import LabVIEW, private_boot_copy
    d = tag_dir(a.tag)
    gen = d / "gen-verify"
    boot = private_boot_copy(BOOT_SRC, gen / "boot", "bbVb_")
    lv = LabVIEW("127.0.0.1", a.port, "", str(boot), str(gen), str(gen), prefix="bbV_", boot_prefix="bbVb_")
    res = {}
    for tid, (name, _, cases) in TASKS.items():
        if (a.ids and tid not in a.ids) or not (d / f"{tid}.jsonl").exists():
            continue
        p = d / "vis" / tid / name
        if not p.exists():
            res[tid] = {"ok": False, "why": "not saved"}
            continue
        try:
            vi = lv.open_vi(str(p))
        except Exception as e:  # noqa: BLE001
            res[tid] = {"ok": False, "why": f"open: {e}"}
            continue
        try:
            err = lv.errors(vi)
            if err.get("broken"):
                res[tid] = {"ok": False, "why": f"broken: {err}"}
                continue
            bad = []
            for inp, exp in cases:
                inp = {k: (v.replace("@TMP@", str(d)) if isinstance(v, str) else v) for k, v in inp.items()}
                try:
                    got = lv.run_vi(vi, inp, list(exp), timeout=30)
                except Exception as e:  # noqa: BLE001
                    bad.append(f"run {inp}: {e}")
                    continue
                for k, v in exp.items():
                    if not close(got.get(k), v):
                        bad.append(f"{inp}: {k} = {got.get(k)!r}, expected {v!r}")
            panel_info = {}
            try:
                bad += check_layout(lv, vi, LAYOUT.get(tid, {}), panel_info)
                (d / "vis" / tid / "panel.png").write_bytes(lv.get_image(vi, "panel"))
            except Exception as e:  # noqa: BLE001
                bad.append(f"panel check: {e}")
            res[tid] = {"ok": not bad, "why": "; ".join(bad)[:600]} | panel_info
        finally:
            lv.close_vi(vi)
    (d / "verify.json").write_text(json.dumps(res, indent=1))
    for tid, r in res.items():
        print(f"{tid}: {'PASS' if r['ok'] else 'FAIL'} {r['why']} (panel: {r.get('overlaps', '?')} overlaps, "
              f"window {r.get('window')})")
    print(f"{sum(r['ok'] for r in res.values())}/{len(res)} pass")


def check_layout(lv, vi, spec, info) -> list[str]:
    """Front-panel checks (every VI: overlaps and window size are recorded; spec: task-specific checks)."""
    ctls = lv.panel(vi, values=False)
    decos = lv.decorations(vi, [pg["h"] for c in ctls for pg in c.get("pages", [])])
    ov = lv.panel_overlaps(ctls + decos)
    pb = lv._get(vi, "VI", "Front Panel Window:Panel Bounds")
    info.update(overlaps=len(ov), window=[pb[2] - pb[0], pb[3] - pb[1]])
    if not spec:
        return []
    bad = [f"overlapping: {ov[:4]}"] if ov else []
    by = {c["label"]: c for c in ctls}
    for k in [x for pair in spec.get("left_of", []) + spec.get("above", []) for x in pair] + \
            list(spec.get("min_size", {})) + spec.get("on_page", []) + spec.get("top_level", []):
        if k not in by:
            return bad + [f"no panel object {k!r}"]
    if "title" in spec and not any(spec["title"].lower() in (t.get("text") or "").lower() for t in decos):
        bad.append(f"no title {spec['title']!r}")
    for a_, b_ in spec.get("left_of", []):
        if by[a_].get("page") == by[b_].get("page") and by[a_]["pos"][0] + by[a_]["size"][0] > by[b_]["pos"][0]:
            bad.append(f"{a_} not left of {b_}")
    for a_, b_ in spec.get("above", []):
        if by[a_]["pos"][1] + by[a_]["size"][1] > by[b_]["pos"][1]:
            bad.append(f"{a_} not above {b_}")
    for k, (w, h) in spec.get("min_size", {}).items():
        if by[k]["size"][0] < w or by[k]["size"][1] < h:
            bad.append(f"{k} size {by[k]['size']} < {[w, h]}")
    for k in spec.get("on_page", []):
        if not by[k].get("page"):
            bad.append(f"{k} not on a tab page")
    for k in spec.get("top_level", []):
        if by[k].get("page"):
            bad.append(f"{k} on a tab page")
    if pb[2] <= pb[0]:
        bad.append("window size not set")
    return bad


def transcript(p: Path):
    """(tool calls, [(tool, error text)], final text, cost, seconds)"""
    calls, errs, final, cost, secs, names = 0, [], "", None, None, {}
    for line in p.read_text().splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("type") == "assistant":
            for c in m["message"].get("content", []):
                if c.get("type") == "tool_use":
                    calls += 1
                    names[c["id"]] = (c["name"].split("__")[-1], json.dumps(c.get("input"))[:200])
        elif m.get("type") == "user":
            for c in m["message"].get("content", []) if isinstance(m["message"].get("content"), list) else []:
                if c.get("type") == "tool_result" and c.get("is_error"):
                    t = c.get("content")
                    t = " ".join(x.get("text", "") for x in t) if isinstance(t, list) else str(t)
                    errs.append((names.get(c["tool_use_id"], ("?", ""))[0], names.get(c["tool_use_id"], ("", ""))[1], t[:400]))
        elif m.get("type") == "result":
            final, cost, secs = m.get("result", ""), m.get("total_cost_usd"), (m.get("duration_ms") or 0) / 1000
    return calls, errs, final, cost, secs


def cmd_report(a):
    d = tag_dir(a.tag)
    ver = json.loads((d / "verify.json").read_text()) if (d / "verify.json").exists() else {}
    out = [f"# Bug bash {a.tag}\n"]
    tot = 0.0
    for tid in TASKS:
        p = d / f"{tid}.jsonl"
        if not p.exists():
            continue
        calls, errs, final, cost, secs = transcript(p)
        tot += cost or 0
        v = ver.get(tid, {})
        out.append(f"## {tid} {TASKS[tid][0]}: {'PASS' if v.get('ok') else 'FAIL ' + v.get('why', '?')}")
        out.append(f"{calls} tool calls, {len(errs)} tool errors, ${cost or 0:.2f}, {secs or 0:.0f} s")
        for tool, inp, e in errs:
            out.append(f"- `{tool}` {inp}\n  -> {e}")
        tp = final.split("TOOL PROBLEMS:", 1)[-1].strip() if "TOOL PROBLEMS:" in final else "(no section)"
        out.append(f"\nTOOL PROBLEMS (agent): {tp[:1500]}\n")
    out.append(f"total ${tot:.2f}")
    (d / "report.md").write_text("\n".join(out))
    print("\n".join(out))


ap = argparse.ArgumentParser()
ap.add_argument("cmd", choices=("run", "verify", "report"))
ap.add_argument("ids", nargs="*")
ap.add_argument("--ports", default="5577,5580,5581,5582")
ap.add_argument("--port", type=int, default=5577)
ap.add_argument("--tag", default="r1")
ap.add_argument("--boot-src", default=str(ROOT / "build/lv2022/boot"), help="boot wrappers of this LabVIEW "
                "(labview/boot for the packaged 14.0 ones)")
a = ap.parse_args()
BOOT_SRC = a.boot_src
sys.path.insert(0, str(ROOT))
{"run": cmd_run, "verify": cmd_verify, "report": cmd_report}[a.cmd](a)
