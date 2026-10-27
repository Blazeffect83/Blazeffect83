-- Core: key/value state, durable job queue, cycle log.
CREATE TABLE kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL UNIQUE,          -- idempotency key
    payload      TEXT NOT NULL DEFAULT '{}',
    checkpoint   TEXT,                           -- resumable progress (JSON)
    state        TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'dead')),
    priority     REAL NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,     -- failed slices (exceptions)
    crashes      INTEGER NOT NULL DEFAULT 0,     -- process deaths while running
    max_attempts INTEGER NOT NULL DEFAULT 5,
    not_before   REAL NOT NULL DEFAULT 0,
    lease_until  REAL,
    slices       INTEGER NOT NULL DEFAULT 0,
    cpu_seconds  REAL NOT NULL DEFAULT 0,
    value        REAL NOT NULL DEFAULT 0,        -- cumulative value produced
    created      REAL NOT NULL,
    updated      REAL NOT NULL,
    last_error   TEXT,
    result       TEXT
);
CREATE INDEX jobs_ready ON jobs (state, not_before, priority DESC);
CREATE INDEX jobs_kind ON jobs (kind, state);

CREATE TABLE cycles (
    id           INTEGER PRIMARY KEY,
    started      REAL NOT NULL,
    ended        REAL NOT NULL,
    action       TEXT,
    job_id       INTEGER,
    status       TEXT NOT NULL,                  -- idle | done | continue | failed | paused
    cpu_seconds  REAL NOT NULL DEFAULT 0,
    wall_seconds REAL NOT NULL DEFAULT 0,
    value        REAL NOT NULL DEFAULT 0,
    reward       REAL NOT NULL DEFAULT 0,
    detail       TEXT
);
CREATE INDEX cycles_ended ON cycles (ended);

-- Effect log of the built-in no-op job; proves exactly-once effects across crashes.
CREATE TABLE noop_log (
    job_key TEXT PRIMARY KEY,
    cycle   INTEGER NOT NULL,
    at      REAL NOT NULL
);
