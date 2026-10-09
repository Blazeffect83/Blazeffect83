-- AEGIS initial schema. All timestamps are UTC ISO-8601 strings.

CREATE TABLE objectives (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'general',      -- general | research | coding | monitor
    priority        INTEGER NOT NULL DEFAULT 5,           -- 1 (highest) .. 9
    success_criteria TEXT NOT NULL DEFAULT '[]',          -- JSON list of strings / checks
    constraints     TEXT NOT NULL DEFAULT '[]',           -- JSON list
    allowed_tools   TEXT NOT NULL DEFAULT '[]',           -- JSON list; empty = all tier 0/1 tools
    deadline        TEXT,
    budget_tokens   INTEGER,
    budget_usd      REAL,
    time_budget_minutes INTEGER,
    status          TEXT NOT NULL DEFAULT 'QUEUED',
    status_reason   TEXT,
    plan            TEXT,                                 -- JSON plan (current)
    plan_version    INTEGER NOT NULL DEFAULT 0,
    replans         INTEGER NOT NULL DEFAULT 0,
    checkpoint      TEXT,                                 -- JSON resumable state
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    completion_summary TEXT,
    clarification_question TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX idx_objectives_status ON objectives(status, priority, created_at);

CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    objective_id    TEXT NOT NULL REFERENCES objectives(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    plan_version    INTEGER NOT NULL,
    title           TEXT NOT NULL,
    tool            TEXT NOT NULL,
    args            TEXT NOT NULL DEFAULT '{}',
    expected        TEXT,                                 -- expected outcome (free text)
    checks          TEXT NOT NULL DEFAULT '[]',           -- JSON deterministic checks
    depends_on      TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'READY',
    status_reason   TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    result          TEXT,
    verification    TEXT,
    approval_id     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_tasks_objective ON tasks(objective_id, plan_version, seq);
CREATE INDEX idx_tasks_status ON tasks(status);

CREATE TABLE task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id    TEXT NOT NULL REFERENCES objectives(id) ON DELETE CASCADE,
    task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    reason          TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_task_events_objective ON task_events(objective_id, id);

CREATE TABLE research_topics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    query           TEXT NOT NULL DEFAULT '',
    feeds           TEXT NOT NULL DEFAULT '[]',           -- JSON list of RSS/Atom URLs
    seed_urls       TEXT NOT NULL DEFAULT '[]',           -- JSON list of page URLs
    allowed_domains TEXT NOT NULL DEFAULT '[]',
    interval_minutes INTEGER NOT NULL DEFAULT 720,
    max_docs_per_run INTEGER NOT NULL DEFAULT 10,
    freshness_days  INTEGER NOT NULL DEFAULT 365,
    priority        INTEGER NOT NULL DEFAULT 5,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_run_at     TEXT,
    next_run_at     TEXT,
    last_status     TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE research_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    domain          TEXT NOT NULL,
    topic_id        INTEGER REFERENCES research_topics(id) ON DELETE SET NULL,
    discovery_method TEXT NOT NULL,                       -- feed | seed | search | objective | manual
    discovered_from TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',      -- pending | fetched | rejected | failed | duplicate
    status_reason   TEXT,
    http_status     INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    discovered_at   TEXT NOT NULL,
    fetched_at      TEXT
);
CREATE INDEX idx_sources_status ON research_sources(status);
CREATE INDEX idx_sources_domain ON research_sources(domain);

CREATE TABLE documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER REFERENCES research_sources(id) ON DELETE SET NULL,
    url             TEXT NOT NULL,
    final_url       TEXT NOT NULL,
    domain          TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    author          TEXT,
    published_at    TEXT,
    content_type    TEXT,
    content_hash    TEXT NOT NULL,
    simhash         TEXT NOT NULL,
    text            TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL DEFAULT '',
    quality_score   REAL NOT NULL DEFAULT 0,
    quality_breakdown TEXT NOT NULL DEFAULT '{}',
    injection_flags TEXT NOT NULL DEFAULT '[]',
    near_duplicate_of INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    extraction      TEXT NOT NULL DEFAULT '{}',           -- JSON: definitions, procedures, limitations, dates
    stale           INTEGER NOT NULL DEFAULT 0,
    retrieved_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_documents_hash ON documents(content_hash);
CREATE INDEX idx_documents_domain ON documents(domain);
CREATE INDEX idx_documents_quality ON documents(quality_score);

CREATE VIRTUAL TABLE documents_fts USING fts5(
    title, summary, text, content='documents', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, summary, text) VALUES (new.id, new.title, new.summary, new.text);
END;
CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary, text) VALUES ('delete', old.id, old.title, old.summary, old.text);
END;
CREATE TRIGGER documents_au AFTER UPDATE OF title, summary, text ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary, text) VALUES ('delete', old.id, old.title, old.summary, old.text);
    INSERT INTO documents_fts(rowid, title, summary, text) VALUES (new.id, new.title, new.summary, new.text);
END;

CREATE TABLE topics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE
);

CREATE TABLE document_topics (
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    topic_id        INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, topic_id)
);

CREATE TABLE claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT NOT NULL,
    normalized_hash TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'statement',    -- statement | definition | procedure | limitation | inference
    origin          TEXT NOT NULL DEFAULT 'source',       -- source (stated by a source) | agent (agent inference) | experiment
    status          TEXT NOT NULL DEFAULT 'active',       -- active | contested | outdated | retracted
    confidence      TEXT NOT NULL DEFAULT 'low',          -- low | medium | high (rubric, not a probability)
    corroboration   INTEGER NOT NULL DEFAULT 0,           -- independent supporting sources
    topic           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_claims_hash ON claims(normalized_hash);

CREATE VIRTUAL TABLE claims_fts USING fts5(text, content='claims', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER claims_au AFTER UPDATE OF text ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO claims_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE claim_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id        INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    document_id     INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    experiment_id   INTEGER REFERENCES experiments(id) ON DELETE CASCADE,
    stance          TEXT NOT NULL DEFAULT 'supports',     -- supports | contradicts
    passage         TEXT NOT NULL DEFAULT '',
    independent     INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE (claim_id, document_id, experiment_id)
);
CREATE INDEX idx_evidence_claim ON claim_evidence(claim_id);

CREATE TABLE claim_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id        INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE relationships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,                        -- contradicts | related | supersedes | derived_from
    from_type       TEXT NOT NULL,                        -- claim | document | experiment
    from_id         INTEGER NOT NULL,
    to_type         TEXT NOT NULL,
    to_id           INTEGER NOT NULL,
    note            TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE (kind, from_type, from_id, to_type, to_id)
);

CREATE TABLE experiments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id    TEXT REFERENCES objectives(id) ON DELETE SET NULL,
    task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    context         TEXT NOT NULL,                        -- what was being attempted
    procedure       TEXT NOT NULL,                        -- tool + args summary
    outcome         TEXT NOT NULL,                        -- success | failure
    lesson          TEXT NOT NULL DEFAULT '',
    error_class     TEXT,
    created_at      TEXT NOT NULL
);
CREATE VIRTUAL TABLE experiments_fts USING fts5(context, procedure, lesson, content='experiments', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER experiments_ai AFTER INSERT ON experiments BEGIN
    INSERT INTO experiments_fts(rowid, context, procedure, lesson) VALUES (new.id, new.context, new.procedure, new.lesson);
END;
CREATE TRIGGER experiments_ad AFTER DELETE ON experiments BEGIN
    INSERT INTO experiments_fts(experiments_fts, rowid, context, procedure, lesson) VALUES ('delete', old.id, old.context, old.procedure, old.lesson);
END;

CREATE TABLE tool_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    objective_id    TEXT REFERENCES objectives(id) ON DELETE SET NULL,
    task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    tool            TEXT NOT NULL,
    args            TEXT NOT NULL,
    status          TEXT NOT NULL,                        -- started | succeeded | failed | blocked
    exit_code       INTEGER,
    output          TEXT,
    error           TEXT,
    error_class     TEXT,
    duration_ms     INTEGER,
    resource_usage  TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX idx_tool_runs_objective ON tool_runs(objective_id);

CREATE TABLE approvals (
    id              TEXT PRIMARY KEY,
    objective_id    TEXT REFERENCES objectives(id) ON DELETE CASCADE,
    task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    tool            TEXT NOT NULL,
    args            TEXT NOT NULL,
    action_hash     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    affected        TEXT NOT NULL,
    consequences    TEXT NOT NULL,
    reversible      INTEGER NOT NULL,
    operation       TEXT NOT NULL,
    scope           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | rejected | expired | consumed
    decided_by      TEXT,
    decided_at      TEXT,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_approvals_status ON approvals(status);

CREATE TABLE model_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id    TEXT,
    purpose         TEXT NOT NULL,                        -- role: planning | extraction | ...
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    cost_is_estimate INTEGER NOT NULL DEFAULT 1,
    success         INTEGER NOT NULL,
    error_class     TEXT,
    latency_ms      INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_model_usage_created ON model_usage(created_at);
CREATE INDEX idx_model_usage_objective ON model_usage(objective_id);

CREATE TABLE system_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cpu_percent     REAL,
    load_1m         REAL,
    memory_percent  REAL,
    memory_available_mb REAL,
    disk_free_mb    REAL,
    disk_percent    REAL,
    cpu_temp_c      REAL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_metrics_created ON system_metrics(created_at);

-- Append-only, hash-chained audit log. UPDATE and DELETE are rejected by triggers.
CREATE TABLE audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    actor           TEXT NOT NULL,                        -- agent | user:<name> | system
    event           TEXT NOT NULL,
    objective_id    TEXT,
    task_id         TEXT,
    tool            TEXT,
    args            TEXT,
    decision        TEXT,
    approval_id     TEXT,
    result          TEXT,
    exit_code       INTEGER,
    duration_ms     INTEGER,
    error           TEXT,
    resources       TEXT,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL,                        -- info | warning | critical
    kind            TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    read            INTEGER NOT NULL DEFAULT 0,
    delivered       TEXT NOT NULL DEFAULT '[]',
    dedupe_key      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_notifications_created ON notifications(created_at);

CREATE TABLE sessions (
    id_hash         TEXT PRIMARY KEY,
    username        TEXT NOT NULL,
    csrf_token      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE TABLE login_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client          TEXT NOT NULL,
    success         INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE kv (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE backups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 0,
    verify_detail   TEXT,
    created_at      TEXT NOT NULL
);
