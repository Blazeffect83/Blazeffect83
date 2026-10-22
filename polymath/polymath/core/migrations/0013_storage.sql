-- Storage pool: drives plugged in and adopted as extra brain space, and what happened to them.
CREATE TABLE volumes (
    id           TEXT PRIMARY KEY,               -- filesystem UUID
    label        TEXT NOT NULL,
    fstype       TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    size_bytes   INTEGER NOT NULL,
    budget_bytes INTEGER NOT NULL,               -- how much of it Polymath may use
    dedicated    INTEGER NOT NULL DEFAULT 0,     -- formatted for / labelled for Polymath (vs. shared with files)
    path         TEXT NOT NULL,                  -- the brain folder on the drive
    online       INTEGER NOT NULL DEFAULT 0,
    used_bytes   INTEGER NOT NULL DEFAULT 0,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    retired      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE volume_events (
    id        INTEGER PRIMARY KEY,
    at        REAL NOT NULL,
    volume_id TEXT NOT NULL,
    event     TEXT NOT NULL,                     -- added | online | offline | retired | full
    detail    TEXT NOT NULL DEFAULT ''
);
