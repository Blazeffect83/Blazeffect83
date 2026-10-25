-- v0.11.0: what a drive that went away held, and relearning it (memory/inventory.py)
CREATE TABLE relearn (
    doc_id    INTEGER PRIMARY KEY,
    volume_id TEXT NOT NULL,                   -- the drive its body was on
    priority  REAL NOT NULL,                   -- higher first: facts that rest on it, your interests, size
    how       TEXT NOT NULL,                   -- wikipedia | web | later (comes back when its dump is read again)
    state     TEXT NOT NULL DEFAULT 'waiting', -- waiting | queued | done | back (the drive came back)
    added     REAL NOT NULL,
    queued    REAL,
    done      REAL
);
CREATE INDEX relearn_state ON relearn (state, priority DESC);
CREATE INDEX relearn_volume ON relearn (volume_id, state);
