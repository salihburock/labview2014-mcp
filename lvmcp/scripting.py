"""VI Scripting through the lvmcp server.

Everything here is done by calling small "wrapper" VIs through the server (see lvclient.py). A wrapper
holds one scripting node whose terminals are all front-panel controls/indicators, so it can be driven
generically by control name.

A handful of wrappers are built by hand (labview/boot/*.vi). Every other wrapper (any property read or
write, any method) is *generated* here with those, saved to a cache directory and reused.
"""
from __future__ import annotations

import hashlib
import json
import struct
import os
import re
from pathlib import Path

from .lvclient import Client, LVError, Variant

# New VI Object style IDs (from the style ring of New VI Object, LabVIEW 2022; to be checked on 2014)
STYLE_PROPERTY_NODE = 1920
STYLE_INVOKE_NODE = 3900
STYLE_NEW_VI_OBJECT = 8015

ERR_IN = "error in (no error)"


class Wrapper:
    """A VI driven through the server by control name."""

    def __init__(self, client: Client, path: str, names=None):
        self.c = client
        self.path = str(path)
        self.templates: dict[str, Variant] = {}
        self.names = list(names or [])
        self.terms = []          # generated wrappers: [{"name", "dir", "label"}] per node terminal

    @property
    def item(self) -> str:
        """Label of the first non-standard terminal (the property value / method return value)."""
        std = ("reference", "reference out", "error in (no error)", "error out")
        return [t["label"] for t in self.terms if t["name"] not in std][0]

    def typed(self, name: str, value, convert=lambda td, v: v) -> Variant:
        """value as a Variant of control `name`'s type. Rarely a learned type turns out wrong (seen a few times:
        the type of another control came back); then the type is learned again once."""
        for attempt in (0, 1):
            t = self.template(name)
            try:
                return t.with_value(convert(t.td, value))
            except (ValueError, TypeError, OverflowError, KeyError, struct.error, IndexError):
                if attempt:
                    raise
                self.templates.pop(name, None)

    def template(self, name: str) -> Variant:
        if name not in self.templates:
            self.learn([name])
        return self.templates[name]

    peek = None   # optional callable(path, names) -> {name: Variant} that reads controls *without running*

    def learn(self, names):
        """Get type templates of the given controls. Without a peek function this runs the VI with
        default inputs (fine for property/invoke nodes: they just return an invalid-refnum error)."""
        if self.peek:
            self.templates.update(self.peek(self.path, list(names)))
            return
        r = self.c.call(self.path, outs=list(names), check=False)
        e = r["__error__"]
        if e.get("status"):   # server-side failure (VI not found, no such control): don't cache garbage types
            raise LVError(e.get("code"), e.get("source"))
        for n in names:
            self.templates[n] = r[n]

    def run(self, inputs: dict | None = None, outs=(), check=True):
        vals = {}
        missing = [k for k, v in (inputs or {}).items() if not isinstance(v, Variant) and k not in self.templates]
        if missing:
            self.learn(missing)
        for k, v in (inputs or {}).items():
            vals[k] = v if isinstance(v, Variant) else self.typed(k, v)
        has_err = not self.terms or any(t["label"] == "error out" for t in self.terms)
        r = self.c.call(self.path, vals, outs=list(outs) + (["error out"] if has_err else []), check=check)
        err = r["error out"].value if has_err else [False, 0, ""]
        if check and err[0]:
            raise LVError(err[1], err[2])
        return {k: r[k].value for k in outs} | {"error out": err}


def lv_join(folder: str, name: str) -> str:
    """Join a LabVIEW-side path (the machine running LabVIEW may be Windows while we run on Linux)."""
    folder = str(folder)
    sep = "\\" if ("\\" in folder or (len(folder) > 1 and folder[1] == ":")) else "/"
    return folder.rstrip("/\\") + sep + name


class Scripting:
    """boot_dir, gen_dir: folders as LabVIEW sees them (e.g. C:\\lvmcp\\boot or \\\\VBOXSVR\\x\\boot).
    cache_dir: local folder for the index of generated wrappers and the style table."""

    def __init__(self, client: Client, boot_dir: str, gen_dir: str, cache_dir: str | Path | None = None,
                 prefix: str = "lvmcp_", boot_prefix: str = ""):
        """prefix: file-name prefix of generated wrappers. VI names must be unique in a LabVIEW instance, so
        two caches used in the same LabVIEW at the same time need different prefixes.
        boot_prefix: file-name prefix of the boot wrappers (default none: new_vi.vi, ...). Clients that share
        one LabVIEW through several servers should each use their own renamed copies of the boot wrappers:
        the server sets controls, calls and reads back in separate steps, so two servers calling the same
        (non-reentrant) wrapper VI at the same time can mix up each other's inputs/outputs."""
        for d in (boot_dir, gen_dir):
            d = str(d)
            if not (d.startswith("/") or d.startswith("\\\\") or (len(d) > 2 and d[1] == ":")):
                raise ValueError(f"LabVIEW-side folder must be an absolute path: {d!r}")
        self.c = client
        self.prefix = prefix
        self.boot_prefix = boot_prefix
        self.boot = str(boot_dir)
        self.gen = str(gen_dir)
        self.cache = Path(cache_dir if cache_dir is not None else gen_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.w = {}
        self.index_file = self.cache / "index.json"
        self.index = {}
        self.reload_index()

    def reload_index(self):
        """Merge the index on disk into memory (another session sharing this cache may have generated
        wrappers since)."""
        try:
            self.index = json.loads(self.index_file.read_text()) | self.index
        except (FileNotFoundError, ValueError):
            pass

    def has(self, name) -> bool:
        if name not in self.index:
            self.reload_index()
        return name in self.index

    def save_index(self):
        """Write the index (merged with the one on disk) atomically."""
        self.reload_index()
        tmp = self.index_file.with_name(f"index.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.index, indent=1))
        os.replace(tmp, self.index_file)

    def save_new(self, vi, path):
        """Save a freshly generated wrapper; if that fails because another session sharing the cache saved
        the same wrapper meanwhile, theirs is used."""
        try:
            self.save(vi, path)
        except LVError:
            if not self.has(path.replace("\\", "/").rsplit("/", 1)[-1]):
                raise

    def boot_w(self, name) -> Wrapper:
        if name not in self.w:
            self.w[name] = Wrapper(self.c, lv_join(self.boot, f"{self.boot_prefix}{name}.vi"))
        return self.w[name]

    # ------------------------------------------------------------ bootstrap operations
    def new_vi(self) -> int:
        return self.boot_w("new_vi").run({"vi type (standard vi)": 1}, ["vi refnum"])["vi refnum"]

    def block_diagram(self, vi: int) -> int:
        return self.boot_w("vi_bd").run({"reference": vi}, ["Block Diagram"])["Block Diagram"]

    def new_object(self, owner: int, style: int, x: int, y: int, w: int = 0, h: int = 0) -> int:
        r = self.boot_w("new_object").run({"owner refnum": owner, "style": style, "location": [x, y],
                                           "bounds": [w, h]}, ["object refnum"])
        return r["object refnum"]

    def node_info(self, node: int):
        r = self.boot_w("node_info").run({"reference": node}, ["Terminals[]", "Style"])
        return r["Terminals[]"], r["Style"]

    def create_control(self, term: int, check=True):
        r = self.boot_w("term_create_control").run({"reference": term}, ["Create Control"], check=check)
        return r["Create Control"], r["error out"]

    def create_indicator(self, term: int, check=True):
        r = self.boot_w("term_create_indicator").run({"reference": term}, ["Create Indicator"], check=check)
        return r["Create Indicator"], r["error out"]

    def create_constant(self, term: int, value: Variant | None = None):
        inp = {"reference": term}
        if value is not None:
            inp["Value"] = value
        return self.boot_w("term_create_constant").run(inp, ["Create Constant"])["Create Constant"]

    def connect(self, src_term: int, dst_term: int, auto_route=True):
        """Wire src_term (source) to dst_term. The method is invoked on dst_term."""
        self.boot_w("term_connect_wire").run({"reference": dst_term, "Wire Source": src_term,
                                              "Auto Wire? (T)": True, "Auto Route? (F)": auto_route})

    @staticmethod
    def _cls(cls: str) -> str:
        return cls if ":" in cls else f"VI Server:{cls}"   # format the Class Name properties expect

    def config_property(self, node: int, cls: str, props: list[str], write=False):
        cls = self._cls(cls)
        self.boot_w("propnode_config").run({"reference": node, "Property Node Class Name": cls,
                                            "ID Strings[]": list(props), "Allow Alternate Names? (F)": True,
                                            "index": 0, "Is Write": bool(write)})

    def config_invoke(self, node: int, cls: str, method: str):
        cls = self._cls(cls)
        self.boot_w("invknode_config").run({"reference": node, "Invoke Node Class Name": cls,
                                            "ID String": method, "Allow Alternate Names? (F)": True})

    def close(self, ref: int):
        self.boot_w("close_ref").run({"reference": ref})

    def save(self, vi: int, path: str | Path):
        self.boot_w("vi_save").run({"reference": vi, "Path to Saved File": str(path)})

    # ------------------------------------------------------------ wrapper generation
    def _fname(self, kind, cls, item, write):
        s = f"{kind}_{'w' if write else 'r'}_{cls}_{item}" if kind == "prop" else f"{kind}_{cls}_{item}"
        return self.prefix + re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:120] + ".vi"

    def wrapper(self, kind: str, cls: str, item: str, write=False, skip=()) -> Wrapper:
        """Get (generating on first use) a wrapper.
        kind "prop": property `item` of class `cls` (read, or write if `write`)
        kind "meth": method `item` of class `cls`
        kind "prim": the function/primitive whose New VI Object style name is `item` (cls ignored)
        skip: terminal names to leave unwired (e.g. optional refnum inputs that must stay unwired).
        For kind "prop", `item` may also be a list/tuple of property names: one property node with several
        items (read in one call; the node stops at the first item that fails)."""
        if isinstance(item, (list, tuple)):
            item = tuple(item) if len(item) > 1 else item[0]
        item_s = "+".join(item) if isinstance(item, tuple) else item
        key = f"{kind}|{cls}|{item_s}|{int(write)}|{','.join(skip)}"
        if key in self.w:
            return self.w[key]
        name = self.wrapper_name(kind, cls, item, write, skip)
        path = lv_join(self.gen, name)
        if not self.has(name):
            try:
                self._generate(kind, cls, item, write, path, skip)
            except LVError:
                if not self.has(name):   # another session sharing the cache generated it meanwhile: use theirs
                    raise
        w = Wrapper(self.c, path)
        w.terms = self.index[name]["terms"]
        if kind == "prim" and item != "Open VI Reference":
            w.peek = self.peek
        self.w[key] = w
        return w

    def wrapper_name(self, kind, cls, item, write=False, skip=()) -> str:
        """File name of the wrapper VI wrapper() makes for these arguments."""
        if isinstance(item, (list, tuple)):
            item = "+".join(item) if len(item) > 1 else item[0]
        name = self._fname(kind, cls, item, write)
        if skip:   # the same item with other unwired terminals is a different wrapper (own file name)
            name = name[:-3] + "_s" + hashlib.md5("|".join(sorted(skip)).encode()).hexdigest()[:6] + ".vi"
        return name

    # The first two generated wrappers are needed to generate any other one, so their terminal
    # names/directions are known in advance. Property node terminal order:
    # reference, reference out, error in, error out, then the property items.
    _PROP_FIXED = ["reference", "reference out", "error in (no error)", "error out"]
    _PROP_DIRS = ["in", "out", "in", "out"]
    _SELF = {("Terminal", "Name"): "Name", ("Terminal", "IsSource"): "Is Source?",
             ("Control", "Label"): "Label", ("Text", "Text"): "Text"}

    def term_name(self, t: int) -> str:
        return self.wrapper("prop", "Terminal", "Name").run({"reference": t}, ["Name"])["Name"]

    def term_is_output(self, t: int) -> bool:
        return self.wrapper("prop", "Terminal", "IsSource").run({"reference": t}, ["Is Source?"])["Is Source?"]

    def styles(self) -> dict:
        """New VI Object style table {name: id}, read from the style ring of a New VI Object node."""
        f = self.cache / "styles.json"
        if f.exists():
            return json.loads(f.read_text())
        vi = self.new_vi()
        nvo = self.new_object(self.block_diagram(vi), STYLE_NEW_VI_OBJECT, 300, 200)
        term = [t for t in self.node_info(nvo)[0] if self.term_name(t) == "style"][0]
        ring = self.create_constant(term)
        w = self.wrapper("prop", "RingConstant", "Strings And Values[]")
        pairs = w.run({"reference": ring}, [w.item])[w.item]
        self.close(vi)
        table = {name: int(v) for name, v in pairs}
        f.write_text(json.dumps(table, indent=0, sort_keys=True))
        return table

    def peek(self, path: str, names) -> dict:
        """Read controls of a VI without running it (Open VI Reference + Control Value:Get)."""
        ovr = self.wrapper("prim", "", "Open VI Reference",
                           skip=("application reference (local)", "type specifier VI Refnum (for type only)"))
        get = self.wrapper("meth", "VI", "Control Value:Get")
        ref = ovr.run({"vi path": path}, ["vi reference"])["vi reference"]
        try:
            out = {}
            for n in names:
                r = self.c.call(get.path, {"reference": get.template("reference").with_value(ref),
                                           "Control Name": get.template("Control Name").with_value(n)},
                                outs=[get.item, "error out"])
                if r["error out"].value[0]:
                    raise LVError(r["error out"].value[1], r["error out"].value[2])
                out[n] = r[get.item]
            return out
        finally:
            self.close(ref)

    def label_of(self, ctl: int) -> str:
        lbl = self.wrapper("prop", "Control", "Label").run({"reference": ctl}, ["Label"])["Label"]
        return self.wrapper("prop", "Text", "Text").run({"reference": lbl}, ["Text"])["Text"]

    def _generate(self, kind, cls, item, write, path, skip=()):
        vi = self.new_vi()
        try:
            self._build(vi, kind, cls, item, write, path, skip)
        except Exception:
            try:
                self.close(vi)          # don't leak an unsaved untitled VI when generation fails
            except Exception:
                pass
            raise
        self.close(vi)
        self._check_and_index(kind, cls, item, write, path, skip)

    def _build(self, vi, kind, cls, item, write, path, skip=()):
        bd = self.block_diagram(vi)
        style = {"prop": STYLE_PROPERTY_NODE, "meth": STYLE_INVOKE_NODE}.get(kind) or self.styles()[item]
        node = self.new_object(bd, style, 300, 200)
        if kind == "prop":
            self.config_property(node, cls, list(item) if isinstance(item, tuple) else [item], write)
        elif kind == "meth":
            self.config_invoke(node, cls, item)
        terms, _ = self.node_info(node)
        if kind == "prop" and not write and (cls, item) in self._SELF:
            names = self._PROP_FIXED + [self._SELF[(cls, item)]]
            dirs = self._PROP_DIRS + ["out"]
        else:
            names = [self.term_name(t) for t in terms]
            dirs = ["out" if self.term_is_output(t) else "in" for t in terms]
        bootstrapping = not write and (cls, item) in self._SELF
        info, used = [], {}
        # controls first: an output whose type follows an input's type (e.g. Read from Binary File's data type)
        # must get its indicator after that input is wired. `info` keeps the node's terminal order though
        # (Wrapper.item is the first non-standard terminal: the property value / method return value).
        order = sorted(enumerate(zip(terms, names, dirs)), key=lambda x: x[1][2] != "in")
        for i, (t, name, d) in order:
            if name in skip:
                continue
            ref, err = (self.create_control if d == "in" else self.create_indicator)(t, check=False)
            if err[0]:
                if err[1] == 1074:             # invoke node pseudo-terminals (method name, input echoes)
                    continue
                raise LVError(err[1], err[2])
            if bootstrapping:
                n = used.get(name, 0) + 1      # LabVIEW numbers duplicate labels: "x", "x 2", ...
                used[name] = n
                label = name if n == 1 else f"{name} {n}"
            else:
                label = self.label_of(ref)
            info.append((i, {"name": name, "dir": d, "label": label}))
        self.save(vi, path)
        self._pending_info = [x for _, x in sorted(info, key=lambda x: x[0])]

    def _check_and_index(self, kind, cls, item, write, path, skip=()):
        info = self._pending_info
        # check that every label exists: this *runs* the wrapper with default inputs, which is harmless for
        # property/method nodes (invalid refnum -> error) but not for primitives/subVIs with side effects
        # (e.g. vi.lib Delete opens a file dialog when its path is empty), so only done for prop/meth.
        if kind in ("prop", "meth"):
            r = self.c.call(path, outs=[x["label"] for x in info], check=False)
            if r["__error__"].get("status") and r["__error__"].get("code") == 1:
                raise RuntimeError(f"label mismatch in generated {path}: {r['__error__']['source']}")
            if r["__error__"].get("status"):     # e.g. 1003/1026: the wrapper is broken (invalid property...)
                raise LVError(r["__error__"].get("code"), f"generated wrapper {path} cannot run: "
                              f"{r['__error__'].get('source')}")
        self.index[path.replace("\\", "/").rsplit("/", 1)[-1]] = {"kind": kind, "class": cls, "item": item, "write": write,
                                                                 "skip": list(skip), "terms": info}
        self.save_index()
