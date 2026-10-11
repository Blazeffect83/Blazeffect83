-- Self-improvement (v0.4): every change it makes to itself, the trust its rules have earned, phrasings it learned.
CREATE TABLE self_changes (
    id      INTEGER PRIMARY KEY,
    at      REAL NOT NULL,
    area    TEXT NOT NULL,              -- tuning | rules | specialists | reading | writing
    subject TEXT NOT NULL,              -- the knob, rule, agent, source or relation it concerns
    action  TEXT NOT NULL,              -- adopted | rejected | rolled_back | reset | demoted | promoted | corrected | spawned | retired | adjusted | learned
    summary TEXT NOT NULL,              -- one plain sentence, with the evidence
    old     TEXT,
    new     TEXT,
    before  REAL,                       -- the measured score before …
    after   REAL,                       -- … and after (or with the candidate)
    state   TEXT NOT NULL DEFAULT 'final',  -- watching (a tuning change still under the regression guard) | final
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX self_changes_at ON self_changes(at);

CREATE TABLE rule_trust (
    kind      TEXT NOT NULL,
    p         INTEGER NOT NULL,
    q         INTEGER NOT NULL DEFAULT 0,
    checked   INTEGER NOT NULL DEFAULT 0,  -- conclusions whose truth is known by now
    confirmed INTEGER NOT NULL DEFAULT 0,  -- … that a source later stated
    refuted   INTEGER NOT NULL DEFAULT 0,  -- … that a source later contradicted
    trust     REAL NOT NULL DEFAULT 0.5,   -- (confirmed + 1) / (checked + 2)
    state     TEXT NOT NULL DEFAULT 'active',  -- active | demoted
    updated   REAL NOT NULL,
    PRIMARY KEY (kind, p, q)
);

CREATE TABLE phrasings (
    predicate_id INTEGER PRIMARY KEY,
    middle       TEXT NOT NULL,         -- the words between subject and object, as read ("was born in")
    support      INTEGER NOT NULL,      -- sentences it was learned from
    confidence   REAL NOT NULL,
    learned_at   REAL NOT NULL,
    updated      REAL NOT NULL
);

-- Conclusions it withdrew: never derived again; refuted ones keep counting against their rule.
CREATE TABLE withdrawn (
    s      INTEGER NOT NULL,
    p      INTEGER NOT NULL,
    o      INTEGER NOT NULL,
    kind   TEXT NOT NULL,                -- the rule that drew it
    rule_p INTEGER NOT NULL,
    rule_q INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,                -- refuted (a source contradicted it) | demoted (its rule lost trust)
    at     REAL NOT NULL,
    PRIMARY KEY (s, p, o)
) WITHOUT ROWID;
CREATE INDEX withdrawn_rule ON withdrawn(kind, rule_p, rule_q, reason);
