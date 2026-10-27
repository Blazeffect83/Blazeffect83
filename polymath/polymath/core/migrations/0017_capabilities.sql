-- v0.5: time, sanity, numbers and places, causes, categories, how-to, a second language, merged duplicates.

-- when a fact holds: Wikidata qualifiers start time (P580), end time (P582), point in time (P585)
CREATE TABLE fact_time (
    triple_id  INTEGER PRIMARY KEY REFERENCES triples(id) ON DELETE CASCADE,
    valid_from TEXT,                 -- YYYY[-MM[-DD]]
    valid_to   TEXT,
    at_time    TEXT
);

-- facts that cannot be true (death before birth, a part bigger than its whole, far outside a learned range)
CREATE TABLE sanity (
    triple_id INTEGER PRIMARY KEY REFERENCES triples(id) ON DELETE CASCADE,
    rule      TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    action    TEXT NOT NULL,         -- disputed | noted
    at        REAL NOT NULL
);

-- the normal range of each numeric relation, learned from its values (log scale, median ± k·MAD)
CREATE TABLE value_ranges (
    p        INTEGER NOT NULL,
    unit     TEXT NOT NULL,
    n        INTEGER NOT NULL,
    median   REAL NOT NULL,
    mad      REAL NOT NULL,
    lo       REAL NOT NULL,
    hi       REAL NOT NULL,
    positive INTEGER NOT NULL,       -- every value seen was > 0
    updated  REAL NOT NULL,
    PRIMARY KEY (p, unit)
) WITHOUT ROWID;

-- where things are (from Wikidata coordinates, P625)
CREATE TABLE geo (
    entity_id INTEGER PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    lat       REAL NOT NULL,
    lon       REAL NOT NULL
);
CREATE INDEX geo_lat ON geo(lat);

-- what a kind of thing can or cannot do, read from text ("birds can fly", "penguins cannot fly")
CREATE TABLE category_props (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    prop      TEXT NOT NULL,         -- normalised verb phrase: "fly", "have feathers"
    polarity  INTEGER NOT NULL,      -- 1 can / does, 0 cannot / does not
    count     INTEGER NOT NULL DEFAULT 1,
    doc_id    INTEGER NOT NULL DEFAULT 0,
    sentence  TEXT NOT NULL DEFAULT '',
    updated   REAL NOT NULL,
    PRIMARY KEY (entity_id, prop, polarity)
) WITHOUT ROWID;
CREATE INDEX category_props_prop ON category_props(prop);

-- step-by-step procedures from Stack Exchange answers
CREATE TABLE howto (
    doc_id   INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    steps    TEXT NOT NULL,          -- JSON list of steps
    score    INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    site     TEXT NOT NULL DEFAULT '',
    created  REAL NOT NULL
);
CREATE VIRTUAL TABLE howto_fts USING fts5(question, doc_id UNINDEXED, tokenize='porter unicode61');

-- titles of the same thing in other Wikipedia languages (from Wikidata sitelinks)
CREATE TABLE wiki_sitelinks (
    lang  TEXT NOT NULL,
    title TEXT NOT NULL,
    qid   TEXT NOT NULL,
    PRIMARY KEY (lang, title)
) WITHOUT ROWID;
CREATE INDEX wiki_sitelinks_qid ON wiki_sitelinks(qid);

-- duplicate entries folded into one
CREATE TABLE entity_merges (
    dropped_key TEXT PRIMARY KEY,
    kept_id     INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    at          REAL NOT NULL
);

-- best-known entities first (contemporaries, events of a year, nearby places)
CREATE INDEX IF NOT EXISTS entities_pagerank ON entities(pagerank);
