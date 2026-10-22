-- Orin the caretaker (agents/caretaker.py, docs/CARETAKER.md C.9): its incidents, what fixed what, and the
-- normal ranges it learns from Polymath's own readings.
CREATE TABLE incidents (
    id           INTEGER PRIMARY KEY,
    key          TEXT NOT NULL,                  -- one open incident per key (its root symptom)
    kind         TEXT NOT NULL,                  -- the incident model (caretaker.MODELS) or 'inbox'
    area         TEXT NOT NULL,
    fault        TEXT NOT NULL DEFAULT '',       -- the known fault (agents/faults.toml) it is, if any
    title        TEXT NOT NULL,
    opened       REAL NOT NULL,
    updated      REAL NOT NULL,
    state        TEXT NOT NULL,                  -- open | acting | verifying | escalated | resolved | closed
    severity     TEXT NOT NULL DEFAULT 'warn',   -- info | warn | bad
    symptoms     TEXT NOT NULL DEFAULT '[]',     -- JSON: readings that opened and kept it open
    hypotheses   TEXT NOT NULL DEFAULT '{}',     -- JSON: cause → probability
    checks       TEXT NOT NULL DEFAULT '[]',     -- JSON: checks run and what they showed
    actions      TEXT NOT NULL DEFAULT '[]',     -- JSON: remedies applied (and when to verify them)
    issue_ids    TEXT NOT NULL DEFAULT '[]',     -- JSON: inbox issues (events ids) it took on
    advice       TEXT NOT NULL DEFAULT '',       -- the message to Christian, when it was escalated
    escalated_at REAL,
    outcome      TEXT NOT NULL DEFAULT ''        -- fixed | fixed itself | escalated | gone
);
CREATE INDEX incidents_state ON incidents (state, key);
CREATE INDEX incidents_opened ON incidents (opened);

-- what works: Beta(a, b) per (cause, remedy) from verified outcomes; remedy '' counts how often a cause was it
CREATE TABLE fault_stats (
    cause  TEXT NOT NULL,
    remedy TEXT NOT NULL,
    a      REAL NOT NULL DEFAULT 1,
    b      REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (cause, remedy)
) WITHOUT ROWID;

-- its senses over time (one sample per signal every 10 minutes, a week kept) and the normal band of each
CREATE TABLE signal_samples (
    signal TEXT NOT NULL,
    at     REAL NOT NULL,
    value  REAL NOT NULL,
    PRIMARY KEY (signal, at)
) WITHOUT ROWID;

CREATE TABLE signal_bands (
    signal  TEXT PRIMARY KEY,
    median  REAL NOT NULL,
    mad     REAL NOT NULL,
    n       INTEGER NOT NULL,
    updated REAL NOT NULL
);
