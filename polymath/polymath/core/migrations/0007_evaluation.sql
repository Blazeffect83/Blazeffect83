-- Self-evaluation: quizzes over held-out facts, and nightly reports.
CREATE TABLE quizzes (
    id       INTEGER PRIMARY KEY,
    created  REAL NOT NULL,
    n        INTEGER NOT NULL,
    correct  INTEGER NOT NULL,
    accuracy REAL NOT NULL,
    chance   REAL NOT NULL,          -- expected accuracy of random guessing
    details  TEXT NOT NULL           -- JSON: accuracy by method / predicate
);

CREATE TABLE quiz_answers (
    id         INTEGER PRIMARY KEY,
    quiz_id    INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    triple_id  INTEGER NOT NULL,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,        -- JSON [{entity, label}]
    answer     INTEGER NOT NULL,     -- correct entity id
    chosen     INTEGER NOT NULL,
    correct    INTEGER NOT NULL,
    method     TEXT NOT NULL,        -- graph | text | related | none
    confidence REAL NOT NULL,
    topic_id   INTEGER
);
CREATE INDEX quiz_answers_topic ON quiz_answers (topic_id);

CREATE TABLE reports (
    id      INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    day     TEXT NOT NULL UNIQUE,
    path    TEXT NOT NULL,
    summary TEXT NOT NULL            -- JSON
);
