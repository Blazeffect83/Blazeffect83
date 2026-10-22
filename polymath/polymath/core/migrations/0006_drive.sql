-- Drive: title → dump position index (targeted reading), topic priorities, effort log, bandit, decisions.
CREATE TABLE wiki_index (
    lang      TEXT NOT NULL,
    title     TEXT NOT NULL,
    dump_url  TEXT NOT NULL,
    stream    INTEGER NOT NULL,     -- byte offset of the bz2 stream holding the page
    next      INTEGER NOT NULL,     -- byte offset where that stream ends
    PRIMARY KEY (lang, title)
) WITHOUT ROWID;

CREATE TABLE topic_priority (
    topic_id   INTEGER PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
    gap        REAL NOT NULL,
    importance REAL NOT NULL,
    novelty    REAL NOT NULL,
    effort     REAL NOT NULL,
    priority   REAL NOT NULL,
    evidence   TEXT NOT NULL,       -- JSON: how each factor was computed
    updated    REAL NOT NULL
);
CREATE INDEX topic_priority_rank ON topic_priority (priority DESC);

CREATE TABLE topic_effort (
    topic_id    INTEGER NOT NULL,
    cpu_seconds REAL NOT NULL,
    at          REAL NOT NULL
);
CREATE INDEX topic_effort_topic ON topic_effort (topic_id, at);

CREATE TABLE bandit_arms (
    context TEXT NOT NULL,
    action  TEXT NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0,
    mean    REAL NOT NULL DEFAULT 0,
    m2      REAL NOT NULL DEFAULT 0,
    updated REAL NOT NULL,
    PRIMARY KEY (context, action)
) WITHOUT ROWID;

CREATE TABLE decisions (
    id       INTEGER PRIMARY KEY,
    at       REAL NOT NULL,
    cycle    INTEGER NOT NULL,
    context  TEXT NOT NULL,
    options  TEXT NOT NULL,         -- JSON {action: sampled score}
    chosen   TEXT NOT NULL,
    reason   TEXT NOT NULL,
    job_id   INTEGER,
    reward   REAL,
    cpu      REAL
);
CREATE INDEX decisions_at ON decisions (at);
