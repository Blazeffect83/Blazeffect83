-- Senses: sources with license metadata, compressed documents, Wikidata staging,
-- redirects, crawl frontier, robots cache, feeds.
CREATE TABLE sources (
    name        TEXT PRIMARY KEY,
    license     TEXT NOT NULL,
    license_url TEXT,
    homepage    TEXT,
    description TEXT
);

CREATE TABLE documents (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT,
    license      TEXT NOT NULL,
    license_url  TEXT,
    lang         TEXT,
    published    TEXT,
    fetched      REAL NOT NULL,
    content_hash TEXT NOT NULL,
    nchars       INTEGER NOT NULL,
    codec        TEXT NOT NULL DEFAULT 'lzma',   -- lzma | zlib | none | evicted
    body         BLOB,                            -- compressed JSON {text, links, extra}
    meta         TEXT NOT NULL DEFAULT '{}',      -- small JSON: kind, categories, authors, year...
    state        TEXT NOT NULL DEFAULT 'new',     -- new | perceived | duplicate
    UNIQUE (source, external_id)
);
CREATE INDEX documents_hash ON documents (content_hash);
CREATE INDEX documents_state ON documents (state, id);
CREATE INDEX documents_title ON documents (title);

CREATE TABLE wiki_redirects (
    lang   TEXT NOT NULL,
    title  TEXT NOT NULL,
    target TEXT NOT NULL,
    PRIMARY KEY (lang, title)
) WITHOUT ROWID;

CREATE TABLE wd_entities (
    qid         TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT,
    aliases     TEXT NOT NULL DEFAULT '[]',
    enwiki      TEXT,
    claims      TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'new',
    fetched     REAL NOT NULL
) WITHOUT ROWID;
CREATE INDEX wd_entities_enwiki ON wd_entities (enwiki);
CREATE INDEX wd_entities_state ON wd_entities (state);

CREATE TABLE hosts (
    host           TEXT PRIMARY KEY,
    robots         TEXT,             -- raw robots.txt (capped)
    robots_status  INTEGER,
    robots_fetched REAL,
    crawl_delay    REAL,
    next_fetch_at  REAL NOT NULL DEFAULT 0,
    tokens         REAL NOT NULL DEFAULT 1,     -- token bucket level (capacity 1)
    bucket_ts      REAL NOT NULL DEFAULT 0,     -- time the level was last computed
    errors         INTEGER NOT NULL DEFAULT 0,
    fetched        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE frontier (
    url          TEXT PRIMARY KEY,
    host         TEXT NOT NULL,
    priority     REAL NOT NULL DEFAULT 0,
    depth        INTEGER NOT NULL DEFAULT 0,
    referrer     TEXT,
    state        TEXT NOT NULL DEFAULT 'new',   -- new | done | failed | skipped
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    added        REAL NOT NULL,
    fetched_at   REAL,
    http_status  INTEGER,
    reason       TEXT,
    etag         TEXT,
    last_modified TEXT
);
CREATE INDEX frontier_next ON frontier (state, next_attempt, priority DESC);
CREATE INDEX frontier_host ON frontier (host, state);

CREATE TABLE feeds (
    url           TEXT PRIMARY KEY,
    title         TEXT,
    etag          TEXT,
    last_modified TEXT,
    last_checked  REAL,
    items_seen    INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0
);
