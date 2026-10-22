-- Reasoning: learned source reliability, learned rules, run log.
CREATE TABLE source_reliability (
    source      TEXT PRIMARY KEY,
    reliability REAL NOT NULL,
    n_facts     INTEGER NOT NULL,
    updated     REAL NOT NULL
);

CREATE TABLE rules (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('transitive', 'inverse', 'symmetric', 'domain', 'range')),
    p          INTEGER NOT NULL REFERENCES predicates(id),
    q          INTEGER,                       -- inverse: the other predicate; domain/range: the class entity
    confidence REAL NOT NULL,
    support    INTEGER NOT NULL,
    updated    REAL NOT NULL,
    UNIQUE (kind, p, q)
);

CREATE TABLE reasoning_runs (
    id      INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,
    started REAL NOT NULL,
    ended   REAL NOT NULL,
    result  TEXT NOT NULL
);
