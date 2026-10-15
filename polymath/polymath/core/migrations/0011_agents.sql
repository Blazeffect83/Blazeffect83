-- The agent society: user-directed (and evolved) agents, their scopes, tasks, rewards and learned preferences.
CREATE TABLE agents (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,          -- research | watch | answer | verify | predict
    directive     TEXT NOT NULL,          -- what the user said
    subject       TEXT NOT NULL,          -- the topic / keywords / relation the directive is about
    scope         TEXT NOT NULL,          -- JSON: resolved entities, topics, predicates, keywords
    params        TEXT NOT NULL,          -- JSON: learnable / heritable parameters (min_conf, batch, weights…)
    parent        INTEGER REFERENCES agents(id),
    generation    INTEGER NOT NULL DEFAULT 0,
    origin        TEXT NOT NULL DEFAULT 'user',   -- user | evolved
    status        TEXT NOT NULL DEFAULT 'active', -- active | paused | retired
    status_reason TEXT,
    xp            REAL NOT NULL DEFAULT 0,  -- sum of positive rewards (never decreases)
    level         INTEGER NOT NULL DEFAULT 1,
    reward_total  REAL NOT NULL DEFAULT 0,  -- net reward (can go down)
    cpu_total     REAL NOT NULL DEFAULT 0,
    steps         INTEGER NOT NULL DEFAULT 0,
    tasks_done    INTEGER NOT NULL DEFAULT 0,
    tasks_correct INTEGER NOT NULL DEFAULT 0,
    tasks_wrong   INTEGER NOT NULL DEFAULT 0,
    scope_at      REAL NOT NULL DEFAULT 0,
    created       REAL NOT NULL,
    updated       REAL NOT NULL
);

CREATE TABLE agent_scope (
    agent_id  INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL,
    weight    REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (agent_id, entity_id)
) WITHOUT ROWID;
CREATE INDEX agent_scope_entity ON agent_scope (entity_id);

CREATE TABLE agent_tasks (
    id           INTEGER PRIMARY KEY,
    agent_id     INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,         -- quiz | predict | read | digest | dispute | answer | calibrate | user
    action       TEXT NOT NULL,         -- the repertoire action that produced it (credited with the reward)
    context      TEXT NOT NULL DEFAULT '',
    target       TEXT NOT NULL DEFAULT '',  -- dedup key: an agent is rewarded once per target
    payload      TEXT NOT NULL DEFAULT '{}',
    result       TEXT NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL,         -- pending | done | correct | wrong | expired
    reward       REAL NOT NULL DEFAULT 0,
    cpu          REAL NOT NULL DEFAULT 0,
    created      REAL NOT NULL,
    done_at      REAL,
    verify_after REAL,
    verified_at  REAL,
    reason       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX agent_tasks_agent ON agent_tasks (agent_id, created);
CREATE INDEX agent_tasks_verify ON agent_tasks (state, verify_after);
CREATE UNIQUE INDEX agent_tasks_target ON agent_tasks (agent_id, kind, target) WHERE target != '';

CREATE TABLE agent_rewards (
    id       INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    task_id  INTEGER,
    amount   REAL NOT NULL,
    reason   TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE INDEX agent_rewards_agent ON agent_rewards (agent_id, at);

CREATE TABLE agent_arms (
    agent_id INTEGER NOT NULL,
    context  TEXT NOT NULL,
    action   TEXT NOT NULL,
    n        INTEGER NOT NULL DEFAULT 0,
    mean     REAL NOT NULL DEFAULT 0,
    m2       REAL NOT NULL DEFAULT 0,
    updated  REAL NOT NULL,
    PRIMARY KEY (agent_id, context, action)
) WITHOUT ROWID;
