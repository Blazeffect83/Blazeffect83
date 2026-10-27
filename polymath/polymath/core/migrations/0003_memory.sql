-- Memory: passage index (FTS5), near-duplicate signatures, knowledge graph, topic map.

-- Documents are split into passages (chunks); FTS indexes passages. The FTS
-- table is contentless (text lives once, compressed, in documents.body).
CREATE TABLE chunks (
    id      INTEGER PRIMARY KEY,
    doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    start   INTEGER NOT NULL,
    end     INTEGER NOT NULL,
    UNIQUE (doc_id, ord)
);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    title, body, content='', tokenize='porter unicode61 remove_diacritics 2'
);
-- Per-term passage frequencies (used to drop near-stopwords from queries).
CREATE VIRTUAL TABLE chunk_vocab USING fts5vocab(chunk_fts, 'row');

ALTER TABLE documents ADD COLUMN simhash INTEGER;
ALTER TABLE documents ADD COLUMN near_dup_of INTEGER;
CREATE TABLE minhash_bands (
    band   INTEGER NOT NULL,
    h      INTEGER NOT NULL,
    doc_id INTEGER NOT NULL,
    PRIMARY KEY (band, h, doc_id)
) WITHOUT ROWID;
CREATE TABLE minhash_sigs (
    doc_id INTEGER PRIMARY KEY,
    sig    BLOB NOT NULL
);

-- Knowledge graph -----------------------------------------------------------
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,          -- 'Q90' or 'wiki:Paris'
    label       TEXT NOT NULL,
    description TEXT,
    kind        TEXT NOT NULL DEFAULT 'item',  -- item | stub | property
    wiki_title  TEXT,
    doc_id      INTEGER,
    pagerank    REAL NOT NULL DEFAULT 0,
    degree      INTEGER NOT NULL DEFAULT 0,
    updated     REAL NOT NULL
);
CREATE INDEX entities_wiki ON entities (wiki_title);
CREATE INDEX entities_label ON entities (label);

CREATE TABLE aliases (
    alias     TEXT NOT NULL,                   -- normalised surface form
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source    TEXT NOT NULL,                   -- label | alias | redirect | anchor | title
    count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (alias, entity_id, source)
) WITHOUT ROWID;
CREATE INDEX aliases_entity ON aliases (entity_id);

CREATE TABLE predicates (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,          -- 'P36', 'infobox:capital', 'pattern:...'
    label       TEXT NOT NULL,
    inverse_id  INTEGER,
    transitive  INTEGER NOT NULL DEFAULT 0,
    symmetric   INTEGER NOT NULL DEFAULT 0,
    functional  REAL,                          -- learned: share of subjects with a single object
    domain_type INTEGER,                       -- learned: dominant P31 class of subjects
    range_type  INTEGER,                       -- learned: dominant P31 class of objects
    datatype    TEXT
);

CREATE TABLE triples (
    id         INTEGER PRIMARY KEY,
    s          INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    p          INTEGER NOT NULL REFERENCES predicates(id),
    o          INTEGER NOT NULL DEFAULT 0,     -- entity id, or 0 for a literal
    value      TEXT NOT NULL DEFAULT '',       -- literal JSON when o = 0
    status     TEXT NOT NULL CHECK (status IN ('sourced', 'inferred', 'disputed')),
    confidence REAL NOT NULL DEFAULT 0.5,
    n_sources  INTEGER NOT NULL DEFAULT 0,
    holdout    INTEGER NOT NULL DEFAULT 0,     -- hidden from reasoning/answering (self-evaluation)
    created    REAL NOT NULL,
    updated    REAL NOT NULL,
    UNIQUE (s, p, o, value)
);
CREATE INDEX triples_sp ON triples (s, p);
CREATE INDEX triples_s_conf ON triples (s, confidence DESC);  -- neighbour lists, best first, LIMIT stops early
CREATE INDEX triples_o_conf ON triples (o, confidence DESC);  -- not partial: o = ? must be able to use it
CREATE INDEX triples_op ON triples (o, p);
CREATE INDEX triples_p ON triples (p, status);

CREATE TABLE provenance (
    id        INTEGER PRIMARY KEY,
    triple_id INTEGER NOT NULL REFERENCES triples(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,                   -- wikidata | infobox | pattern | rule
    doc_id    INTEGER NOT NULL DEFAULT 0,      -- 0 when the evidence is not a document
    source    TEXT NOT NULL,                   -- source name for reliability learning
    detail    TEXT NOT NULL DEFAULT '',        -- pattern id / rule + premises / sentence
    weight    REAL NOT NULL DEFAULT 1.0,
    created   REAL NOT NULL,
    UNIQUE (triple_id, kind, source, doc_id, detail)
);
CREATE INDEX provenance_triple ON provenance (triple_id);
CREATE INDEX provenance_doc ON provenance (doc_id);

-- Topic map --------------------------------------------------------------------
CREATE TABLE topics (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL DEFAULT 'label',   -- category | mesh | concept | tag | subject | cluster
    level     INTEGER,                         -- depth below the roots (computed)
    n_docs    INTEGER NOT NULL DEFAULT 0,      -- direct documents
    n_total   INTEGER NOT NULL DEFAULT 0,      -- documents in the subtree (rolled up)
    entity_id INTEGER
);
CREATE TABLE topic_edges (
    child  INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    parent INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    PRIMARY KEY (child, parent)
) WITHOUT ROWID;
CREATE INDEX topic_edges_parent ON topic_edges (parent);
CREATE TABLE doc_topics (
    doc_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    weight   REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (doc_id, topic_id)
) WITHOUT ROWID;
CREATE INDEX doc_topics_topic ON doc_topics (topic_id);

CREATE TABLE wiki_category_edges (
    lang   TEXT NOT NULL,
    child  TEXT NOT NULL,
    parent TEXT NOT NULL,
    PRIMARY KEY (lang, child, parent)
) WITHOUT ROWID;
