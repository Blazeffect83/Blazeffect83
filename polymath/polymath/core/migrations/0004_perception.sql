-- Perception: n-gram statistics and phrases, surface statistics for linking, mentions,
-- relation-learning contexts and patterns, infobox↔property alignment.
ALTER TABLE documents ADD COLUMN stage INTEGER NOT NULL DEFAULT 0;  -- 0 new, 1 anchored, 2 perceived
CREATE INDEX documents_stage ON documents (stage, state, id);

CREATE TABLE ngram_counts (
    n     INTEGER NOT NULL,
    gram  TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (n, gram)
) WITHOUT ROWID;

CREATE TABLE phrases (
    phrase TEXT PRIMARY KEY,
    n      INTEGER NOT NULL,
    count  INTEGER NOT NULL,
    npmi   REAL NOT NULL
) WITHOUT ROWID;

-- How often a surface form is linked (Wikipedia anchors) vs. merely seen: "keyphraseness".
CREATE TABLE surface_stats (
    alias  TEXT PRIMARY KEY,
    linked INTEGER NOT NULL DEFAULT 0,   -- documents where it is a link
    seen   INTEGER NOT NULL DEFAULT 0    -- documents where it occurs at all
) WITHOUT ROWID;

CREATE TABLE doc_entities (
    doc_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    count     INTEGER NOT NULL,
    score     REAL NOT NULL,
    PRIMARY KEY (doc_id, entity_id)
) WITHOUT ROWID;
CREATE INDEX doc_entities_entity ON doc_entities (entity_id);

-- Sentences mentioning two linked entities, queued for relation learning.
CREATE TABLE pair_contexts (
    id       INTEGER PRIMARY KEY,
    e1       INTEGER NOT NULL,
    e2       INTEGER NOT NULL,
    doc_id   INTEGER NOT NULL,
    left_ctx TEXT NOT NULL,
    middle   TEXT NOT NULL,
    right_ctx TEXT NOT NULL,
    sentence TEXT NOT NULL,
    created  REAL NOT NULL
);
CREATE INDEX pair_contexts_pair ON pair_contexts (e1, e2);

CREATE TABLE patterns (
    id           INTEGER PRIMARY KEY,
    predicate_id INTEGER NOT NULL REFERENCES predicates(id),
    middle       TEXT NOT NULL,          -- normalised token sequence between the entities
    order_flag   INTEGER NOT NULL,       -- 0: subject first, 1: object first
    positive     INTEGER NOT NULL DEFAULT 0,
    negative     INTEGER NOT NULL DEFAULT 0,
    unknown      INTEGER NOT NULL DEFAULT 0,
    confidence   REAL NOT NULL DEFAULT 0,
    UNIQUE (predicate_id, middle, order_flag)
);
CREATE INDEX patterns_middle ON patterns (middle);

CREATE TABLE entity_profiles (
    entity_id INTEGER PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    terms     TEXT NOT NULL              -- JSON {term: weight}, L2-normalised
);

CREATE TABLE infobox_alignment (
    infobox_key TEXT NOT NULL,
    pid         TEXT NOT NULL,
    agree       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (infobox_key, pid)
) WITHOUT ROWID;
