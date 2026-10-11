"""Minimal 7z archive reader (stdlib ``lzma`` does the LZMA/LZMA2 decoding).

Supports what the Stack Exchange data dumps use: encoded (LZMA-compressed)
headers, solid folders with LZMA, LZMA2, BCJ(x86), Delta and Copy coders, and
streaming extraction of one entry without holding the archive in memory.
PPMd, AES and multi-input coders (BCJ2) are rejected with a clear error.
"""

from __future__ import annotations

import lzma
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

SIGNATURE = b"7z\xbc\xaf\x27\x1c"

K_END, K_HEADER, K_ARCHIVE_PROPERTIES, K_ADDITIONAL_STREAMS, K_MAIN_STREAMS = 0x00, 0x01, 0x02, 0x03, 0x04
K_FILES_INFO, K_PACK_INFO, K_UNPACK_INFO, K_SUBSTREAMS_INFO, K_SIZE, K_CRC = 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A
K_FOLDER, K_CODERS_UNPACK_SIZE, K_NUM_UNPACK_STREAM, K_EMPTY_STREAM, K_EMPTY_FILE = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
K_NAME, K_ENCODED_HEADER, K_DUMMY = 0x11, 0x17, 0x19

CODER_COPY = b"\x00"
CODER_LZMA = b"\x03\x01\x01"
CODER_LZMA2 = b"\x21"
CODER_BCJ = b"\x03\x03\x01\x03"
CODER_DELTA = b"\x03"


class SevenZipError(ValueError):
    pass


@dataclass
class Coder:
    method: bytes
    num_in: int
    num_out: int
    props: bytes


@dataclass
class Folder:
    coders: list[Coder]
    bind_pairs: list[tuple[int, int]]
    packed_streams: list[int]
    unpack_sizes: list[int] = field(default_factory=list)
    crc: int | None = None

    def final_size(self) -> int:
        bound_outs = {out for _in, out in self.bind_pairs}
        for i, size in enumerate(self.unpack_sizes):
            if i not in bound_outs:
                return size
        return self.unpack_sizes[-1]


@dataclass
class Entry:
    name: str
    size: int
    folder: int | None
    offset: int  # offset of this entry within its folder's unpacked data
    crc: int | None
    is_dir: bool = False


class _Buf:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.i = 0

    def byte(self) -> int:
        if self.i >= len(self.d):
            raise SevenZipError("truncated header")
        b = self.d[self.i]
        self.i += 1
        return b

    def bytes(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise SevenZipError("truncated header")
        out = self.d[self.i : self.i + n]
        self.i += n
        return out

    def u32(self) -> int:
        return int(struct.unpack("<I", self.bytes(4))[0])

    def number(self) -> int:
        first = self.byte()
        mask = 0x80
        value = 0
        for i in range(8):
            if first & mask == 0:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def bits(self, n: int) -> list[bool]:
        out: list[bool] = []
        mask = 0
        cur = 0
        for _ in range(n):
            if mask == 0:
                cur = self.byte()
                mask = 0x80
            out.append(bool(cur & mask))
            mask >>= 1
        return out

    def defined_bits(self, n: int) -> list[bool]:
        all_defined = self.byte()
        return [True] * n if all_defined else self.bits(n)


@dataclass
class _Streams:
    pack_pos: int = 0
    pack_sizes: list[int] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)
    substream_counts: list[int] = field(default_factory=list)
    substream_sizes: list[int] = field(default_factory=list)
    substream_crcs: list[int | None] = field(default_factory=list)


def _read_digests(b: _Buf, n: int) -> list[int | None]:
    defined = b.defined_bits(n)
    return [b.u32() if d else None for d in defined]


def _read_pack_info(b: _Buf, s: _Streams) -> None:
    s.pack_pos = b.number()
    n = b.number()
    while True:
        prop = b.byte()
        if prop == K_END:
            break
        if prop == K_SIZE:
            s.pack_sizes = [b.number() for _ in range(n)]
        elif prop == K_CRC:
            _read_digests(b, n)
        else:
            raise SevenZipError(f"unexpected pack-info property {prop}")


def _read_folder(b: _Buf) -> Folder:
    coders = []
    for _ in range(b.number()):
        flag = b.byte()
        method = b.bytes(flag & 0x0F)
        num_in = num_out = 1
        if flag & 0x10:
            num_in, num_out = b.number(), b.number()
        props = b.bytes(b.number()) if flag & 0x20 else b""
        if flag & 0x80:
            raise SevenZipError("alternative coder methods are not supported")
        coders.append(Coder(method, num_in, num_out, props))
    total_out = sum(c.num_out for c in coders)
    total_in = sum(c.num_in for c in coders)
    binds = [(b.number(), b.number()) for _ in range(total_out - 1)]
    n_packed = total_in - len(binds)
    if n_packed == 1:
        bound_ins = {i for i, _o in binds}
        packed = [next(i for i in range(total_in) if i not in bound_ins)]
    else:
        packed = [b.number() for _ in range(n_packed)]
    return Folder(coders, binds, packed)


def _read_unpack_info(b: _Buf, s: _Streams) -> None:
    if b.byte() != K_FOLDER:
        raise SevenZipError("expected folder list")
    n = b.number()
    if b.byte() != 0:
        raise SevenZipError("external folder data is not supported")
    s.folders = [_read_folder(b) for _ in range(n)]
    if b.byte() != K_CODERS_UNPACK_SIZE:
        raise SevenZipError("expected coder unpack sizes")
    for f in s.folders:
        f.unpack_sizes = [b.number() for _ in range(sum(c.num_out for c in f.coders))]
    while True:
        prop = b.byte()
        if prop == K_END:
            break
        if prop == K_CRC:
            for f, crc in zip(s.folders, _read_digests(b, n)):
                f.crc = crc
        else:
            raise SevenZipError(f"unexpected unpack-info property {prop}")


def _read_substreams(b: _Buf, s: _Streams) -> None:
    counts = [1] * len(s.folders)
    prop = b.byte()
    if prop == K_NUM_UNPACK_STREAM:
        counts = [b.number() for _ in s.folders]
        prop = b.byte()
    sizes: list[int] = []
    if prop == K_SIZE:
        for f, c in zip(s.folders, counts):
            if c == 0:
                continue
            parts = [b.number() for _ in range(c - 1)]
            sizes.extend(parts)
            sizes.append(f.final_size() - sum(parts))
        prop = b.byte()
    else:
        for f, c in zip(s.folders, counts):
            if c == 1:
                sizes.append(f.final_size())
    crcs: list[int | None] = []
    if prop == K_CRC:
        unknown = sum(c for f, c in zip(s.folders, counts) if not (c == 1 and f.crc is not None))
        digests = _read_digests(b, unknown)
        it = iter(digests)
        for f, c in zip(s.folders, counts):
            if c == 1 and f.crc is not None:
                crcs.append(f.crc)
            else:
                crcs.extend(next(it) for _ in range(c))
        prop = b.byte()
    if prop != K_END:
        raise SevenZipError("malformed substreams info")
    s.substream_counts = counts
    s.substream_sizes = sizes
    s.substream_crcs = crcs or [f.crc if c == 1 else None for f, c in zip(s.folders, counts) for _ in range(c)]


def _read_streams_info(b: _Buf) -> _Streams:
    s = _Streams()
    while True:
        prop = b.byte()
        if prop == K_END:
            break
        if prop == K_PACK_INFO:
            _read_pack_info(b, s)
        elif prop == K_UNPACK_INFO:
            _read_unpack_info(b, s)
        elif prop == K_SUBSTREAMS_INFO:
            _read_substreams(b, s)
        else:
            raise SevenZipError(f"unexpected streams-info property {prop}")
    if not s.substream_counts:
        s.substream_counts = [1] * len(s.folders)
        s.substream_sizes = [f.final_size() for f in s.folders]
        s.substream_crcs = [f.crc for f in s.folders]
    return s


def _lzma_filters(coder: Coder) -> dict[str, int]:
    if coder.method == CODER_LZMA:
        if len(coder.props) < 5:
            raise SevenZipError("bad LZMA properties")
        d = coder.props[0]
        lc, rest = d % 9, d // 9
        lp, pb = rest % 5, rest // 5
        dict_size = int.from_bytes(coder.props[1:5], "little")
        return {"id": lzma.FILTER_LZMA1, "dict_size": max(dict_size, 4096), "lc": lc, "lp": lp, "pb": pb}
    if coder.method == CODER_LZMA2:
        p = coder.props[0] if coder.props else 0
        dict_size = 0xFFFFFFFF if p >= 40 else (2 | (p & 1)) << (p // 2 + 11)
        return {"id": lzma.FILTER_LZMA2, "dict_size": dict_size}
    if coder.method == CODER_BCJ:
        return {"id": lzma.FILTER_X86}
    if coder.method == CODER_DELTA:
        return {"id": lzma.FILTER_DELTA, "dist": (coder.props[0] + 1) if coder.props else 1}
    raise SevenZipError(f"unsupported 7z coder {coder.method.hex()} (PPMd/AES/BCJ2 are not implemented)")


def _folder_filters(folder: Folder) -> list[dict[str, int]] | None:
    """liblzma raw filter chain for a folder whose coders form a simple chain.

    The chain starts at the coder producing the folder's final (unbound) output
    and follows bind pairs towards the packed stream; that is also liblzma's
    *encoder* order (e.g. ``[BCJ, LZMA]``), which ``FORMAT_RAW`` expects.
    """
    if any(c.num_in != 1 or c.num_out != 1 for c in folder.coders):
        raise SevenZipError("multi-stream coders (e.g. BCJ2) are not supported")
    bound_outs = {out for _in, out in folder.bind_pairs}
    current = next(i for i in range(len(folder.coders)) if i not in bound_outs)
    chain = [current]
    while len(chain) < len(folder.coders):
        nxt = next((out for inp, out in folder.bind_pairs if inp == current), None)
        if nxt is None:
            break
        chain.append(nxt)
        current = nxt
    methods = [folder.coders[i] for i in chain]
    if len(methods) == 1 and methods[0].method == CODER_COPY:
        return None
    return [_lzma_filters(c) for c in methods if c.method != CODER_COPY]


class SevenZipFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with open(self.path, "rb") as fh:
            sig = fh.read(32)
            if len(sig) < 32 or sig[:6] != SIGNATURE:
                raise SevenZipError("not a 7z archive")
            next_off, next_size, next_crc = struct.unpack("<QQI", sig[12:32])
            if zlib.crc32(sig[12:32]) != struct.unpack("<I", sig[8:12])[0]:
                raise SevenZipError("start header CRC mismatch")
            fh.seek(32 + next_off)
            header = fh.read(next_size)
        if len(header) != next_size:
            raise SevenZipError("archive is truncated (incomplete download?)")
        if zlib.crc32(header) != next_crc:
            raise SevenZipError("header CRC mismatch")
        b = _Buf(header)
        kind = b.byte()
        while kind == K_ENCODED_HEADER:
            s = _read_streams_info(b)
            data = b"".join(self._decode_folder(s, 0))
            b = _Buf(data)
            kind = b.byte()
        if kind != K_HEADER:
            raise SevenZipError(f"unexpected header type {kind}")
        self.streams, self.entries = self._read_header(b)

    def _read_header(self, b: _Buf) -> tuple[_Streams, list[Entry]]:
        streams = _Streams()
        names: list[str] = []
        empty_stream: list[bool] = []
        empty_file: list[bool] = []
        n_files = 0
        while True:
            prop = b.byte()
            if prop == K_END:
                break
            if prop == K_ARCHIVE_PROPERTIES:
                while b.byte() != K_END:
                    b.bytes(b.number())
            elif prop == K_ADDITIONAL_STREAMS:
                _read_streams_info(b)
            elif prop == K_MAIN_STREAMS:
                streams = _read_streams_info(b)
            elif prop == K_FILES_INFO:
                n_files = b.number()
                while True:
                    ptype = b.byte()
                    if ptype == K_END:
                        break
                    size = b.number()
                    sub = _Buf(b.bytes(size))
                    if ptype == K_EMPTY_STREAM:
                        empty_stream = sub.bits(n_files)
                    elif ptype == K_EMPTY_FILE:
                        empty_file = sub.bits(empty_stream.count(True))
                    elif ptype == K_NAME:
                        if sub.byte() != 0:
                            raise SevenZipError("external names are not supported")
                        raw = sub.d[sub.i :].decode("utf-16-le")
                        names = raw.split("\x00")[:n_files]
            else:
                raise SevenZipError(f"unexpected header property {prop}")
        entries: list[Entry] = []
        folder = 0
        in_folder = 0
        offset = 0
        stream_idx = 0
        empty_idx = 0
        for i in range(n_files):
            name = names[i] if i < len(names) else f"file{i}"
            if empty_stream and empty_stream[i]:
                is_dir = not (empty_file and empty_idx < len(empty_file) and empty_file[empty_idx])
                empty_idx += 1
                entries.append(Entry(name, 0, None, 0, None, is_dir))
                continue
            while folder < len(streams.substream_counts) and in_folder >= streams.substream_counts[folder]:
                folder += 1
                in_folder = 0
                offset = 0
            size = streams.substream_sizes[stream_idx]
            crc = streams.substream_crcs[stream_idx] if stream_idx < len(streams.substream_crcs) else None
            entries.append(Entry(name, size, folder, offset, crc))
            offset += size
            in_folder += 1
            stream_idx += 1
        return streams, entries

    def _decode_folder(self, s: _Streams, index: int, chunk: int = 1 << 20) -> Iterator[bytes]:
        folder = s.folders[index]
        pack_index = sum(len(f.packed_streams) for f in s.folders[:index])
        start = 32 + s.pack_pos + sum(s.pack_sizes[:pack_index])
        left = s.pack_sizes[pack_index]
        remaining = folder.final_size()
        filters = _folder_filters(folder)
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters) if filters else None
        with open(self.path, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                raw = b""
                if dec is None or dec.needs_input:
                    raw = fh.read(min(chunk, left))
                    left -= len(raw)
                    if not raw:
                        raise SevenZipError("archive is truncated")
                out = raw[:remaining] if dec is None else dec.decompress(raw, max_length=min(remaining, 8 << 20))
                remaining -= len(out)
                if out:
                    yield out

    def open(self, name: str, chunk: int = 1 << 20) -> Iterator[bytes]:
        """Stream the bytes of entry ``name`` (decoding preceding solid data as needed)."""
        entry = next((e for e in self.entries if e.name == name or e.name.rsplit("/", 1)[-1] == name), None)
        if entry is None:
            raise KeyError(name)
        if entry.folder is None:
            return
        skip = entry.offset
        left = entry.size
        crc = 0
        for block in self._decode_folder(self.streams, entry.folder, chunk):
            if skip >= len(block):
                skip -= len(block)
                continue
            part = block[skip : skip + left]
            skip = 0
            left -= len(part)
            crc = zlib.crc32(part, crc)
            yield part
            if left <= 0:
                break
        if left > 0:
            raise SevenZipError(f"{name}: archive ended early")
        if entry.crc is not None and crc != entry.crc:
            raise SevenZipError(f"{name}: CRC mismatch")

    def read(self, name: str) -> bytes:
        return b"".join(self.open(name))

    def names(self) -> list[str]:
        return [e.name for e in self.entries if not e.is_dir]
