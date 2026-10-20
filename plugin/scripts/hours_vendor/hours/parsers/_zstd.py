from __future__ import annotations
import struct
from typing import List, Optional, Tuple
MAGIC = 0xFD2FB528
MAX_OUTPUT = 1 << 31
class ZstdError(ValueError):
    pass
def _hb(x: int) -> int:
    return x.bit_length() - 1
class _Fwd:
    __slots__ = ("data", "bit", "end_bit")
    def __init__(self, data: bytes, pos: int, end: int) -> None:
        self.data = data
        self.bit = pos * 8
        self.end_bit = end * 8
    def read(self, n: int) -> int:
        if n == 0:
            return 0
        b = self.bit
        if b + n > self.end_bit:
            raise ZstdError("header past the end of its section")
        i = b >> 3
        v = int.from_bytes(self.data[i:i + 5], "little") >> (b & 7)
        self.bit = b + n
        return v & ((1 << n) - 1)
    def rewind(self, n: int) -> None:
        self.bit -= n
    def byte_pos(self) -> int:
        return (self.bit + 7) >> 3
class _Back:
    __slots__ = ("data", "off")
    def __init__(self, data: bytes) -> None:
        if not data or data[-1] == 0:
            raise ZstdError("bitstream without its end marker")
        self.data = data
        self.off = len(data) * 8 - (8 - _hb(data[-1]))
    def read(self, n: int) -> int:
        if n == 0:
            return 0
        off = self.off - n
        self.off = off
        if off >= 0:
            i = off >> 3
            return (int.from_bytes(self.data[i:i + 5], "little") >> (off & 7)) & ((1 << n) - 1)
        have = n + off
        if have <= 0:
            return 0
        return (int.from_bytes(self.data[0:5], "little") & ((1 << have) - 1)) << (-off)
class _FSE:
    __slots__ = ("log", "sym", "nb", "base")
    def __init__(self, log: int, sym: List[int], nb: List[int], base: List[int]) -> None:
        self.log, self.sym, self.nb, self.base = log, sym, nb, base
def _fse_table(norm: List[int], log: int) -> _FSE:
    size = 1 << log
    sym = [0] * size
    state_desc = [0] * len(norm)
    high = size
    for s, f in enumerate(norm):
        if f == -1:
            high -= 1
            sym[high] = s
            state_desc[s] = 1
    step = (size >> 1) + (size >> 3) + 3
    mask = size - 1
    pos = 0
    for s, f in enumerate(norm):
        if f <= 0:
            continue
        state_desc[s] = f
        for _ in range(f):
            sym[pos] = s
            pos = (pos + step) & mask
            while pos >= high:
                pos = (pos + step) & mask
    if pos != 0:
        raise ZstdError("bad FSE distribution")
    nb = [0] * size
    base = [0] * size
    for i in range(size):
        s = sym[i]
        d = state_desc[s]
        state_desc[s] = d + 1
        b = log - _hb(d)
        nb[i] = b
        base[i] = (d << b) - size
    return _FSE(log, sym, nb, base)
def _fse_rle(symbol: int) -> _FSE:
    return _FSE(0, [symbol], [0], [0])
def _fse_header(data: bytes, pos: int, end: int, max_log: int) -> Tuple[_FSE, int]:
    r = _Fwd(data, pos, end)
    log = r.read(4) + 5
    if log > max_log:
        raise ZstdError("FSE accuracy too high")
    remaining = 1 << log
    norm: List[int] = []
    while remaining > 0 and len(norm) < 256:
        bits = _hb(remaining + 1) + 1
        val = r.read(bits)
        lower = (1 << (bits - 1)) - 1
        threshold = (1 << bits) - 1 - (remaining + 1)
        if (val & lower) < threshold:
            r.rewind(1)
            val &= lower
        elif val > lower:
            val -= threshold
        p = val - 1
        remaining -= -p if p < 0 else p
        norm.append(p)
        if p == 0:
            while True:
                rep = r.read(2)
                norm.extend([0] * rep)
                if rep != 3:
                    break
    if remaining != 0 or len(norm) >= 256:
        raise ZstdError("bad FSE header")
    return _fse_table(norm, log), r.byte_pos()
class _Huf:
    __slots__ = ("max_bits", "sym", "nb")
    def __init__(self, max_bits: int, sym: List[int], nb: List[int]) -> None:
        self.max_bits, self.sym, self.nb = max_bits, sym, nb
def _huf_from_weights(weights: List[int]) -> _Huf:
    if len(weights) + 1 > 256:
        raise ZstdError("too many Huffman symbols")
    total = 0
    for w in weights:
        if w > 11:
            raise ZstdError("bad Huffman weight")
        if w:
            total += 1 << (w - 1)
    if total == 0:
        raise ZstdError("empty Huffman table")
    max_bits = _hb(total) + 1
    left = (1 << max_bits) - total
    if left & (left - 1):
        raise ZstdError("bad Huffman weights")
    bits = [(max_bits + 1 - w) if w else 0 for w in weights] + [max_bits + 1 - (_hb(left) + 1)]
    if max_bits > 11:
        raise ZstdError("Huffman codes too long")
    count = [0] * (max_bits + 1)
    for b in bits:
        count[b] += 1
    size = 1 << max_bits
    rank = [0] * (max_bits + 1)
    nb = [0] * size
    idx = 0
    for b in range(max_bits, 0, -1):
        rank[b] = idx
        span = count[b] << (max_bits - b)
        nb[idx:idx + span] = [b] * span
        idx += span
    if idx != size:
        raise ZstdError("bad Huffman table")
    sym = [0] * size
    for s, b in enumerate(bits):
        if b:
            n = 1 << (max_bits - b)
            c = rank[b]
            sym[c:c + n] = [s] * n
            rank[b] = c + n
    return _Huf(max_bits, sym, nb)
def _huf_table(data: bytes, pos: int, end: int) -> Tuple[_Huf, int]:
    if pos >= end:
        raise ZstdError("missing Huffman table")
    head = data[pos]
    pos += 1
    if head >= 128:
        n = head - 127
        nbytes = (n + 1) // 2
        if pos + nbytes > end:
            raise ZstdError("Huffman weights past the end")
        w = []
        for i in range(n):
            b = data[pos + i // 2]
            w.append(b >> 4 if i % 2 == 0 else b & 15)
        return _huf_from_weights(w), pos + nbytes
    if pos + head > end:
        raise ZstdError("Huffman weights past the end")
    fse, p2 = _fse_header(data, pos, pos + head, 7)
    stream = data[p2:pos + head]
    br = _Back(stream)
    s1 = br.read(fse.log)
    s2 = br.read(fse.log)
    w = []
    sym, nb, base = fse.sym, fse.nb, fse.base
    while True:
        w.append(sym[s1])
        s1 = base[s1] + br.read(nb[s1])
        if br.off < 0:
            w.append(sym[s2])
            break
        w.append(sym[s2])
        s2 = base[s2] + br.read(nb[s2])
        if br.off < 0:
            w.append(sym[s1])
            break
        if len(w) > 255:
            raise ZstdError("too many Huffman weights")
    return _huf_from_weights(w), pos + head
def _huf_stream(h: _Huf, stream: bytes, out: bytearray) -> None:
    br = _Back(stream)
    mb = h.max_bits
    mask = (1 << mb) - 1
    sym, nb = h.sym, h.nb
    state = br.read(mb)
    data = stream
    off = br.off
    app = out.append
    while off > -mb:
        app(sym[state])
        n = nb[state]
        off -= n
        if off >= 0:
            i = off >> 3
            rest = (int.from_bytes(data[i:i + 3], "little") >> (off & 7)) & ((1 << n) - 1)
        else:
            have = n + off
            rest = ((int.from_bytes(data[0:3], "little") & ((1 << have) - 1)) << (-off)) if have > 0 else 0
        state = ((state << n) + rest) & mask
    if off != -mb:
        raise ZstdError("Huffman stream not consumed exactly")
LL_BASE = list(range(16)) + [16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384,
                             32768, 65536]
LL_BITS = [0] * 16 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
ML_BASE = list(range(3, 35)) + [35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051, 4099, 8195,
                                16387, 32771, 65539]
ML_BITS = [0] * 32 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
LL_DEFAULT = [4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
              -1, -1, -1, -1]
OF_DEFAULT = [1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1]
ML_DEFAULT = [1, 4, 3, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
              1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1]
_PREDEFINED = {"ll": (LL_DEFAULT, 6), "of": (OF_DEFAULT, 5), "ml": (ML_DEFAULT, 6)}
_MAX_LOG = {"ll": 9, "of": 8, "ml": 9}
_MAX_CODE = {"ll": 35, "of": 31, "ml": 52}
class _Frame:
    def __init__(self) -> None:
        self.huf: Optional[_Huf] = None
        self.tables = {"ll": None, "of": None, "ml": None}
        self.rep = [1, 4, 8]
def _seq_table(fr: _Frame, kind: str, mode: int, data: bytes, pos: int, end: int) -> int:
    if mode == 0:
        norm, log = _PREDEFINED[kind]
        fr.tables[kind] = _fse_table(norm, log)
    elif mode == 1:
        if pos >= end:
            raise ZstdError("missing RLE symbol")
        if data[pos] > _MAX_CODE[kind]:
            raise ZstdError("bad RLE symbol")
        fr.tables[kind] = _fse_rle(data[pos])
        pos += 1
    elif mode == 2:
        fr.tables[kind], pos = _fse_header(data, pos, end, _MAX_LOG[kind])
    elif fr.tables[kind] is None:
        raise ZstdError("repeat table with no earlier table")
    return pos
def _literals(fr: _Frame, data: bytes, pos: int, end: int) -> Tuple[bytes, int]:
    b0 = data[pos]
    typ = b0 & 3
    fmt = (b0 >> 2) & 3
    if typ <= 1:
        if fmt in (0, 2):
            size, pos = b0 >> 3, pos + 1
        elif fmt == 1:
            size, pos = (b0 >> 4) + (data[pos + 1] << 4), pos + 2
        else:
            size, pos = (b0 >> 4) + (data[pos + 1] << 4) + (data[pos + 2] << 12), pos + 3
        if typ == 0:
            if pos + size > end:
                raise ZstdError("raw literals past the end")
            return bytes(data[pos:pos + size]), pos + size
        if pos >= end:
            raise ZstdError("RLE literals past the end")
        return bytes([data[pos]]) * size, pos + 1
    streams = 1 if fmt == 0 else 4
    hsize, bits = ((3, 10), (3, 10), (4, 14), (5, 18))[fmt]
    h = int.from_bytes(data[pos:pos + hsize], "little")
    regen = (h >> 4) & ((1 << bits) - 1)
    comp = (h >> (4 + bits)) & ((1 << bits) - 1)
    pos += hsize
    stop = pos + comp
    if stop > end:
        raise ZstdError("compressed literals past the end")
    if typ == 2:
        fr.huf, pos = _huf_table(data, pos, stop)
    elif fr.huf is None:
        raise ZstdError("treeless literals with no earlier table")
    out = bytearray()
    if streams == 1:
        _huf_stream(fr.huf, data[pos:stop], out)
    else:
        if pos + 6 > stop:
            raise ZstdError("bad jump table")
        s1, s2, s3 = struct.unpack_from("<3H", data, pos)
        pos += 6
        bounds = [pos, pos + s1, pos + s1 + s2, pos + s1 + s2 + s3, stop]
        if bounds[3] > stop:
            raise ZstdError("bad jump table")
        for a, b in zip(bounds, bounds[1:]):
            _huf_stream(fr.huf, data[a:b], out)
    if len(out) != regen:
        raise ZstdError("literals size mismatch")
    return bytes(out), stop
def _block(fr: _Frame, data: bytes, pos: int, end: int, out: bytearray) -> None:
    lits, pos = _literals(fr, data, pos, end)
    if pos >= end:
        raise ZstdError("missing sequences section")
    b0 = data[pos]
    if b0 == 0:
        out += lits
        return
    if b0 < 128:
        nseq, pos = b0, pos + 1
    elif b0 < 255:
        nseq, pos = ((b0 - 128) << 8) + data[pos + 1], pos + 2
    else:
        nseq, pos = data[pos + 1] + (data[pos + 2] << 8) + 0x7F00, pos + 3
    modes = data[pos]
    pos += 1
    if modes & 3:
        raise ZstdError("reserved bits set")
    pos = _seq_table(fr, "ll", (modes >> 6) & 3, data, pos, end)
    pos = _seq_table(fr, "of", (modes >> 4) & 3, data, pos, end)
    pos = _seq_table(fr, "ml", (modes >> 2) & 3, data, pos, end)
    br = _Back(data[pos:end])
    llt, oft, mlt = fr.tables["ll"], fr.tables["of"], fr.tables["ml"]
    lls = br.read(llt.log)
    ofs = br.read(oft.log)
    mls = br.read(mlt.log)
    rep = fr.rep
    lp = 0
    nlits = len(lits)
    read = br.read
    for i in range(nseq):
        ofc = oft.sym[ofs]
        llc = llt.sym[lls]
        mlc = mlt.sym[mls]
        if llc > 35 or mlc > 52 or ofc > 31:
            raise ZstdError("bad sequence code")
        ofv = (1 << ofc) + read(ofc)
        ml = ML_BASE[mlc] + read(ML_BITS[mlc])
        ll = LL_BASE[llc] + read(LL_BITS[llc])
        if i != nseq - 1:
            lls = llt.base[lls] + read(llt.nb[lls])
            mls = mlt.base[mls] + read(mlt.nb[mls])
            ofs = oft.base[ofs] + read(oft.nb[ofs])
        if ll:
            if lp + ll > nlits:
                raise ZstdError("literals overrun")
            out += lits[lp:lp + ll]
            lp += ll
        if ofv > 3:
            off = ofv - 3
            rep[2], rep[1], rep[0] = rep[1], rep[0], off
        else:
            idx = ofv - 1 + (1 if ll == 0 else 0)
            if idx == 0:
                off = rep[0]
            else:
                off = rep[idx] if idx < 3 else rep[0] - 1
                if idx > 1:
                    rep[2] = rep[1]
                rep[1] = rep[0]
                rep[0] = off
        n = len(out)
        if off <= 0 or off > n:
            raise ZstdError("match offset before the start")
        start = n - off
        if off >= ml:
            out += out[start:start + ml]
        else:
            chunk = bytes(out[start:])
            out += (chunk * (ml // off + 1))[:ml]
        if len(out) > MAX_OUTPUT:
            raise ZstdError("output too large")
    if br.off != 0:
        raise ZstdError("sequence stream not consumed exactly")
    out += lits[lp:]
_P1, _P2, _P3, _P4, _P5 = (11400714785074694791, 14029467366897019727, 1609587929392839161, 9650029242287828579,
                           2870177450012600261)
_M = (1 << 64) - 1
def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & _M
def _round(acc: int, v: int) -> int:
    return (_rotl((acc + v * _P2) & _M, 31) * _P1) & _M
def xxh64(data: bytes, seed: int = 0) -> int:
    n = len(data)
    i = 0
    if n >= 32:
        v1, v2, v3, v4 = (seed + _P1 + _P2) & _M, (seed + _P2) & _M, seed, (seed - _P1) & _M
        unpack = struct.unpack_from
        while i + 32 <= n:
            a, b, c, d = unpack("<4Q", data, i)
            v1 = (_rotl((v1 + a * _P2) & _M, 31) * _P1) & _M
            v2 = (_rotl((v2 + b * _P2) & _M, 31) * _P1) & _M
            v3 = (_rotl((v3 + c * _P2) & _M, 31) * _P1) & _M
            v4 = (_rotl((v4 + d * _P2) & _M, 31) * _P1) & _M
            i += 32
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _M
        for v in (v1, v2, v3, v4):
            h = ((h ^ _round(0, v)) * _P1 + _P4) & _M
    else:
        h = (seed + _P5) & _M
    h = (h + n) & _M
    while i + 8 <= n:
        h = (_rotl(h ^ _round(0, struct.unpack_from("<Q", data, i)[0]), 27) * _P1 + _P4) & _M
        i += 8
    if i + 4 <= n:
        h = (_rotl(h ^ ((struct.unpack_from("<I", data, i)[0] * _P1) & _M), 23) * _P2 + _P3) & _M
        i += 4
    while i < n:
        h = (_rotl(h ^ ((data[i] * _P5) & _M), 11) * _P1) & _M
        i += 1
    h ^= h >> 33
    h = (h * _P2) & _M
    h ^= h >> 29
    h = (h * _P3) & _M
    h ^= h >> 32
    return h
def _frame(data: bytes, pos: int, out: bytearray) -> int:
    start_out = len(out)
    fhd = data[pos]
    pos += 1
    fcs_flag, single, reserved, checksum, did_flag = fhd >> 6, (fhd >> 5) & 1, (fhd >> 3) & 1, (fhd >> 2) & 1, fhd & 3
    if reserved:
        raise ZstdError("reserved frame bit set")
    if not single:
        pos += 1
    did_size = (0, 1, 2, 4)[did_flag]
    if did_size and int.from_bytes(data[pos:pos + did_size], "little"):
        raise ZstdError("frames with a dictionary are not supported")
    pos += did_size
    fcs_size = (1 if single else 0, 2, 4, 8)[fcs_flag]
    fcs: Optional[int] = None
    if fcs_size:
        fcs = int.from_bytes(data[pos:pos + fcs_size], "little") + (256 if fcs_size == 2 else 0)
        pos += fcs_size
    fr = _Frame()
    while True:
        if pos + 3 > len(data):
            raise ZstdError("cut block header")
        bh = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16)
        pos += 3
        last, btype, bsize = bh & 1, (bh >> 1) & 3, bh >> 3
        if btype == 0:
            if pos + bsize > len(data):
                raise ZstdError("cut raw block")
            out += data[pos:pos + bsize]
            pos += bsize
        elif btype == 1:
            if pos >= len(data):
                raise ZstdError("cut RLE block")
            out += bytes([data[pos]]) * bsize
            pos += 1
        elif btype == 2:
            if pos + bsize > len(data):
                raise ZstdError("cut compressed block")
            _block(fr, data, pos, pos + bsize, out)
            pos += bsize
        else:
            raise ZstdError("reserved block type")
        if len(out) > MAX_OUTPUT:
            raise ZstdError("output too large")
        if last:
            break
    if fcs is not None and len(out) - start_out != fcs:
        raise ZstdError("content size mismatch")
    if checksum:
        if pos + 4 > len(data):
            raise ZstdError("cut checksum")
        want = int.from_bytes(data[pos:pos + 4], "little")
        if xxh64(bytes(out[start_out:])) & 0xFFFFFFFF != want:
            raise ZstdError("checksum mismatch")
        pos += 4
    return pos
def decompress(data: bytes) -> bytes:
    data = bytes(data)
    out = bytearray()
    pos = 0
    n = len(data)
    while pos < n:
        if pos + 4 > n:
            raise ZstdError("cut frame header")
        magic = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        if magic == MAGIC:
            pos = _frame(data, pos, out)
        elif magic & 0xFFFFFFF0 == 0x184D2A50:
            if pos + 4 > n:
                raise ZstdError("cut skippable frame")
            pos += 4 + int.from_bytes(data[pos:pos + 4], "little")
        else:
            raise ZstdError("not a zstd frame")
    return bytes(out)
