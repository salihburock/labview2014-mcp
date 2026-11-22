"""Client for the lvmcp scripting server VI (lvmcp_server.vi) running inside LabVIEW.

Wire protocol (one line each way, JSON):
  request : ["<token>", "<vi path>", "<json names[]>", "<json values[]>", "<json outs[]>"]
            values[i] is a string holding a JSON array of bytes: a LabVIEW *flattened variant* (Flatten To String of a variant)
  reply   : ["<json error cluster>", "<json bytes of flattened variant>" for each name in outs]

The server opens the VI (strictly typed, empty connector pane), sets each named control from its
variant, calls it with Call By Reference, reads each named control/indicator as a variant and closes it.
References created inside the called VI belong to the server's VI hierarchy, so they stay valid
between calls until the server stops.
"""
from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass, field


class LVError(Exception):
    def __init__(self, code, source):
        super().__init__(f"LabVIEW error {code}: {source}")
        self.code = code
        self.source = source


# ---------------------------------------------------------------- type descriptors

NUMERIC = {0x01: ">b", 0x02: ">h", 0x03: ">i", 0x04: ">q", 0x05: ">B", 0x06: ">H", 0x07: ">I", 0x08: ">Q",
           0x09: ">f", 0x0A: ">d"}
NUMERIC_CODES = set(NUMERIC) | {0x0B, 0x0C, 0x0D, 0x0E}  # + EXT and complex: one extra byte in the TD
EXT, CSG, CDB, CXT, TIMESTAMP = 0x0B, 0x0C, 0x0D, 0x0E, 0x54
WAVEFORM_Y = {2: "h", 3: "d", 4: "f"}   # 0x54 subtypes that are waveforms -> Y element format
ENUMS = {0x15: ">B", 0x16: ">H", 0x17: ">I"}


@dataclass
class TD:
    """One LabVIEW type descriptor."""
    code: int
    raw: bytes
    label: str | None = None
    items: list = field(default_factory=list)   # cluster: element TDs; array: [element TD]; typedef: [inner]
    dims: int = 0                                 # array dimensions
    names: list = field(default_factory=list)     # enum item names
    refkind: int = 0
    subtype: int = 0                              # 0x54 measure data subtype

    def describe(self) -> str:
        c = self.code
        if c == 0x54 and self.subtype in WAVEFORM_Y:
            return {2: "waveform I16", 3: "waveform DBL", 4: "waveform SGL"}[self.subtype]
        if c in NUMERIC or c in ENUMS:
            return {0x01: "I8", 0x02: "I16", 0x03: "I32", 0x04: "I64", 0x05: "U8", 0x06: "U16", 0x07: "U32",
                    0x08: "U64", 0x09: "SGL", 0x0A: "DBL", 0x15: "enum8", 0x16: "enum16", 0x17: "enum32"}[c]
        if c in (0x0B, 0x0C, 0x0D, 0x0E):
            return {0x0B: "EXT", 0x0C: "CSG", 0x0D: "CDB", 0x0E: "CXT"}[c]
        return {0x00: "void", 0x21: "bool", 0x30: "string", 0x32: "path", 0x40: f"array{self.dims}d", 0x54: "timestamp",
                0x50: "cluster", 0x53: "variant", 0x70: "refnum", 0xF1: "typedef"}.get(c, f"0x{c:02x}")


def _pstr(b: bytes, i: int):
    n = b[i]
    return b[i + 1:i + 1 + n].decode("latin-1"), i + 1 + n


def parse_td(b: bytes, i: int, table: list) -> tuple[TD, int]:
    """Parse the type descriptor at b[i:]. Returns (TD, index after it)."""
    size = struct.unpack_from(">H", b, i)[0]
    end = i + size
    flags, code = b[i + 2], b[i + 3]
    td = TD(code=code, raw=b[i:end])
    j = i + 4
    if code in NUMERIC_CODES:
        j += 1                                    # numerics: one extra (units) byte
    elif code in ENUMS:
        n = struct.unpack_from(">H", b, j)[0]; j += 2
        for _ in range(n):
            s, j = _pstr(b, j); td.names.append(s)
        j += (j - i) % 2 + 1                      # pad to even, then the numeric extra byte
    elif code in (0x30, 0x32):
        j += 4
    elif code == 0x54:                            # "measure data": u16 subtype (3 = DBL waveform, 6 = timestamp)
        td.subtype = struct.unpack_from(">H", b, j)[0]; j += 2
    elif code == 0x40:
        td.dims = struct.unpack_from(">H", b, j)[0]; j += 2 + 4 * td.dims
        k = struct.unpack_from(">H", b, j)[0]; j += 2   # element type: index into the type table
        td.items = [table[k]]
    elif code == 0x50:
        n = struct.unpack_from(">H", b, j)[0]; j += 2
        idx = struct.unpack_from(f">{n}H", b, j); j += 2 * n
        td.items = [table[k] for k in idx]
    elif code == 0x70:
        td.refkind = struct.unpack_from(">H", b, j)[0]
    elif code == 0xF1:
        j += 4
        n = struct.unpack_from(">I", b, j)[0]; j += 4
        for _ in range(n):
            _, j = _pstr(b, j)
        inner, j = parse_td(b, j, table)
        td.items = [inner]
        td.label = inner.label
    if flags & 0x40 and code not in (0x70, 0xF1) and j < end:
        try:
            td.label, _ = _pstr(b, j)
        except IndexError:
            pass
    if flags & 0x40 and code == 0x70:
        # refnum: label is the last pascal string; scan back from the end
        k = end - 1
        while k > i and b[k] == 0:
            k -= 1
        for start in range(i + 4, end):
            n = b[start]
            if start + 1 + n == k + 1 and n > 0:
                td.label = b[start + 1:k + 1].decode("latin-1"); break
    return td, end


# ---------------------------------------------------------------- data (un)flattening

def read_data(td: TD, b: bytes, i: int):
    c = td.code
    if c == 0x00:
        return None, i
    if c in NUMERIC:
        f = NUMERIC[c]; return struct.unpack_from(f, b, i)[0], i + struct.calcsize(f)
    if c in ENUMS:
        f = ENUMS[c]; return struct.unpack_from(f, b, i)[0], i + struct.calcsize(f)
    if c == 0x0B:  # EXT: flattened as IEEE 754 binary128, big-endian
        return _quad_to_float(b[i:i + 16]), i + 16
    if c in (0x0C, 0x0D):  # CSG, CDB
        f = ">ff" if c == 0x0C else ">dd"
        re_, im = struct.unpack_from(f, b, i); return complex(re_, im), i + struct.calcsize(f)
    if c == 0x0E:  # CXT
        return complex(_quad_to_float(b[i:i + 16]), _quad_to_float(b[i + 16:i + 32])), i + 32
    if c == 0x54 and getattr(td, "subtype", 0) in WAVEFORM_Y:  # waveform: t0, dt, Y[], attributes variant
        sec, frac = struct.unpack_from(">qQ", b, i); i += 16
        dt = struct.unpack_from(">d", b, i)[0]; i += 8
        n = struct.unpack_from(">I", b, i)[0]; i += 4
        f = WAVEFORM_Y[td.subtype]
        y = list(struct.unpack_from(f">{n}{f}", b, i)); i += n * struct.calcsize(f)
        # then 9 bytes of unknown meaning (always zero so far) and the attributes variant; look for the
        # variant's version stamp in the next few bytes instead of trusting the 9.
        stamp = struct.pack(">I", VERSION)
        k = b.find(stamp, i, i + 24)
        if k < 0:
            raise NotImplementedError("waveform attributes not found")
        attrs, used = parse_variant_at(b, k)
        i = k
        return {"t0": sec + frac / 2.0 ** 64, "dt": dt, "Y": y}, i + used
    if c == 0x54:  # timestamp: i64 seconds since 1904-01-01 UTC + u64 fraction
        sec, frac = struct.unpack_from(">qQ", b, i); return sec + frac / 2.0 ** 64, i + 16
    if c == 0x21:
        return b[i] != 0, i + 1
    if c == 0x30:
        n = struct.unpack_from(">I", b, i)[0]; return b[i + 4:i + 4 + n].decode("latin-1"), i + 4 + n
    if c == 0x32:
        assert b[i:i + 4] == b"PTH0", b[i:i + 4]
        n = struct.unpack_from(">I", b, i + 4)[0]
        body = b[i + 8:i + 8 + n]
        ptype, count = struct.unpack_from(">HH", body, 0)
        parts, k = [], 4
        for _ in range(count):
            s, k = _pstr(body, k); parts.append(s)
        return {"type": ptype, "parts": parts}, i + 8 + n
    if c == 0x40:
        dims = struct.unpack_from(f">{td.dims}I", b, i); i += 4 * td.dims
        total = 1
        for d in dims:
            total *= d
        out = []
        for _ in range(total):
            v, i = read_data(td.items[0], b, i); out.append(v)
        for d in reversed(dims[1:]):           # n-D arrays come back as nested lists (row major)
            out = [out[k:k + d] for k in range(0, len(out), d)] if d else []
        return out, i
    if c == 0x50:
        out = []
        for el in td.items:
            v, i = read_data(el, b, i); out.append(v)
        return out, i
    if c == 0x70:
        return struct.unpack_from(">I", b, i)[0], i + 4
    if c == 0xF1:
        return read_data(td.items[0], b, i)
    if c == 0x53:
        try:
            v, n = parse_variant_at(b, i)
        except (struct.error, AssertionError, IndexError) as e:
            raise NotImplementedError(f"nested variant: {e}") from e
        return v, i + n
    raise NotImplementedError(f"type 0x{c:02x}")


def _quad_to_float(q: bytes) -> float:
    n = int.from_bytes(q, "big")
    sign = -1.0 if n >> 127 else 1.0
    exp = (n >> 112) & 0x7FFF
    frac = n & ((1 << 112) - 1)
    if exp == 0 and frac == 0:
        return 0.0 * sign
    if exp == 0x7FFF:
        return sign * float("inf") if frac == 0 else float("nan")
    return sign * (1 + frac / (1 << 112)) * 2.0 ** (exp - 16383)


def _float_to_quad(x: float) -> bytes:
    import math
    x = float(x)
    sign = 1 if math.copysign(1.0, x) < 0 else 0
    if x == 0:
        return (sign << 127).to_bytes(16, "big")
    if math.isinf(x):
        return ((sign << 127) | (0x7FFF << 112)).to_bytes(16, "big")
    if math.isnan(x):
        return ((0x7FFF << 112) | (1 << 111)).to_bytes(16, "big")
    m, e = math.frexp(abs(x))                 # abs(x) = m * 2**e, 0.5 <= m < 1
    frac = int((m * 2 - 1) * (1 << 112))
    return ((sign << 127) | ((e - 1 + 16383) << 112) | frac).to_bytes(16, "big")


def write_data(td: TD, v) -> bytes:
    c = td.code
    if c == 0x00:
        return b""
    if c in NUMERIC:
        return struct.pack(NUMERIC[c], v)
    if c == 0x0B:
        return _float_to_quad(v)
    if c in (0x0C, 0x0D):
        v = complex(v); return struct.pack(">ff" if c == 0x0C else ">dd", v.real, v.imag)
    if c == 0x0E:
        v = complex(v); return _float_to_quad(v.real) + _float_to_quad(v.imag)
    if c == 0x54 and td.subtype in WAVEFORM_Y:   # {"t0", "dt", "Y"} -> waveform (no attributes)
        t0 = float(v.get("t0", 0.0)) if isinstance(v, dict) else 0.0
        dt = float(v.get("dt", 1.0)) if isinstance(v, dict) else 1.0
        y = list(v.get("Y", []) if isinstance(v, dict) else v)
        sec = int(t0 // 1)
        f = WAVEFORM_Y[td.subtype]
        attrs = struct.pack(">II", VERSION, 1) + b"\x00\x04\x00\x00" + struct.pack(">HHI", 1, 0, 0)
        return (struct.pack(">qQd", sec, int((t0 - sec) * 2.0 ** 64) & (2 ** 64 - 1), dt)
                + struct.pack(f">I{len(y)}{f}", len(y), *y) + b"\x00" * 9 + attrs)
    if c == 0x54:
        sec = int(v // 1); return struct.pack(">qQ", sec, int((v - sec) * 2.0 ** 64) & (2 ** 64 - 1))
    if c in ENUMS:
        return struct.pack(ENUMS[c], v)
    if c == 0x21:
        return b"\x01" if v else b"\x00"
    if c == 0x30:
        s = v.encode("latin-1") if isinstance(v, str) else bytes(v)
        return struct.pack(">I", len(s)) + s
    if c == 0x32:
        return flatten_path(v)
    if c == 0x40:
        if td.dims == 1:
            return struct.pack(">I", len(v)) + b"".join(write_data(td.items[0], x) for x in v)
        shape, flat = [], [v]                 # n-D: nested lists, row major
        for _ in range(td.dims):
            shape.append(len(flat[0]) if flat else 0)
            flat = [x for row in flat for x in row]
        return struct.pack(f">{td.dims}I", *shape) + b"".join(write_data(td.items[0], x) for x in flat)
    if c == 0x50:
        return b"".join(write_data(el, x) for el, x in zip(td.items, v))
    if c == 0x70:
        return struct.pack(">I", v)
    if c == 0xF1:
        return write_data(td.items[0], v)
    raise NotImplementedError(f"type 0x{c:02x}")


def flatten_path(p) -> bytes:
    """LabVIEW path data. p: str (absolute, '/' or '\\' separated) or {"type":..,"parts":[..]}."""
    if isinstance(p, dict):
        ptype, parts = p["type"], p["parts"]
    else:
        s = str(p)
        if s.startswith("\\\\"):                   # UNC: type 3, first part is "\\\\server\\share"
            bits = [x for x in s[2:].replace("/", "\\").split("\\") if x]
            parts = ["\\\\" + bits[0] + "\\" + bits[1]] + bits[2:]
            body = struct.pack(">HH", 3, len(parts)) + b"".join(bytes([len(x)]) + x.encode("latin-1") for x in parts)
            return b"PTH0" + struct.pack(">I", len(body)) + body
        if len(s) >= 2 and s[1] == ":":           # Windows drive path
            parts = [s[0]] + [x for x in s[2:].replace("/", "\\").split("\\") if x]
        else:
            parts = [x for x in s.split("/") if x]
        ptype = 0                                   # 0 = absolute
    body = struct.pack(">HH", ptype, len(parts)) + b"".join(bytes([len(x)]) + x.encode("latin-1") for x in parts)
    return b"PTH0" + struct.pack(">I", len(body)) + body


# ---------------------------------------------------------------- variants

@dataclass
class Variant:
    version: int
    types: list          # list[TD]
    top: int             # index of the value's type
    data: bytes          # flattened data of the value
    tail: bytes          # attributes section (raw)

    @property
    def td(self) -> TD:
        return self.types[self.top]

    @property
    def value(self):
        return read_data(self.td, self.data, 0)[0]

    def with_value(self, v) -> "Variant":
        return Variant(self.version, self.types, self.top, write_data(self.td, v), b"\x00\x00\x00\x00")

    def flatten(self) -> bytes:
        out = struct.pack(">II", self.version, len(self.types)) + b"".join(t.raw for t in self.types)
        return out + struct.pack(">HH", 1, self.top) + self.data + (self.tail or b"\x00\x00\x00\x00")


def make_td(code: int, label: str = "") -> TD:
    """Type descriptor for a scalar (numeric, bool, string, path). Labels are padded pascal strings."""
    body = bytes([0x40 if label else 0x00, code])
    if code in NUMERIC_CODES:
        body += b"\x00"
    elif code in (0x30, 0x32):
        body += b"\xff\xff\xff\xff"
    if label:
        p = bytes([len(label)]) + label.encode("latin-1")
        body += p + (b"\x00" if len(p) % 2 else b"")
    raw = struct.pack(">H", len(body) + 2) + body
    return parse_td(raw, 0, [])[0]


VERSION = 0x22308000   # updated from the first variant received from the server


def make_variant(code: int, value, label: str = "", version: int | None = None) -> Variant:
    td = make_td(code, label)
    return Variant(version or VERSION, [td], 0, write_data(td, value), b"\x00\x00\x00\x00")


DBL, I32, U32, BOOL, STRING, PATH = 0x0A, 0x03, 0x07, 0x21, 0x30, 0x32


def parse_variant_at(b: bytes, i: int = 0) -> tuple[Variant, int]:
    global VERSION
    version, n = struct.unpack_from(">II", b, i); j = i + 8
    VERSION = version             # new variants are stamped with the version of the LabVIEW we talk to
    types = []
    for _ in range(n):
        td, j = parse_td(b, j, types); types.append(td)
    cnt, top = struct.unpack_from(">HH", b, j); j += 4
    assert cnt == 1, cnt
    try:
        _, k = read_data(types[top], b, j)
    except (NotImplementedError, struct.error, IndexError, AssertionError):
        # unknown/unparsable type (e.g. variants nested in waveforms): assume it is the whole rest
        # (no attributes); .value will raise
        k = len(b) - 4
    data = b[j:k]
    nattr = struct.unpack_from(">I", b, k)[0]
    end = k + 4
    attrs = {}
    try:
        for _ in range(nattr):       # each attribute: u32-length name + a nested flattened variant
            n = struct.unpack_from(">I", b, end)[0]
            name = b[end + 4:end + 4 + n].decode("latin-1")
            v, used = parse_variant_at(b, end + 4 + n)
            attrs[name] = v
            end += 4 + n + used
    except (struct.error, NotImplementedError, IndexError, AssertionError):
        end = len(b)                 # unparsable attributes: keep the value, drop the attributes
    var = Variant(version, types, top, data, b"\x00\x00\x00\x00")
    var.attributes = attrs
    return var, end - i


def parse_variant(b: bytes) -> Variant:
    return parse_variant_at(b, 0)[0]


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, host="127.0.0.1", port=5577, token="", timeout=60):
        self.addr = (host, port)
        self.token = token
        self.timeout = timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        return self

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def raw(self, path: str, names=(), values=(), outs=()):
        """values: flattened variant bytes per name. Returns (error dict, [bytes per out])."""
        if self.sock is None:
            self.connect()
        req = [self.token, str(path), json.dumps(list(names)), json.dumps([json.dumps(list(v)) for v in values]),
               json.dumps(list(outs))]
        self.sock.sendall(json.dumps(req).encode() + b"\r\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        reply = json.loads(line)
        err = json.loads(reply[0])
        return err, [bytes(json.loads(x)) for x in reply[1:]]

    def call(self, path: str, inputs: dict | None = None, outs=(), check=True):
        """inputs: name -> Variant. Returns dict name -> Variant (raises LVError on error if check)."""
        inputs = inputs or {}
        if not inputs and outs:
            # A VI called as a subVI only updates its front panel when the panel is in memory, and only
            # setting a control loads it: with no inputs the outputs would come back as defaults. So read the
            # first output once and write that same value back as the "input" (changes nothing, loads the panel).
            err0, vals0 = self.raw(path, [], [], list(outs)[:1])
            if not err0.get("status") and vals0:
                inputs = {list(outs)[0]: parse_variant(vals0[0])}
        args = (path, list(inputs.keys()), [v.flatten() for v in inputs.values()], outs)
        err, vals = self.raw(*args)
        src = str(err.get("source", ""))
        if err.get("status") and err.get("code") == 1026 and "lvmcp_server" in src and "->" not in src.split("<")[0]:
            # rare transient: the server's own Open VI Reference gave an invalid reference without an error, so
            # nothing ran (setting/calling fails on the invalid reference first): safe to send it again once
            err, vals = self.raw(*args)
        if check and err.get("status"):
            raise LVError(err.get("code"), err.get("source"))
        return {n: parse_variant(v) for n, v in zip(outs, vals)} | {"__error__": err}
