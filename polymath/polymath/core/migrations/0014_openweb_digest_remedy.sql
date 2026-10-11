-- Vetted open-web learning, the daily digest, and learning from mistakes.

-- Sites cited by Wikipedia articles: candidates for open-web learning (counted once per article read).
CREATE TABLE site_citations (
    site       TEXT PRIMARY KEY,
    citations  INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
) WITHOUT ROWID;
CREATE INDEX site_citations_rank ON site_citations (citations DESC);
CREATE TABLE site_urls (                     -- a few cited pages per site: where a visit starts
    site TEXT NOT NULL,
    url  TEXT NOT NULL,
    PRIMARY KEY (site, url)
) WITHOUT ROWID;

-- The vetting verdict for every site considered: approved | probation | refused | dropped | retry.
CREATE TABLE sites (
    site       TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    citations  INTEGER NOT NULL DEFAULT 0,
    trust      REAL,                           -- agreement of its facts with well-sourced ones (Beta mean)
    checked    INTEGER NOT NULL DEFAULT 0,     -- facts compared
    vetted_at  REAL NOT NULL,
    retry_at   REAL
) WITHOUT ROWID;

-- Offline safety lists (adult, malware, phishing, gambling, fake news): 64-bit hashes of domains.
CREATE TABLE blocked_domains (
    h    INTEGER PRIMARY KEY,                  -- blake2b-64 of the lower-case domain
    list INTEGER NOT NULL,                     -- index into kv 'blocklists'
    gen  INTEGER NOT NULL
) WITHOUT ROWID;

-- What the daily digest said.
CREATE TABLE digests (
    id      INTEGER PRIMARY KEY,
    day     TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    data    TEXT NOT NULL                      -- JSON
);

-- Wrong self-test answers it goes back to: read, re-test, up to three attempts.
CREATE TABLE remediation (
    id        INTEGER PRIMARY KEY,
    origin    TEXT NOT NULL,                   -- quiz | agent
    triple_id INTEGER NOT NULL UNIQUE,
    subject   INTEGER NOT NULL,
    predicate INTEGER NOT NULL,
    answer    INTEGER NOT NULL,
    wrong     INTEGER NOT NULL,
    options   TEXT NOT NULL,                   -- JSON [entity ids]
    question  TEXT NOT NULL,
    state     TEXT NOT NULL,                   -- pending | reading | fixed | still_wrong
    attempts  INTEGER NOT NULL DEFAULT 0,
    created   REAL NOT NULL,
    next_at   REAL NOT NULL,
    updated   REAL NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX remediation_due ON remediation (state, next_at);

-- Things worth telling the user that have no table of their own (vetting, safety lists, relearning).
CREATE TABLE events (
    id     INTEGER PRIMARY KEY,
    at     REAL NOT NULL,
    kind   TEXT NOT NULL,
    text   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
