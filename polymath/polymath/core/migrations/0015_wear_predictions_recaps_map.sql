-- SD card wear guard: disk writes per device, sampled every 15 minutes (body.wear).
CREATE TABLE disk_writes (
    at          REAL NOT NULL,
    device      TEXT NOT NULL,
    role        TEXT NOT NULL,              -- data | system
    kind        TEXT NOT NULL,              -- sd | mmc | ssd | hdd | other
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL,           -- written to the device since the previous sample
    agent_bytes INTEGER NOT NULL DEFAULT 0, -- of which by the agent process
    PRIMARY KEY (at, device)
);

-- Predictions: facts the link predictor guessed before reading them, checked later (reasoning.predictions).
CREATE TABLE predictions (
    id         INTEGER PRIMARY KEY,
    s          INTEGER NOT NULL,
    p          INTEGER NOT NULL,
    o          INTEGER NOT NULL,           -- the predicted object
    score      REAL NOT NULL,              -- the predictor's confidence
    runner_up  INTEGER,                    -- its second choice
    made_at    REAL NOT NULL,
    state      TEXT NOT NULL DEFAULT 'open',  -- open | confirmed | refuted | expired
    checked_at REAL,
    truth      INTEGER,                    -- the object it later read (when refuted)
    triple_id  INTEGER,                    -- the fact that settled it
    UNIQUE (s, p)
);
CREATE INDEX predictions_state ON predictions(state, made_at);

-- Weekly recap (evaluation.recap).
CREATE TABLE recaps (
    id      INTEGER PRIMARY KEY,
    week    TEXT NOT NULL UNIQUE,          -- ISO year-week, e.g. 2026-W41
    created REAL NOT NULL,
    data    TEXT NOT NULL
);

-- Knowledge map time-lapse: each topic's size once a day (interface.knowledge_map).
CREATE TABLE topic_snapshots (
    day      TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    docs     INTEGER NOT NULL,
    facts    INTEGER NOT NULL,
    PRIMARY KEY (day, topic_id)
);

-- Fewer pages written per job slice: jobs_kind (kind, state) is a prefix of jobs_kind_ready (kind, state,
-- not_before), so it only cost writes. Every state change of a job rewrote an entry in it.
DROP INDEX IF EXISTS jobs_kind;
