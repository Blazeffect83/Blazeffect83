-- v0.12.0: every backup is restored once to prove it can be (body.verify_backup), and an hourly mirror
ALTER TABLE backups ADD COLUMN counts TEXT;          -- JSON: rows in the main tables and the database size, as taken
ALTER TABLE backups ADD COLUMN verified REAL;        -- when its test restore ran (NULL: not yet)
ALTER TABLE backups ADD COLUMN verify_ok INTEGER;    -- 1 restored and checked, 0 broken, NULL not tested (no room, gone)
ALTER TABLE backups ADD COLUMN verify_detail TEXT;
