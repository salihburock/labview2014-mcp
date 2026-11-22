"""Palette table: what users/LLMs call LabVIEW things -> how to create them by VI Scripting.

The table is generated from the LabVIEW install that the server runs in (so it covers that machine's drivers
and toolkits, e.g. NI-USRP), and cached per install (directory + version) in the Scripting cache folder.

How it is built (see docs/palette.md):
  * The Functions palette is a tree of `.mnu` files (LabVIEW resource files, RSRC/LMNU) starting at
    `<menus>/default/root.mnu`. They are read **through LabVIEW** (`List Folder` + `Read from Binary File`
    wrappers), because the client may not see LabVIEW's disk (Windows VM), and parsed here in Python.
  * PLM2 resource = palette items. A built-in item (function, structure, constant) is stored with its
    **New VI Object style ID**; a VI item with an index into the LPTH resource (folder / .llb / .lvlib) plus
    its file name in the CPST resource; a subpalette with an LPTH index of another .mnu (or a folder).
    Palettes with a PATH resource are synchronized with that folder (extra .mnu/.vi/.llb/subfolders appear).
  * The New VI Object style table (Scripting.styles()) adds every creatable built-in object, also those that
    are not on any palette.
  * Quick Drop shortcuts (LabVIEW.ini / labview.conf `QuickDropDiagramShortcuts`, LabVIEW's default shortcut
    file, or a built-in copy of the default list) and the palette search keywords
    (`menus/menuKeyword/<language>/*.xml`) give extra names.
  * Optionally each item is dropped into a scratch VI to read its terminals (name, direction, data type).

Table format (JSON): see `Palette.build` and docs/palette.md.
"""
from __future__ import annotations

import difflib
import hashlib
import html
import json
import re
import struct
import time
from pathlib import Path

from .lvclient import LVError, Variant
from .scripting import Scripting, Wrapper, lv_join, STYLE_PROPERTY_NODE

FORMAT = 1

# ----------------------------------------------------------------------------------------- RSRC / .mnu parsing


def rsrc_blocks(b: bytes) -> dict:
    """LabVIEW resource file -> {type: [(section id, section name, data bytes)]}."""
    if b[:6] != b"RSRC\r\n":
        raise ValueError("not a LabVIEW resource file")
    _ver, _ftype, _creator, roff, _rsize, doff, _dsize = struct.unpack_from(">H4s4sIIII", b, 6)
    _i1, _i2, _i3, bioff, bisize = struct.unpack_from(">IIIII", b, roff + 32)
    base = roff + bioff
    names_at = roff + bisize
    out = {}
    for i in range(struct.unpack_from(">I", b, base)[0] + 1):
        ident, cnt, off = struct.unpack_from(">4sII", b, base + 4 + 12 * i)
        secs = []
        for k in range(cnt + 1):
            sid, nameoff, _x, dataoff = struct.unpack_from(">iIII", b, base + off + 20 * k)
            name = None
            if nameoff != 0xFFFFFFFF and names_at + nameoff < len(b):
                j = names_at + nameoff
                name = b[j + 1:j + 1 + b[j]].decode("latin-1")
            sz = struct.unpack_from(">I", b, doff + dataoff)[0]
            secs.append((sid, name, b[doff + dataoff + 4:doff + dataoff + 4 + sz]))
        out[ident.decode("latin-1")] = secs
    return out


def _pstr(b: bytes, i: int):
    n = b[i]
    return b[i + 1:i + 1 + n].decode("latin-1"), i + 1 + n


def _path_at(d: bytes, i: int):
    """PTH0 record -> ((type, parts) or None, index after it)."""
    tag = d[i:i + 4]
    sz = struct.unpack_from(">I", d, i + 4)[0]
    body = d[i + 8:i + 8 + sz]
    if tag != b"PTH0" or len(body) < 4:
        return None, i + 8 + sz
    t, c = struct.unpack_from(">HH", body, 0)
    k, parts = 4, []
    for _ in range(c):
        s, k = _pstr(body, k)
        parts.append(s)
    return (t, parts), i + 8 + sz


def _strlist(d: bytes):
    n = struct.unpack_from(">I", d, 0)[0]
    i, out = 4, []
    for _ in range(n):
        s, i = _pstr(d, i)
        out.append(s)
    return out


def parse_mnu(b: bytes) -> dict:
    """Parse a palette file. Uses the section with id -1 (the Functions part of root.mnu) or the first one.
    Returns {"name", "desc", "items": [(id, kind, lpth index, record name, display/file name)],
    "paths": [(type, parts) | None], "sync": (type, parts) | None}."""
    bl = rsrc_blocks(b)

    def get(t):
        secs = bl.get(t) or []
        for sid, nm, d in secs:
            if sid == -1:
                return nm, d
        return (secs[0][1], secs[0][2]) if secs else (None, None)

    name, plm2 = get("PLM2")
    if plm2 is None:
        raise ValueError("no PLM2 resource")
    _ver, n = struct.unpack_from(">HH", plm2, 0)
    i, recs = 4, []
    for _ in range(n):
        sid, kind, idx = struct.unpack_from(">HHI", plm2, i)
        i += 8
        s, i = _pstr(plm2, i)
        if (1 + len(s)) % 2:
            i += 1
        recs.append((sid, kind, idx, s))
    _, lpth = get("LPTH")
    paths = []
    if lpth is not None:
        j = 4
        for _ in range(struct.unpack_from(">I", lpth, 0)[0]):
            p, j = _path_at(lpth, j)
            paths.append(p)
    _, cpst = get("CPST")
    names = _strlist(cpst) if cpst is not None else []
    _, lst = get("LST#")
    desc = ""
    if lst is not None and len(lst) >= 8:
        try:
            desc = _strlist(lst[4:])[0] if struct.unpack_from(">I", lst, 0)[0] else ""
        except (IndexError, struct.error):
            desc = ""
    _, pth = get("PATH")
    sync = _path_at(pth, 0)[0] if pth else None
    items = [r + (names[k] if k < len(names) else "",) for k, r in enumerate(recs)]
    return {"name": name or "", "desc": desc, "items": items, "paths": paths, "sync": sync}


# ----------------------------------------------------------------------------------------- paths
# A LabVIEW path is kept as (type, tuple(parts)) exactly as LabVIEW flattens it: type 0 absolute (Linux:
# ("usr", "local", ...); Windows: ("C", "Program Files", ...)), type 3 UNC (first part "\\\\server\\share").
# lvclient.flatten_path accepts {"type", "parts"} dicts, so paths go back to LabVIEW without any separator
# guessing. Symbolic forms (<vilib>/...) are kept for display and portability.

SYMBOLS = {"<vilib>": "vi.lib", "<menus>": "menus", "<instrlib>": "instr.lib", "<userlib>": "user.lib",
           "<helpdir>": "help", "<resource>": "resource", "<examples>": "examples", "<labview>": ""}


def path_str(p) -> str:
    t, parts = p
    if not parts:
        return ""
    if t == 3:
        return "\\".join(parts)
    if len(parts[0]) == 1 or parts[0].endswith(":"):        # Windows drive
        return parts[0].rstrip(":") + ":\\" + "\\".join(parts[1:])
    return "/" + "/".join(parts)


def lvpath(p) -> dict:
    return {"type": p[0], "parts": list(p[1])}


def _ext(name: str) -> str:
    m = re.search(r"\.[A-Za-z0-9]+$", name)
    return m.group(0).lower() if m else ""


def _stem(name: str) -> str:
    return name[: -len(_ext(name))] if _ext(name) in (".vi", ".vim", ".ctl", ".xnode", ".llb", ".mnu", ".lvlib",
                                                      ".lvclass", ".vit") else name


# ----------------------------------------------------------------------------------------- LabVIEW-side files

class LVFiles:
    """File access through the LabVIEW server (the client may not see LabVIEW's disk)."""

    def __init__(self, S: Scripting):
        self.S = S
        self.c = S.c
        self._w = {}
        self.cache = {}
        info = S.wrapper("prop", "Application", ("App.Dir", "App.Version"), skip=("reference",))
        r = run_named(info, {"error in (no error)": [False, 0, ""]}, ["App.Dir", "App.Version"])
        if r["error out"][0]:
            raise LVError(r["error out"][1], r["error out"][2])
        d = r["App.Dir"]
        self.dir = (d["type"], tuple(d["parts"]))
        self.version = r["App.Version"]

    # ---- wrappers (custom builds: inputs wired before outputs, so outputs get their final types)
    def _reader(self) -> Wrapper:
        if "read" not in self._w:
            st = self.S.styles()

            def u8_type(S, vi, dst):    # data type = U8: a "To Unsigned Byte Integer" output feeds the type
                n = S.new_object(S.block_diagram(vi), st["To Unsigned Byte Integer"], 150, 200)
                terms = S.node_info(n)[0]
                ins = [t for t in terms if not S.term_is_output(t)]
                outs = [t for t in terms if S.term_is_output(t)]
                S.create_constant(ins[0])
                S.connect(outs[0], dst)
            self._w["read"] = custom_wrapper(
                self.S, "pal_readbytes", st["Read from Binary File"], consts={"data type": u8_type},
                skip=("prompt (Open existing file)", "cancelled", "refnum out",
                      "byte order (0:big-endian, network order)"))
        return self._w["read"]

    def _lister(self) -> Wrapper:
        if "list" not in self._w:
            st = self.S.styles()
            style = st.get("List Folder") or st["List Directory"]
            # datalog type must stay unwired: wired, List Folder returns only datalog files of that type
            self._w["list"] = custom_wrapper(self.S, "pal_listfolder", style, skip=("datalog type",))
        return self._w["list"]

    # ---- operations
    def read(self, p) -> bytes:
        """Whole file as bytes. Never call with an empty path: the function would open a file dialog."""
        if not p[1]:
            raise ValueError("empty path")
        key = ("r", p)
        if key not in self.cache:
            w = self._reader()
            r = run_named(w, {"file (use dialog)": lvpath(p), "count": -1}, ["data"])
            if r["error out"][0]:
                self.cache[key] = LVError(r["error out"][1], r["error out"][2])
            else:
                self.cache[key] = bytes(r["data"])
        v = self.cache[key]
        if isinstance(v, Exception):
            raise v
        return v

    def listdir(self, p):
        """(files, folders) of a folder or .llb; ([], []) if it does not exist."""
        if not p[1]:
            raise ValueError("empty path")
        key = ("l", p)
        if key not in self.cache:
            w = self._lister()
            r = run_named(w, {"path": lvpath(p), "pattern": ""}, ["filenames", "folder names"])
            self.cache[key] = ([], []) if r["error out"][0] else (r["filenames"], r["folder names"])
        return self.cache[key]

    def exists(self, p) -> bool:
        parent = (p[0], p[1][:-1])
        files, folders = self.listdir(parent)
        return p[1][-1] in files or p[1][-1] in folders

    # ---- symbolic paths
    def resolve(self, rel, base=None):
        """LPTH/PATH entry (type, parts) -> absolute (type, parts). base: the file it is relative to."""
        if rel is None:
            return None
        t, parts = rel
        if t == 1:                                   # relative to the file: '' = up one level
            if base is None:
                return None
            out = list(base[1])
            for x in parts:
                if x == "":
                    out = out[:-1]
                else:
                    out.append(x)
            return (base[0], tuple(out))
        if parts and parts[0].startswith("<") and parts[0].endswith(">"):
            sub = SYMBOLS.get(parts[0].lower())
            if sub is None:
                return None
            return (self.dir[0], self.dir[1] + ((sub,) if sub else ()) + tuple(parts[1:]))
        return (t, tuple(parts))

    def sym(self, p) -> str:
        """Absolute path -> '<vilib>/...' style when inside the LabVIEW folder."""
        n = len(self.dir[1])
        if p[0] == self.dir[0] and p[1][:n] == self.dir[1]:
            rest = p[1][n:]
            for k, v in SYMBOLS.items():
                if v and rest[:1] == (v,):
                    return "/".join((k,) + rest[1:])
            return "/".join(("<labview>",) + rest)
        return path_str(p)


def lab(w: Wrapper, name: str) -> str:
    """Front-panel label of the control/indicator on node terminal `name` (labels differ between LabVIEW
    versions, e.g. "count (1)" for terminal "count")."""
    for t in w.terms:
        if t["name"] == name:
            return t["label"]
    return name


def run_named(w: Wrapper, inputs: dict, outs) -> dict:
    """Wrapper.run with terminal names instead of labels; returns {name: value} plus "error out"
    (the error out of the wrapped node, whatever its label is)."""
    eo = lab(w, "error out")
    vals = {lab(w, k): v if isinstance(v, Variant) else w.template(lab(w, k)).with_value(v)
            for k, v in inputs.items()}
    r = w.c.call(w.path, vals, [lab(w, o) for o in outs] + [eo], check=False)
    if r["__error__"].get("status"):
        raise LVError(r["__error__"].get("code"), r["__error__"].get("source"))
    out = {o: r[lab(w, o)].value for o in outs}
    out["error out"] = r[eo].value
    return out


def custom_wrapper(S: Scripting, tag: str, style: int, consts=None, skip=()) -> Wrapper:
    """Wrapper VI around one New VI Object `style`, built like Scripting.wrapper but
    * constants first (consts: {terminal: Variant or callable(S, vi, terminal)}),
    * then controls on all inputs, then indicators on all outputs (so output types are final),
    * cached in the Scripting index under the file name prefix+tag.vi (prim wrappers can't vary `skip`)."""
    consts = consts or {}
    name = S.prefix + tag + ".vi"
    path = lv_join(S.gen, name)
    if not S.has(name):
        vi = S.new_vi()
        try:
            node = S.new_object(S.block_diagram(vi), style, 300, 200)
            terms, sname = S.node_info(node)
            expect = {v: k for k, v in S.styles().items()}.get(style)
            if expect and sname != expect:
                raise RuntimeError(f"building {tag}: New VI Object made a {sname!r}, expected {expect!r}")
            names = [S.term_name(t) for t in terms]
            dirs = ["out" if S.term_is_output(t) else "in" for t in terms]
            for t, n in zip(terms, names):
                if n in consts:
                    k = consts[n]
                    k(S, vi, t) if callable(k) else S.create_constant(t, k)
            info = []
            for want in ("in", "out"):
                for t, n, d in zip(terms, names, dirs):
                    if d != want or n in skip or n in consts:
                        continue
                    ref, err = (S.create_control if d == "in" else S.create_indicator)(t, check=False)
                    if err[0]:
                        raise LVError(err[1], err[2])
                    info.append({"name": n, "dir": d, "label": S.label_of(ref)})
            S.save_new(vi, path)
        finally:
            S.close(vi)
        S.index[name] = {"kind": "custom", "class": "", "item": tag, "write": False, "terms": info}
        S.save_index()
    w = Wrapper(S.c, path)
    w.terms = S.index[name]["terms"]
    w.peek = S.peek
    return w


def subvi_dropper(S: Scripting) -> Wrapper:
    """Wrapper: New VI Object with `vi object class` = SubVI and `path` wired -> drops a subVI node.
    The SubVI class comes from a SubVI refnum constant (made with Create Constant on the reference input of a
    SubVI property node; that node's error out gets an indicator so it can't trigger automatic error handling)."""
    def subvi_class(S, vi, dst):
        pn = S.new_object(S.block_diagram(vi), STYLE_PROPERTY_NODE, 100, 320)
        S.config_property(pn, "SubVI", ["ClassName"])
        terms = S.node_info(pn)[0]
        names = [S.term_name(t) for t in terms]
        k = S.create_constant(terms[names.index("reference")])
        w = S.wrapper("prop", "Constant", "Terminal")
        S.connect(w.run({"reference": k}, [w.item])[w.item], dst)
        S.create_indicator(terms[names.index("error out")])
    return custom_wrapper(S, "pal_subvi_drop", S.styles()["New VI Object"], consts={"vi object class": subvi_class},
                          skip=("bounds", "auto wire? (F)", "style"))


# ----------------------------------------------------------------------------------------- type names

def type_str(v) -> str:
    """Short text for the type of a Variant (terminal Data Type)."""
    def td_s(td, depth=0):
        d = td.describe()
        if td.code == 0xF1 and td.items:
            return td_s(td.items[0], depth)
        if d.startswith("enum") and td.names:
            return f"{d}{{{','.join(td.names[:8])}{',...' if len(td.names) > 8 else ''}}}"
        if d.startswith("array"):
            return f"{d}<{td_s(td.items[0], depth + 1)}>" if td.items else d
        if d == "cluster":
            labels = [x.label for x in td.items]
            if labels[:3] == ["status", "code", "source"]:
                return "error"
            if depth > 1:
                return "cluster"
            return "cluster{" + ",".join(f"{x.label}:{td_s(x, depth + 1)}" if x.label else td_s(x, depth + 1)
                                         for x in td.items) + "}"
        return d
    try:
        return td_s(v.td)
    except Exception:            # noqa: BLE001  unknown/odd type descriptors
        return "?"


# ----------------------------------------------------------------------------------------- default shortcuts
# LabVIEW's default Quick Drop diagram shortcuts (from LabVIEW 2022's
# resource/dialog/QuickDrop/Default QD Object Shortcuts (en).txt), used when the install has neither its own
# defaults file nor a QuickDropDiagramShortcuts entry in its ini file.
DEFAULT_QD = (
    "-inf:Negative Infinity;1dg:One Button Dialog;2as:Matrix Size;2dg:Two Button Dialog;"
    "3dg :Three Button Dialog.vi;aae:Add Array Elements;ac:Array Constant;ad1:Increment;as:Array Size;"
    "asb:Array Subset;atch:Match Pattern;athc:Path Constant;ats:Array To Spreadsheet String;"
    "avg:Mean.vi [NI_AALBase.lvlib];ba:Build Array;bath:Build Path;bbn:Bundle By Name;car:Compound Arithmetic;"
    "cbc:Color Box Constant;cbr:Call By Reference;cc:Cluster Constant;ccat:Concatenate Strings;"
    "cds:Conditional Disable Structure;cerr:Clear Errors.vi;cf:Close File;cmd:System Exec.vi;"
    "cr:Close Reference;crf:Open/Create/Replace File;cs:Case Structure;csc:Class Specifier Constant;"
    "cv:Current VI's Path;dbl:To Double Precision Float;dc:DBL Numeric Constant;dds:Diagram Disable Structure;"
    "dec:Decrement;dfa:Delete From Array;dq:Dequeue Element;dv:Divide;ea:Empty Array?;eath:Empty Path Constant;"
    "ec:Error Cluster Constant;ecf:Error Cluster From Error Code.vi;eq:Equal?;eqz:Equal To 0?;er:Error Ring;"
    "erg:Merge Errors;ess:Less?;esseq:Less Or Equal?;esseqz:Less Or Equal To 0?;essz:Less Than 0?;"
    "estr:Empty String/Path?;estrc:Empty String Constant;evstr:Event Structure;ext:To Extended Precision Float;"
    "fb:Feedback Node;fc:False Constant;fca:First Call?;fs:For Loop;fss:Flat Sequence Structure;"
    "fstr:Format Into String;fxp:To Fixed-Point;geq:Greater Or Equal?;geqz:Greater Or Equal To 0?;gr:Greater?;"
    "grz:Greater Than 0?;i16:To Word Integer;i32:To Long Integer;i64:To Quad Integer;i8:To Byte Integer;"
    "inf:Positive Infinity;mod:Quotient & Remainder;na:Initialize Array;nae:And Array Elements;"
    "nan:Not A Number/Path/Refnum?;nanc:Not A Number Constant;nc:Numeric Constant;ndx:Index Array;"
    "neq:Not Equal?;neqz:Not Equal To 0?;nes:In Place Element Structure;nq:Enqueue Element;nt:Not;"
    "nv:Invoke Node;qr:Quotient & Remainder;ra:Reverse 1D Array;ras:Replace Array Subset;"
    "rbf:Read from Binary File;rn:Property Node;rndd:Round Toward -Infinity;rndu:Round Toward +Infinity;"
    "rtf:Read from Text File;s1d:Search 1D Array;sath:Strip Path;sav:Select a VI...;sb:Subtract;"
    "sc:String Constant;se:Select;seng:String Length;serr:Simple Error Handler.vi;"
    "sgl:To Single Precision Float;sqr:Square Root;srt:Sort 1D Array;sss:Search/Split String;"
    "sta:Spreadsheet String To Array;svr:Static VI Reference;tc:True Constant;tcase:To Lower Case;"
    "tct:Tick Count (ms);tra:Transpose 2D Array;tsc:To More Specific Class;tvar:To Variant;"
    "u16:To Unsigned Word Integer;u32:To Unsigned Long Integer;u64:To Unsigned Quad Integer;"
    "u8:To Unsigned Byte Integer;ubn:Unbundle By Name;vae:Add Array Elements;vc:Variant Constant;"
    "vr:Open VI Reference;vsr:VI Server Reference;vtd:Variant To Data;wat:Wait (ms);"
    "wbf:Write to Binary File;wc:Waveform Constant;ws:While Loop;wtf:Write to Text File;x:Multiply")


def parse_qd(text: str) -> dict:
    """'QuickDropDiagramShortcuts="a:Name;b:Name2"' (or just the value) -> {shortcut: name}."""
    m = re.search(r'QuickDropDiagramShortcuts\s*=\s*"?([^"\r\n]*)', text)
    val = m.group(1) if m else text
    out = {}
    for pair in val.split(";"):
        if ":" in pair:
            k, v = pair.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def parse_keywords(text: str) -> dict:
    """menuKeyword XML -> {object name: [keywords]} (regex based: the files are simple and not always valid XML)."""
    out = {}
    for m in re.finditer(r'<object\s+name="([^"]*)"\s*>(.*?)</object>', text, re.S):
        kws = [html.unescape(k) for k in re.findall(r'<keyword\s+name="([^"]*)"', m.group(2))]
        out.setdefault(html.unescape(m.group(1)), []).extend(kws)
    return out


# ----------------------------------------------------------------------------------------- the table


# Built-in objects never dropped by the terminal scan: MathScript Node hangs LabVIEW 2022 for > 60 s when
# MathScript is not installed; Global Variable creates a new untitled global VI; decorations have no terminals.
NO_DROP = re.compile(r"MathScript|^Global Variable$")
NO_DROP_OTYPES = ("Label", "Decoration")
# folders not shown as palettes when a palette is synthesized from a folder
SKIP_DIRS = ("private", "examples", "subvis", "support", "typedefs")


class Palette:
    """Build / load the palette table of the LabVIEW the server runs in, and look things up in it."""

    def __init__(self, S: Scripting, cache_dir: str | Path | None = None):
        self.S = S
        self.cache_dir = Path(cache_dir) if cache_dir is not None else S.cache
        self.fs = None
        self.table = None
        self.file = None
        self._idx = None

    # ------------------------------------------------------------ cache
    def _cache_file(self, fs: LVFiles) -> Path:
        h = hashlib.sha1(path_str(fs.dir).encode()).hexdigest()[:8]
        return self.cache_dir / f"palette_{re.sub(r'[^A-Za-z0-9.]+', '_', fs.version)}_{h}.json"

    def load(self, rebuild=False, terminals=False, progress=None) -> dict:
        """Load the cached table for this LabVIEW install, building it if missing (or if rebuild)."""
        self.fs = self.fs or LVFiles(self.S)
        self.file = self._cache_file(self.fs)
        if self.file.exists() and not rebuild:
            self.table = json.loads(self.file.read_text())
            self._idx = None
            if self.table.get("format") == FORMAT:
                if terminals:
                    self.scan_terminals(progress=progress)
                return self.table
        old = json.loads(self.file.read_text()) if self.file.exists() else None
        self.build(progress=progress)
        if old and old.get("format") == FORMAT:          # keep terminal scans of the previous table
            def key(it):
                return f"s:{it['style']}" if it["kind"] == "prim" else f"p:{it.get('sym', '').lower()}"
            prev = {key(it): it for it in old["items"]}
            for it in self.table["items"]:
                o = prev.get(key(it))
                if o:
                    for f in ("terminals", "vi_type", "desc", "instance_vis"):
                        if f in o and f not in it:
                            it[f] = o[f]
                    if o.get("kind") in ("polyvi", "express") and it["kind"] == "vi":
                        it["kind"] = o["kind"]
            self.save()
        if terminals:
            self.scan_terminals(progress=progress)
        return self.table

    def save(self):
        self.file.write_text(json.dumps(self.table, indent=0))

    # ------------------------------------------------------------ build
    def build(self, progress=None) -> dict:
        """Walk the Functions palette from root.mnu and build the table.

        table = {"format", "labview": {dir, version, built, build_seconds},
                 "palettes": {sym file: {"name", "path": "A>B", "desc", "also": [...]}},
                 "items": [item], "shortcuts": {sc: name}, "missing": [...]}
        item  = {"name", "kind", "style" | "path"+"sym", "palette": "A>B" | None, "also": [...],
                 "lib", "otype", "flags": [...], "shortcuts": [...], "keywords": [...],
                 later "terminals": [{"name", "dir", "type"}], "vi_type", "desc"}
        kind  = prim (New VI Object style) | vi | vim | polyvi | express | xnode | mergevi | ctl"""
        t0 = time.time()
        self.fs = fs = self.fs or LVFiles(self.S)
        self.file = self._cache_file(fs)
        self.styles = self.S.styles()
        self.style_names = {v: k for k, v in sorted(self.styles.items(), reverse=True)}
        self.items, self.by_key, self.pals, self.missing = [], {}, {}, []
        self.lvlibs = {}
        self.progress = progress
        self._n = 0
        root = fs.resolve((0, ["<menus>", "default", "root.mnu"]))
        self._walk_mnu(root, [], depth=0)
        # every other creatable built-in object
        for name, style in sorted(self.styles.items(), key=lambda kv: kv[1]):
            k = f"s:{style}"
            if k not in self.by_key:
                self._add(k, {"name": name, "kind": "prim", "style": style, "palette": None})
        self._add_names()
        self.table = {"format": FORMAT,
                      "labview": {"dir": path_str(fs.dir), "version": fs.version,
                                  "built": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "build_seconds": round(time.time() - t0, 1)},
                      "palettes": self.pals, "items": self.items, "shortcuts": self.shortcuts,
                      "missing": self.missing}
        self._idx = None
        self.save()
        return self.table

    def _tick(self, what):
        self._n += 1
        if self.progress:
            self.progress(self._n, what)

    def _add(self, key, item, trail=None):
        if key in self.by_key:
            it = self.by_key[key]
            pal = ">".join(trail) if trail else None
            if pal and pal != it.get("palette") and pal not in it.setdefault("also", []):
                it["also"].append(pal)
            return it
        item.setdefault("flags", [])
        self.by_key[key] = item
        self.items.append(item)
        return item

    def _walk_mnu(self, p, trail, depth, name_hint=None):
        """Read one .mnu palette and its subpalettes. Returns the palette's display name (or None)."""
        fs = self.fs
        sym = fs.sym(p)
        if sym in self.pals:
            pal = self.pals[sym]
            t = ">".join(trail + [pal["name"]])
            if t != pal["path"] and t not in pal["also"]:
                pal["also"].append(t)
            return pal["name"]
        if depth > 12:
            return None
        try:
            m = parse_mnu(fs.read(p))
        except (LVError, ValueError, struct.error, IndexError) as e:
            why = "not installed" if getattr(e, "code", None) == 7 else str(e)[:120]
            self.missing.append({"palette": ">".join(trail), "ref": sym, "why": why})
            return None
        self._tick(sym)
        pname = m["name"] or name_hint or _stem(p[1][-1])
        here = trail + [pname] if depth > 0 else []
        pal = self.pals[sym] = {"name": pname, "path": ">".join(here), "desc": m["desc"], "also": []}
        refs = set()
        for sid, kind, idx, rname, fname in m["items"]:
            target = None
            if 0 < idx <= len(m["paths"]):
                target = fs.resolve(m["paths"][idx - 1], p)
            if sid == 0xFFFF:                                       # subpalette
                if target is None:
                    continue
                refs.add(target)
                self._sub(target, here, depth, fname)
            elif sid == 4000 or (sid == 0 and kind == 0):            # "Select a VI...", empty slot
                continue
            elif kind & 0xFF == 5:                                   # VI (InstrUse)
                if target is None or not fname:
                    self.missing.append({"palette": pal["path"], "ref": fname, "why": "no path"})
                    continue
                vp = self._vi_path(target, fname)
                refs.add(vp)
                self._add_vi(vp, fname, here, flags=kind >> 8, lib=target if _ext(target[1][-1]) == ".lvlib"
                             else None)
            elif kind & 0xFF == 1 and rname == "DDOUse":             # controls palette item
                continue
            else:                                                   # built-in object: sid = style ID
                pal_name = fname or rname
                style = sid
                if style not in self.style_names and pal_name in self.styles:
                    style = self.styles[pal_name]                  # (palette and style IDs differ for TDMS)
                # the style table name is what Quick Drop shows; palette (CPST) names are sometimes internal
                # ones ("Open File (New)" = Open/Create/Replace File, "Function" = a Map function)
                sname = self.style_names.get(style, pal_name)
                name = pal_name if re.search(r" #\d+$", sname) else sname     # "While Loop #1"
                item = {"name": name, "kind": "prim", "style": style, "palette": ">".join(here) or None,
                        "otype": rname}
                alias = [x for x in (pal_name, sname) if x != name and x not in ("Function", "InstrUse")]
                if alias:
                    item["alias"] = alias
                it = self._add(f"s:{style}", item, here)
                if style not in self.style_names:
                    it["flags"] = sorted(set(it["flags"]) | {"no_style"})
        if m["sync"] is not None:
            sp = fs.resolve(m["sync"], p)
            if sp is not None:
                self._sync(sp, here, depth, refs | {p})
        return pname

    def _sub(self, target, here, depth, name_hint):
        e = _ext(target[1][-1])
        if e == ".mnu":
            self._walk_mnu(target, here, depth + 1, name_hint)
        elif e == ".lvlib":                    # name_hint = the library's .mnu item, or <UseDefaultMenu>
            lib = self._lvlib(target)
            menu = lib["items"].get(name_hint) if name_hint and name_hint.endswith(".mnu") else lib["menu"]
            if menu:
                self._walk_mnu(menu, here, depth + 1, _stem(target[1][-1]))
            else:
                self.missing.append({"palette": ">".join(here), "ref": self.fs.sym(target),
                                     "why": "not installed" if lib.get("missing") else
                                     f"library palette {name_hint!r} not found"})
        elif e in ("", ".llb"):
            self._walk_dir(target, here, depth + 1, name_hint)
        else:
            self.missing.append({"palette": ">".join(here), "ref": self.fs.sym(target), "why": f"unsupported {e}"})

    def _walk_dir(self, d, trail, depth, name_hint=None):
        """Folder or .llb shown as a palette (synchronized folder)."""
        fs = self.fs
        files, folders = fs.listdir(d)
        if "dir.mnu" in files:
            return self._walk_mnu((d[0], d[1] + ("dir.mnu",)), trail, depth, name_hint or _stem(d[1][-1]))
        for f in files:
            if _ext(f) == ".lvlib":
                menu = self._lvlib((d[0], d[1] + (f,))).get("menu")
                if menu:
                    return self._walk_mnu(menu, trail, depth, name_hint or _stem(d[1][-1]))
        sym = fs.sym(d)
        if sym in self.pals or depth > 6 or not (files or folders):
            return None
        self._tick(sym)
        pname = name_hint if name_hint and _ext(name_hint) not in (".mnu",) else _stem(d[1][-1])
        here = trail + [pname]
        self.pals[sym] = {"name": pname, "path": ">".join(here), "desc": "", "also": [], "synthesized": True}
        self._sync(d, here, depth, {d}, synth=True)
        return pname

    def _sync(self, d, here, depth, refs, synth=False):
        """Add the contents of a synchronized folder that the palette file does not list itself."""
        fs = self.fs
        files, folders = fs.listdir(d)
        for f in sorted(files):
            fp = (d[0], d[1] + (f,))
            e = _ext(f)
            if fp in refs or f.lower() == "dir.mnu" or f.startswith("_"):
                continue
            if e == ".mnu":
                self._walk_mnu(fp, here, depth + 1)
            elif e in (".vi", ".vim", ".xnode"):
                self._add_vi(fp, f, here, flags=0, synth=synth)
            elif e == ".llb":
                self._walk_dir(fp, here, depth + 1)
            elif e == ".lvlib":
                menu = self._lvlib(fp).get("menu")
                if menu and menu not in refs:
                    self._walk_mnu(menu, here, depth + 1, _stem(f))
        for f in sorted(folders):
            fp = (d[0], d[1] + (f,))
            if f.startswith("_") or f.lower() in SKIP_DIRS or fp in refs \
                    or (d[0], d[1] + (f, "dir.mnu")) in refs:
                continue
            self._walk_dir(fp, here, depth + 1)

    def _lvlib(self, p) -> dict:
        """Project library: {"menu": default palette path, "items": {item name: path}}."""
        if p in self.lvlibs:
            return self.lvlibs[p]
        info = {"menu": None, "items": {}}
        self.lvlibs[p] = info
        try:
            text = self.fs.read(p).decode("utf-8", "replace")
        except (LVError, ValueError):
            info["missing"] = True
            return info
        self._tick(self.fs.sym(p))
        for m in re.finditer(r'<Item Name="([^"]+)" Type="[^"]*" URL="([^"]+)"', text):
            name = html.unescape(m.group(1))
            if name not in info["items"]:
                info["items"][name] = self._lib_url(p, html.unescape(m.group(2)))
        m = re.search(r'<Property Name="NI\.Lib\.DefaultMenu"[^>]*>([^<]+)</Property>', text)
        if m:                                 # the name of a .mnu item of the library
            name = html.unescape(m.group(1))
            info["menu"] = info["items"].get(name) or self._lib_url(p, name)
        return info

    def _lib_url(self, lib, url):
        """URL inside a .lvlib: relative to the library file ('..' = up), or '/<vilib>/...'."""
        parts = url.split("/")
        if url.startswith("/") and len(parts) > 1 and parts[1].startswith("<"):
            return self.fs.resolve((0, parts[1:]))
        if url.startswith("/"):
            return None
        out = list(lib[1])
        for x in parts:
            if x == "..":
                out = out[:-1]
            elif x not in ("", "."):
                out.append(x)
        if parts and parts[0] not in ("..", "."):      # plain name: relative to the library's folder
            out = list(lib[1][:-1]) + [x for x in parts if x]
        return (lib[0], tuple(out))

    def _vi_path(self, target, fname):
        e = _ext(target[1][-1])
        if e == ".lvlib":
            p = self._lvlib(target)["items"].get(fname)
            if p is not None:
                return p
            return (target[0], target[1][:-1] + (fname,))
        if e == ".lvclass":
            return (target[0], target[1][:-1] + (fname,))
        return (target[0], target[1] + (fname,))

    def _add_vi(self, vp, fname, here, flags=0, lib=None, synth=False):
        fs = self.fs
        sym = fs.sym(vp)
        e = _ext(fname)
        kind = {".vim": "vim", ".xnode": "xnode", ".ctl": "ctl"}.get(e, "vi")
        it_flags = []
        low = sym.lower()
        if "/express/" in low or re.search(r"(block|source)\.llb/", low) or fname.startswith("Ex_"):
            kind = "express"
        elif "xnode" in low:                  # e.g. Control & Simulation "XNodeFramework/Palette VIs/PID"
            kind = "xnode"
        if flags & 0x02:
            it_flags.append("merge")          # "place VI contents" palette option
            if kind == "vi":
                kind = "mergevi"
        if flags & 0x10:
            it_flags.append("special")
        if synth:
            it_flags.append("synth")          # from a folder without palette file: maybe not really on a palette
        if not fs.exists(vp):
            it_flags.append("missing")        # listed by the palette file but not installed (LabVIEW hides it)
        if kind in ("express", "xnode"):
            it_flags.append("dialog")         # configured by dialogs on drop: don't create blindly
        name = _stem(fname)
        if kind == "express":                 # Ex_Inst_Time Delay.vi -> Time Delay
            name = re.sub(r"^Ex_(Inst_)?|\s*(Express)?\s*Block$", "", name) or name
        item = {"name": name, "kind": kind, "path": path_str(vp), "sym": sym,
                "palette": ">".join(here) or None, "flags": it_flags}
        if lib is not None:
            item["lib"] = lib[1][-1]
        return self._add(f"p:{sym.lower()}", item, here)

    # ------------------------------------------------------------ names: shortcuts, keywords
    def _read_text(self, p):
        try:
            return self.fs.read(p).decode("latin-1")
        except (LVError, ValueError):
            return None

    def _add_names(self):
        fs = self.fs
        sc = {}
        qd_dir = fs.resolve((0, ["<resource>", "dialog", "QuickDrop", "Default QD Object Shortcuts"]))
        files, _ = fs.listdir(qd_dir)
        default = [f for f in files if f.endswith("(en).txt")] or [f for f in files if f.endswith(".txt")]
        if default:
            sc = parse_qd(self._read_text((qd_dir[0], qd_dir[1] + (default[0],))) or "")
        if not sc:
            sc = parse_qd(DEFAULT_QD)
        for ini in ("LabVIEW.ini", "labview.ini", "labview.conf"):      # user's own shortcuts override
            p = (fs.dir[0], fs.dir[1] + (ini,))
            if fs.exists(p):
                txt = self._read_text(p) or ""
                if "QuickDropDiagramShortcuts" in txt:
                    sc.update(parse_qd(txt))
                break
        local = sorted(Path.home().glob("natinst/.config/LabVIEW-*/labview.conf"))  # Linux: client = LV machine
        if local and Path(path_str(fs.dir)).exists():
            ver = fs.version.split(".")[0]
            mine = [x for x in local if x.parent.name.endswith(ver)] or local
            txt = mine[-1].read_text(errors="replace")
            if "QuickDropDiagramShortcuts" in txt:
                sc.update(parse_qd(txt))
        self.shortcuts = sc
        names = self._name_index()
        for k, v in sc.items():
            for it in names.get(v.lower(), [])[:1]:
                it.setdefault("shortcuts", []).append(k)
        # palette search keywords
        kwd = fs.resolve((0, ["<menus>", "menuKeyword", "English"]))
        files, _ = fs.listdir(kwd)
        for f in files:
            if not f.lower().endswith(".xml"):
                continue
            kws = parse_keywords(self._read_text((kwd[0], kwd[1] + (f,))) or "")
            for obj, words in kws.items():
                hits = names.get(obj.lower(), [])
                for it in hits[:1]:
                    # Express VI list: bare names are ambiguous ("Backlash"), so only an unambiguous hit
                    if "express" in f.lower() and it["kind"] in ("vi", "mergevi") and len(hits) == 1:
                        it["kind"] = "express"
                        it["flags"] = sorted(set(it["flags"]) | {"dialog"})
                    if words:
                        key = "instances" if "poly" in f.lower() else "keywords"
                        if key == "instances" and it["kind"] == "vi":
                            it["kind"] = "polyvi"
                        lst = it.setdefault(key, [])
                        lst.extend(w for w in words if w not in lst)
        # a VI with polymorphic instances that the Express keyword list also names (e.g. Butterworth Filter)
        # is a polymorphic VI, not an Express VI, unless its path says Express
        for it in self.items:
            low = (it.get("sym") or "").lower()
            if (it["kind"] == "express" and it.get("instances") and "/express/" not in low
                    and not re.search(r"(block|source)\.llb/", low) and not low.rsplit("/", 1)[-1].startswith("ex_")):
                it["kind"] = "polyvi"
                it["flags"] = [f for f in it["flags"] if f != "dialog"]

    def _name_index(self) -> dict:
        """lower-case name -> [items]; names: display name, style name, file name, 'file [lib]'."""
        idx = {}
        for it in self.items:
            for n in self._names(it):
                lst = idx.setdefault(n.lower(), [])
                if it not in lst:
                    lst.append(it)
        for lst in idx.values():
            lst.sort(key=lambda it: (it.get("palette") is None, it["kind"] != "prim"))
        return idx

    @staticmethod
    def _names(it):
        ns = [it["name"]]
        ns += it.get("alias", [])
        if it.get("sym"):
            fname = it["sym"].rsplit("/", 1)[-1]
            ns += [fname, _stem(fname)]
            if it.get("lib"):
                ns.append(f"{fname} [{it['lib']}]")
        return ns

    # ------------------------------------------------------------ lookup
    def find(self, query: str, n: int = 8, kinds=None, min_score: float = 20) -> list:
        """Best candidates for a name: exact name, Quick Drop shortcut, case-insensitive name, keyword,
        prefix, all words, substring, fuzzy. Returns [(score, item)], best first."""
        if self.table is None:
            self.load()
        if self._idx is None:
            self.items = self.table["items"]
            self._idx = self._name_index()
            self._kw = {}
            for it in self.items:
                for k in it.get("keywords", []):
                    self._kw.setdefault(k.lower(), []).append(it)
            self._sc = {k.lower(): v for k, v in self.table.get("shortcuts", {}).items()}
        q = query.strip()
        ql = q.lower()
        scores = {}

        def hit(it, s):
            if kinds and it["kind"] not in kinds:
                return
            s += 3 if it.get("palette") else 0          # on a palette
            s += 2 if it["kind"] == "prim" else 0
            s += 3 if it.get("shortcuts") else 0       # has a Quick Drop shortcut: commonly used
            s -= 4 if "dialog" in it.get("flags", ()) else 0
            s -= 5 if "synth" in it.get("flags", ()) else 0
            s -= 30 if "missing" in it.get("flags", ()) else 0
            if s > scores.get(id(it), (0, None))[0]:
                scores[id(it)] = (s, it)

        for it in self._idx.get(ql, []):
            hit(it, 100 if q in self._names(it) else 90)
        if ql in self._sc:
            for it in self._idx.get(self._sc[ql].lower(), [])[:1]:
                hit(it, 95)
        for it in self._kw.get(ql, []):
            hit(it, 70)
        words = re.findall(r"[a-z0-9]+|[^a-z0-9\s]", ql)
        for name, its in self._idx.items():
            if name.startswith(ql):
                s = 60 - min(len(name) - len(ql), 20) / 2
            elif words and all(w in name for w in words):
                s = 50 - min(len(name) - len(ql), 30) / 3
            elif len(ql) >= 3 and ql in name:
                s = 40
            else:
                continue
            for it in its:
                hit(it, s)
        qw = [w for w in re.findall(r"[a-z]+|[0-9]+", ql) if len(w) >= 2 or w.isdigit()]
        if len(qw) >= 2:   # several words, not all in one name ("pi constant", "add subtract multiply divide")
            for name, its in self._idx.items():
                nw = re.findall(r"[a-z]+|[0-9]+", name)
                m = sum(1 for w in qw if w in nw)
                if not m:
                    continue
                s = 20 + 25 * m / len(qw) - 1.5 * max(len(nw) - m, 0) + (10 if len(nw) == 1 and nw[0] in qw else 0)
                for it in its:
                    hit(it, s)
        if len(scores) < n:
            for name in difflib.get_close_matches(ql, list(self._idx), n=n, cutoff=0.6):
                r = difflib.SequenceMatcher(None, ql, name).ratio()
                for it in self._idx[name]:
                    hit(it, 30 * r)
        return [x for x in sorted(scores.values(), key=lambda x: -x[0]) if x[0] >= min_score][:n]

    def get(self, query: str):
        """Best match or None."""
        r = self.find(query, 1)
        return r[0][1] if r else None

    # ------------------------------------------------------------ terminals
    def scan_terminals(self, items=None, kinds=("prim", "vi", "vim", "polyvi", "mergevi"), only_palette=True,
                       batch=40, progress=None, save_every=200, retry=False):
        """Drop items into scratch VIs (closed without saving) and record their terminals:
        item["terminals"] = [{"name", "dir": "in"|"out", "type"}]. For VIs also "vi_type" and "desc"
        (VI Type 4 = polymorphic -> kind polyvi; 5 = Express -> kind express, not dropped).
        Never drops Express VIs / XNodes / items flagged "dialog". Returns (done, failed)."""
        S = self.S
        if self.table is None:
            self.load()
        if retry:
            for it in (items if items is not None else self.table["items"]):
                it.pop("terminals_error", None)
        todo = [it for it in (items if items is not None else self.table["items"])
                if "terminals" not in it and "terminals_error" not in it and it["kind"] in kinds
                and "dialog" not in it.get("flags", ()) and "missing" not in it.get("flags", ())
                and (it.get("palette") or not only_palette)
                and not (it["kind"] == "prim" and ("no_style" in it.get("flags", ()) or NO_DROP.search(it["name"])
                                                  or it.get("otype") in NO_DROP_OTYPES
                                                  or "Decorations" in (it.get("palette") or "")))]
        tw = self._term_wrapper()
        self._prepare(todo)                   # generate helper wrappers before any scratch VI exists
        done = failed = 0
        for b0 in range(0, len(todo), batch):
            vi = S.new_vi()
            bd = S.block_diagram(vi)
            try:
                for k, it in enumerate(todo[b0:b0 + batch]):
                    try:
                        node = self._drop(it, bd, 60 + (k % 8) * 140, 60 + (k // 8) * 120)
                        if node is None:
                            continue
                        it["terminals"] = self._terminals(node, tw)
                        done += 1
                    except (LVError, KeyError, IndexError, ValueError, NotImplementedError, struct.error) as e:
                        it["terminals_error"] = str(e)[:200]
                        failed += 1
                    if progress:
                        progress(b0 + k + 1, len(todo), it["name"])
            finally:
                S.close(vi)
            if (b0 // batch + 1) % max(1, save_every // batch) == 0:
                self.save()
        self.save()
        return done, failed

    def terminals(self, query_or_item):
        """Terminals of one item (scanned on demand and cached)."""
        it = self.get(query_or_item) if isinstance(query_or_item, str) else query_or_item
        if it is not None and "terminals" not in it:
            it.pop("terminals_error", None)
            self.scan_terminals([it], kinds=(it["kind"],), only_palette=False)
        return it.get("terminals") if it else None

    def _prepare(self, todo):
        if any(it["kind"] != "prim" for it in todo):
            S = self.S
            S.wrapper("prim", "", "Open VI Reference",
                      skip=("application reference (local)", "type specifier VI Refnum (for type only)"))
            S.wrapper("prop", "VI", ("VIType", "Description"))
            try:
                S.wrapper("prop", "PolymorphicVI", "InstanceInfo")
            except LVError:
                pass
            if not hasattr(self, "_dropper"):
                self._dropper = subvi_dropper(S)

    def _term_wrapper(self):
        try:
            w = self.S.wrapper("prop", "Terminal", ("Name", "IsSource", "Data Type"))
            w.has_type = True
        except LVError:                       # no Terminal:Data Type in this LabVIEW
            w = self.S.wrapper("prop", "Terminal", ("Name", "IsSource"))
            w.has_type = False
        return w

    def _terminals(self, node, tw):
        out = []
        try:
            terms, _ = self.S.node_info(node)
        except LVError as e:
            if e.code != 1058:                # 1058: not a Node (constants, structures, decorations)
                raise
            try:                              # a block diagram constant: its one terminal
                cw = self.S.wrapper("prop", "Constant", "Terminal")
                terms = [cw.run({"reference": node}, [cw.item])[cw.item]]
            except LVError:
                raise e from None
        names = ["Name", "Is Source?"] + (["Data Type"] if tw.has_type else [])
        for t in terms:
            ref = {"reference": tw.template("reference").with_value(t)}
            try:
                r = self.S.c.call(tw.path, ref, names + ["error out"])
            except (NotImplementedError, AssertionError, IndexError, struct.error):   # type the client can't decode
                r = self.S.c.call(tw.path, ref, ["Name", "Is Source?", "error out"])
                r["Data Type"] = None
            if r["error out"].value[0]:
                continue
            x = {"name": r["Name"].value, "dir": "out" if r["Is Source?"].value else "in"}
            if tw.has_type:
                x["type"] = type_str(r["Data Type"]) if r["Data Type"] is not None else "?"
            if not x["name"] and x.get("type", "void") == "void":
                continue                      # unassigned connector pane terminal of a subVI
            out.append(x)
        return out

    def _drop(self, it, bd, x, y):
        S = self.S
        if it["kind"] == "prim":
            return S.new_object(bd, it["style"], x, y)
        # subVI: open it first (Open VI Reference never prompts for missing files), check its type
        ovr = S.wrapper("prim", "", "Open VI Reference",
                        skip=("application reference (local)", "type specifier VI Refnum (for type only)"))
        r = run_named(ovr, {"vi path": it["path"]}, ["vi reference"])
        if r["error out"][0]:
            raise LVError(r["error out"][1], r["error out"][2])
        ref = r["vi reference"]
        try:
            vw = S.wrapper("prop", "VI", ("VIType", "Description"))
            r = run_named(vw, {"reference": ref}, ["VIType", "Description"])
            it["vi_type"] = r["VIType"]
            if r["Description"]:
                it["desc"] = r["Description"][:600]
            if r["VIType"] == 5:                      # Configuration VI = Express VI: dropping opens a dialog
                it["kind"] = "express"
                it["flags"] = sorted(set(it["flags"]) | {"dialog"})
                return None
            if r["VIType"] == 4:
                if it["kind"] == "vi":
                    it["kind"] = "polyvi"
                try:                            # instance VIs: drop one of them to get that instance
                    pw = S.wrapper("prop", "PolymorphicVI", "InstanceInfo")
                    inst = pw.run({"reference": ref}, [pw.item])[pw.item]
                    it["instance_vis"] = [{"path": path_str((p["type"], p["parts"])), "menu": m, "selector": sel}
                                          for p, m, sel in inst]
                except (LVError, KeyError, ValueError, NotImplementedError):
                    pass
            if not hasattr(self, "_dropper"):
                self._dropper = subvi_dropper(S)
            r = run_named(self._dropper, {"owner refnum": bd, "path": it["path"], "position/next to": [x, y]},
                          ["object refnum"])
            if r["error out"][0]:
                raise LVError(r["error out"][1], r["error out"][2])
            return r["object refnum"]
        finally:
            S.close(ref)


def describe(it: dict) -> str:
    """One-line human/LLM summary of an item."""
    how = f"style {it['style']}" if it["kind"] == "prim" else it.get("sym") or it.get("path")
    s = f"{it['name']} [{it['kind']}] {how}"
    if it.get("palette"):
        s += f"  ({it['palette']})"
    if it.get("shortcuts"):
        s += f"  QD: {','.join(it['shortcuts'])}"
    if it.get("flags"):
        s += f"  flags: {','.join(it['flags'])}"
    return s
