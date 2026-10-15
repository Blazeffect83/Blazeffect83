-- "Is a job of this kind ready?" as one index seek per kind, whatever the queue size.
CREATE INDEX jobs_kind_ready ON jobs (kind, state, not_before);
