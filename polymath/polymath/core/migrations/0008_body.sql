-- Body: vital signs history (dashboard charts, thermal/disk decisions) and backup log.
CREATE TABLE vitals (
    at           REAL PRIMARY KEY,
    temp_c       REAL,
    load1        REAL,
    mem_mb       REAL,
    disk_free_gb REAL,
    data_gb      REAL,
    players      INTEGER,
    mode         TEXT NOT NULL
);

CREATE TABLE backups (
    id      INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    path    TEXT NOT NULL,
    bytes   INTEGER NOT NULL,
    ok      INTEGER NOT NULL,
    detail  TEXT NOT NULL
);
