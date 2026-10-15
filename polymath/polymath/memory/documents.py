"""Document store: every item the senses collect, with provenance and license.

Bodies are stored as lzma-compressed JSON (text + link spans + extras), which
keeps the NVMe budget small; ``meta`` holds small searchable fields. When the main disk fills up, bodies
can be *spilled* to a plugged-in drive (``codec = 'spilled'``, pointer in ``meta.spill``; see
:mod:`polymath.memory.pool`). They read transparently; while the drive is away they read as empty, with
``meta.unavailable`` set.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from polymath.core.db import Database
from polymath.memory import pool as storage_pool


@dataclass
class Document:
    source: str
    external_id: str
    title: str
    text: str
    license: str
    url: str | None = None
    license_url: str | None = None
    lang: str | None = "en"
    published: str | None = None
    links: list[tuple[int, int, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # stored compressed with the body


@dataclass
class StoredDocument:
    id: int
    source: str
    external_id: str
    title: str
    url: str | None
    license: str
    license_url: str | None
    lang: str | None
    published: str | None
    text: str
    links: list[tuple[int, int, str]]
    meta: dict[str, Any]
    extra: dict[str, Any]
    state: str


def content_hash(text: str) -> str:
    norm = " ".join(text.split()).lower()
    return hashlib.blake2b(norm.encode("utf-8"), digest_size=16).hexdigest()


def encode_body(payload: dict[str, Any], codec: str = "lzma") -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if codec == "lzma":
        return lzma.compress(raw, preset=1)
    if codec == "zlib":
        return zlib.compress(raw, 6)
    return raw


def decode_body(blob: bytes | None, codec: str) -> dict[str, Any]:
    if blob is None or codec == "evicted":
        return {"text": "", "links": [], "extra": {}}
    if codec == "lzma":
        raw = lzma.decompress(blob)
    elif codec == "zlib":
        raw = zlib.decompress(blob)
    else:
        raw = blob
    data: dict[str, Any] = json.loads(raw)
    return data


EMPTY_BODY: dict[str, Any] = {"text": "", "links": [], "extra": {}}


def load_body(row: Any) -> tuple[dict[str, Any], bool]:
    """A document row's body, wherever it is stored. ``(body, available)``: False while its drive is away."""
    if row["codec"] != "spilled":
        return decode_body(row["body"], row["codec"]), True
    try:
        ref = json.loads(row["meta"]).get("spill") or {}
    except ValueError:
        ref = {}
    p = storage_pool.active()
    blob = p.read_body(ref, int(row["id"])) if p is not None and ref else None
    if blob is None:
        return dict(EMPTY_BODY), False
    return decode_body(blob, str(ref.get("codec", "lzma"))), True


class DocumentStore:
    def __init__(self, db: Database, codec: str = "lzma") -> None:
        self.db = db
        self.codec = codec

    def register_source(
        self, name: str, license: str, license_url: str | None, homepage: str | None, description: str
    ) -> None:
        self.db.execute(
            "INSERT INTO sources(name, license, license_url, homepage, description) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET license=excluded.license, license_url=excluded.license_url, "
            "homepage=excluded.homepage, description=excluded.description",
            (name, license, license_url, homepage, description),
        )

    def add(self, doc: Document) -> tuple[int, str]:
        """Insert or update. Returns ``(id, status)`` with status new|updated|unchanged|duplicate."""
        if not doc.license:
            raise ValueError("every document needs license metadata")
        h = content_hash(doc.text)
        row = self.db.one(
            "SELECT id, content_hash, codec, body, meta FROM documents WHERE source=? AND external_id=?",
            (doc.source, doc.external_id),
        )
        body = encode_body({"text": doc.text, "links": doc.links, "extra": doc.extra}, self.codec)
        meta = json.dumps(doc.meta, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        if row is not None:
            if row["content_hash"] == h:
                return int(row["id"]), "unchanged"
            if row["codec"] == "spilled" and not load_body(row)[1]:
                # its old passages can only leave the index with the old text: update when the drive is back
                return int(row["id"]), "unchanged"
            self._forget_index(int(row["id"]))
            self.db.execute(
                "UPDATE documents SET title=?, url=?, license=?, license_url=?, lang=?, published=?, fetched=?, "
                "content_hash=?, nchars=?, codec=?, body=?, meta=?, state='new' WHERE id=?",
                (
                    doc.title,
                    doc.url,
                    doc.license,
                    doc.license_url,
                    doc.lang,
                    doc.published,
                    now,
                    h,
                    len(doc.text),
                    self.codec,
                    body,
                    meta,
                    row["id"],
                ),
            )
            return int(row["id"]), "updated"
        dup = self.db.one("SELECT id FROM documents WHERE content_hash=? AND state!='duplicate' LIMIT 1", (h,))
        state = "duplicate" if dup is not None else "new"
        cur = self.db.execute(
            "INSERT INTO documents(source, external_id, title, url, license, license_url, lang, published, fetched, "
            "content_hash, nchars, codec, body, meta, state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc.source,
                doc.external_id,
                doc.title,
                doc.url,
                doc.license,
                doc.license_url,
                doc.lang,
                doc.published,
                now,
                h,
                len(doc.text),
                self.codec if state == "new" else "evicted",
                body if state == "new" else None,
                meta,
                state,
            ),
        )
        return int(cur.lastrowid or 0), state

    def _forget_index(self, doc_id: int) -> None:
        """Before a body changes, remove its passages from the (contentless) FTS index and its signatures."""
        old = self.db.one("SELECT id, title, body, codec, meta FROM documents WHERE id=?", (doc_id,))
        chunks = self.db.query("SELECT id, start, end FROM chunks WHERE doc_id=?", (doc_id,))
        if old is not None and chunks:
            text = load_body(old)[0].get("text", "")
            for c in chunks:
                self.db.execute(
                    "INSERT INTO chunk_fts(chunk_fts, rowid, title, body) VALUES('delete', ?, ?, ?)",
                    (c["id"], old["title"], text[c["start"] : c["end"]]),
                )
            self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        from polymath.memory.dedup import NearDupIndex

        NearDupIndex(self.db).remove(doc_id)

    def get(self, doc_id: int) -> StoredDocument | None:
        row = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if row is None:
            return None
        body, available = load_body(row)
        meta = json.loads(row["meta"])
        if not available:
            meta["unavailable"] = True
        return StoredDocument(
            id=int(row["id"]),
            source=row["source"],
            external_id=row["external_id"],
            title=row["title"],
            url=row["url"],
            license=row["license"],
            license_url=row["license_url"],
            lang=row["lang"],
            published=row["published"],
            text=body.get("text", ""),
            links=[(int(x[0]), int(x[1]), str(x[2])) for x in body.get("links", [])],
            meta=meta,
            extra=body.get("extra", {}),
            state=row["state"],
        )

    def count(self, source: str | None = None) -> int:
        if source:
            return int(self.db.scalar("SELECT COUNT(*) FROM documents WHERE source=?", (source,), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM documents", default=0))
