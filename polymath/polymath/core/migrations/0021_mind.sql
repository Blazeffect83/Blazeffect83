-- v0.13.0: distilled knowledge, traceable facts, the dream cycle, procedures, the scientist, the red team, curiosity

-- distilled Wikidata (memory.distill): once linked, an entity keeps the hash of its claims, not the claims
ALTER TABLE wd_entities ADD COLUMN claims_hash TEXT;

-- every piece of evidence remembers the job slice that found it (interface.explain: why? traces back through it)
ALTER TABLE provenance ADD COLUMN job_id INTEGER;
ALTER TABLE provenance ADD COLUMN agent_id INTEGER;
CREATE INDEX provenance_job ON provenance (job_id) WHERE job_id IS NOT NULL;

-- the dream cycle (drive.dream): one row a night
CREATE TABLE dreams (
    id        INTEGER PRIMARY KEY,
    started   REAL NOT NULL,
    finished  REAL,
    before    TEXT NOT NULL DEFAULT '{}',  -- JSON: the held-out quiz and counts before
    after     TEXT NOT NULL DEFAULT '{}',  -- …and after
    steps     TEXT NOT NULL DEFAULT '{}',  -- JSON: what each phase did
    plan      TEXT NOT NULL DEFAULT '{}',  -- JSON: the plan for the day it chose by simulating alternatives
    report    TEXT NOT NULL DEFAULT ''     -- the morning report
);

-- procedures agents invented (agents.procedures): sequences of repertoire steps, run by a fixed interpreter
CREATE TABLE procedures (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL,               -- the agent kind that found it (others may use it)
    steps     TEXT NOT NULL,               -- JSON: [{"op": action, "args": {...}}, ...]
    found_by  INTEGER,                     -- agents.id
    support   INTEGER NOT NULL DEFAULT 0,  -- how often the sequence worked before it was saved
    mean      REAL NOT NULL DEFAULT 0,     -- its mean reward then
    uses      INTEGER NOT NULL DEFAULT 0,
    wins      REAL NOT NULL DEFAULT 0,     -- reward earned since
    state     TEXT NOT NULL DEFAULT 'active',  -- active | retired
    created   REAL NOT NULL
);

-- the scientist (agents.scientist): hypotheses tested on one half of the evidence, kept only if the other half agrees
CREATE TABLE hypotheses (
    id        INTEGER PRIMARY KEY,
    family    TEXT NOT NULL,               -- pattern_reliable | source_reliable | rule_holds
    subject   TEXT NOT NULL,               -- what it is about (a pattern id, a source, a rule)
    claim     TEXT NOT NULL,               -- in words
    state     TEXT NOT NULL DEFAULT 'proposed',  -- proposed | supported | refuted | inconclusive
    train_n   INTEGER NOT NULL DEFAULT 0,
    train_k   INTEGER NOT NULL DEFAULT 0,
    test_n    INTEGER NOT NULL DEFAULT 0,
    test_k    INTEGER NOT NULL DEFAULT 0,
    p_value   REAL,
    applied   TEXT NOT NULL DEFAULT '',    -- what changed because of it
    created   REAL NOT NULL,
    decided   REAL,
    UNIQUE (family, subject)
);

-- the red team (agents.redteam): attacks that got through become permanent regression cases, re-run every day
CREATE TABLE redteam_cases (
    id        INTEGER PRIMARY KEY,
    defense   TEXT NOT NULL,               -- which defense it attacks
    payload   TEXT NOT NULL,               -- JSON: the input
    found     REAL NOT NULL,
    last_run  REAL,
    passing   INTEGER NOT NULL DEFAULT 0,  -- 1 once the defense stops it
    detail    TEXT NOT NULL DEFAULT '',
    UNIQUE (defense, payload)
);

-- curiosity (drive.curiosity): how unsure it is about each topic, day by day
CREATE TABLE topic_uncertainty (
    topic_id  INTEGER NOT NULL,
    day       TEXT NOT NULL,
    u         REAL NOT NULL,               -- 0 sure … 1 knows nothing
    PRIMARY KEY (topic_id, day)
) WITHOUT ROWID;
