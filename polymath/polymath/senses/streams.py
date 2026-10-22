"""Readers for compressed dump formats with fine-grained resume points.

* :class:`Bz2BlockReader` — random access into *single-stream* bz2 files (the
  Wikidata dump) by locating bz2 block boundaries at bit level and wrapping each
  block into a tiny standalone bz2 stream. A checkpoint is a bit offset, so a
  100 GB dump resumes instantly instead of being decompressed from the start.
* :func:`multistream_ranges` — byte ranges of independent bz2 streams from a
  Wikipedia multistream index.
* :func:`iter_gzip_lines` / :func:`iter_xml_elements` — streaming decoders
  for gzip JSON-lines and large XML files with bounded memory.
"""

from __future__ import annotations

import bz2
import itertools
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

BLOCK_MAGIC = 0x314159265359
EOS_MAGIC = 0x177245385090
_MAGIC_BITS = 48


@dataclass(frozen=True)
class _Pattern:
    shift: int
    core: bytes
    core_offset: int


def _patterns(magic: int) -> list[_Pattern]:
    pats = []
    for s in range(8):
        total = s + _MAGIC_BITS
        nbytes = (total + 7) // 8
        raw = (magic << (nbytes * 8 - total)).to_bytes(nbytes, "big")
        first_full = 0 if s == 0 else 1
        pats.append(_Pattern(s, raw[first_full : total // 8], first_full))
    return pats


_BLOCK_PATTERNS = _patterns(BLOCK_MAGIC)
_EOS_PATTERNS = _patterns(EOS_MAGIC)


def read_bits(data: bytes, bit_start: int, nbits: int) -> int:
    start = bit_start // 8
    end = (bit_start + nbits + 7) // 8
    value = int.from_bytes(data[start:end], "big")
    value >>= end * 8 - (bit_start + nbits)
    return value & ((1 << nbits) - 1)


def find_magic(data: bytes, magic: int, patterns: list[_Pattern], from_bit: int = 0) -> list[int]:
    """All bit offsets in ``data`` where the 48-bit ``magic`` starts (>= ``from_bit``)."""
    found: set[int] = set()
    limit_bits = len(data) * 8 - _MAGIC_BITS
    for pat in patterns:
        pos = max(0, from_bit // 8 - 1)
        while True:
            idx = data.find(pat.core, pos)
            if idx < 0:
                break
            bit = (idx - pat.core_offset) * 8 + pat.shift
            if from_bit <= bit <= limit_bits and bit >= 0 and read_bits(data, bit, _MAGIC_BITS) == magic:
                found.add(bit)
            pos = idx + 1
    return sorted(found)


def block_boundaries(data: bytes, from_bit: int = 0) -> tuple[list[int], list[int]]:
    return find_magic(data, BLOCK_MAGIC, _BLOCK_PATTERNS, from_bit), find_magic(
        data, EOS_MAGIC, _EOS_PATTERNS, from_bit
    )


def decode_block(data: bytes, start_bit: int, end_bit: int, level: int = 9) -> bytes:
    """Decompress the single bz2 block occupying bits [start_bit, end_bit) of ``data``."""
    nbits = end_bit - start_bit
    block = read_bits(data, start_bit, nbits)
    crc = read_bits(data, start_bit + _MAGIC_BITS, 32)
    total = nbits + _MAGIC_BITS + 32
    pad = (-total) % 8
    stream = (((block << _MAGIC_BITS) | EOS_MAGIC) << 32 | crc) << pad
    raw = b"BZh" + str(level).encode() + stream.to_bytes((total + pad) // 8, "big")
    return bz2.decompress(raw)


Fetch = Callable[[int, int], bytes]  # (start_byte, end_byte_exclusive) -> bytes


@dataclass
class DecodedBlock:
    start_bit: int
    end_bit: int
    data: bytes


class Bz2BlockReader:
    """Block-at-a-time access to a single-stream bz2 file through ``fetch``.

    ``fetch`` may be a local file read or an HTTP range request; ``size`` is the
    number of bytes currently available (a download may still be growing).
    """

    def __init__(self, fetch: Fetch, size: int, *, window: int = 8 << 20, level: int | None = None) -> None:
        self.fetch = fetch
        self.size = size
        self.window = window
        if level is None:
            head = fetch(0, 4)
            if head[:3] != b"BZh" or not head[3:4].isdigit():
                raise ValueError("not a bz2 file")
            level = int(head[3:4])
        self.level = level

    def first_block(self) -> int:
        return 32  # 'BZh9' header is 4 bytes; the first block magic follows immediately

    def blocks(self, start_bit: int) -> Iterator[DecodedBlock]:
        """Yield successive blocks starting at ``start_bit`` (must be a block start)."""
        bit = start_bit
        while bit // 8 < self.size:
            base = bit // 8
            end_byte = min(self.size, base + self.window)
            data = self.fetch(base, end_byte)
            rel = bit - base * 8
            if read_bits(data, rel, _MAGIC_BITS) != BLOCK_MAGIC:
                if read_bits(data, rel, _MAGIC_BITS) == EOS_MAGIC:
                    nxt = self._after_eos(data, rel, base)
                    if nxt is None:
                        return
                    bit = nxt
                    continue
                raise ValueError(f"no bz2 block at bit {bit}")
            blocks, eoss = block_boundaries(data, rel + 1)
            ends = sorted(set(blocks) | set(eoss))
            if not ends:
                if end_byte >= self.size:
                    return  # last block not fully available yet
                self.window *= 2  # a block larger than the window (should not happen: max ~900 kB)
                continue
            cur = rel
            eos_set = set(eoss)
            for nxt in ends:
                try:
                    out = decode_block(data, cur, nxt, self.level)
                except (OSError, ValueError):
                    continue  # a magic-like bit pattern inside compressed data (p ~ 2^-48 per bit)
                yield DecodedBlock(base * 8 + cur, base * 8 + nxt, out)
                if nxt in eos_set:
                    after = self._after_eos(data, nxt, base)
                    if after is None:
                        return
                    bit = after
                    break
                cur = nxt
            else:
                if cur == rel:
                    if end_byte >= self.size:
                        return
                    self.window *= 2
                    continue
                bit = base * 8 + cur

    def _after_eos(self, data: bytes, rel_eos: int, base: int) -> int | None:
        """Concatenated streams: skip EOS + CRC + padding + next 'BZhN' header."""
        after = rel_eos + _MAGIC_BITS + 32
        next_byte = base + (after + 7) // 8
        if next_byte + 4 >= self.size:
            return None
        head = self.fetch(next_byte, next_byte + 4)
        if head[:3] != b"BZh":
            return None
        return (next_byte + 4) * 8


@dataclass
class LineCursor:
    """Resume point for line-oriented data inside a block stream."""

    block_bit: int
    offset: int  # byte offset inside the decompressed block where the next line starts


def iter_block_lines(reader: Bz2BlockReader, cursor: LineCursor) -> Iterator[tuple[bytes, LineCursor]]:
    """Yield ``(line, cursor_after_line)``; the cursor always points at a line start."""
    carry = b""
    first = True
    for block in reader.blocks(cursor.block_bit):
        data = block.data[cursor.offset :] if first else block.data
        base_off = cursor.offset if first else 0
        first = False
        buf = carry + data
        start = 0
        carry_len = len(carry)
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            line = buf[start:nl]
            start = nl + 1
            # position of the next line start, expressed relative to this block
            in_block = start - carry_len
            yield line, LineCursor(block.start_bit, base_off + in_block)
        carry = buf[start:]


def multistream_ranges(index_lines: Iterable[str], file_size: int | None = None) -> list[tuple[int, int]]:
    """Byte ranges [start, end) of the bz2 streams named in a multistream index.

    Index lines look like ``offset:page_id:title``. The final stream's end is
    ``file_size`` when known (it also holds ``</mediawiki>``).
    """
    offsets = sorted({int(line.split(":", 1)[0]) for line in index_lines if line and line[0].isdigit()})
    ranges = list(itertools.pairwise(offsets))
    if offsets and file_size is not None and file_size > offsets[-1]:
        ranges.append((offsets[-1], file_size))
    return ranges


def iter_gzip_chunks(path: Path, *, limit: int | None = None, chunk: int = 1 << 20) -> Iterator[bytes]:
    """Decompress a (possibly multi-member, possibly truncated) gzip file."""
    remaining = limit
    with open(path, "rb") as fh:
        dec = zlib.decompressobj(wbits=31)
        while True:
            size = chunk if remaining is None else min(chunk, remaining)
            if size <= 0:
                break
            raw = fh.read(size)
            if remaining is not None:
                remaining -= len(raw)
            if not raw:
                break
            while raw:
                try:
                    out = dec.decompress(raw)
                except zlib.error:
                    return  # truncated/corrupt tail of a sampled file
                if out:
                    yield out
                if dec.eof:
                    raw = dec.unused_data
                    dec = zlib.decompressobj(wbits=31)
                else:
                    raw = b""


def iter_lines(chunks: Iterable[bytes], *, final_partial: bool = False) -> Iterator[bytes]:
    carry = b""
    for chunk in chunks:
        buf = carry + chunk
        parts = buf.split(b"\n")
        carry = parts.pop()
        yield from parts
    if final_partial and carry:
        yield carry


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def iter_xml_elements(chunks: Iterable[bytes], tags: set[str]) -> Iterator[ET.Element]:
    """Stream elements whose local name is in ``tags``; memory stays bounded.

    Truncated input (a sampled partial file) simply ends the iteration.
    """
    parser: ET.XMLPullParser[ET.Element] = ET.XMLPullParser(events=("start", "end"))
    root: ET.Element | None = None

    def drain() -> Iterator[ET.Element]:
        nonlocal root
        for item in parser.read_events():
            event: Any = item[0]
            elem: Any = item[-1]
            if not isinstance(elem, ET.Element):
                continue
            if event == "start":
                if root is None:
                    root = elem
            elif local_name(elem.tag) in tags:
                yield elem
                if root is not None:
                    root.clear()

    try:
        for chunk in chunks:
            parser.feed(chunk)
            yield from drain()
        parser.close()
        yield from drain()
    except ET.ParseError:
        return


def file_fetcher(path: Path) -> Fetch:
    def fetch(start: int, end: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(max(0, end - start))

    return fetch
