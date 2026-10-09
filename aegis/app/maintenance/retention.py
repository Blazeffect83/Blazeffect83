"""Retention and database maintenance. Knowledge (documents, claims) is never
deleted for age alone; only operational telemetry is pruned."""
from __future__ import annotations

from datetime import timedelta

from ..db import Database, now_iso


def apply_retention(db: Database, metrics_days: int, tool_output_days: int) -> dict:
    out = {}
    out["system_metrics"] = db.execute("DELETE FROM system_metrics WHERE created_at < ?",
                                       (now_iso(timedelta(days=-metrics_days)),)).rowcount
    # Keep tool-run metadata; drop large captured output after the retention window.
    out["tool_outputs_trimmed"] = db.execute(
        "UPDATE tool_runs SET output = NULL WHERE output IS NOT NULL AND started_at < ?",
        (now_iso(timedelta(days=-tool_output_days)),)).rowcount
    out["sessions"] = db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),)).rowcount
    out["login_attempts"] = db.execute("DELETE FROM login_attempts WHERE created_at < ?",
                                       (now_iso(timedelta(days=-7)),)).rowcount
    out["notifications"] = db.execute("DELETE FROM notifications WHERE read = 1 AND created_at < ?",
                                      (now_iso(timedelta(days=-90)),)).rowcount
    return out


def cleanup_orphans(db: Database) -> dict:
    out = {}
    out["document_topics"] = db.execute(
        "DELETE FROM document_topics WHERE document_id NOT IN (SELECT id FROM documents)").rowcount
    out["unused_topics"] = db.execute(
        "DELETE FROM topics WHERE id NOT IN (SELECT topic_id FROM document_topics)").rowcount
    out["dangling_relationships"] = db.execute(
        "DELETE FROM relationships WHERE (from_type = 'claim' AND from_id NOT IN (SELECT id FROM claims)) "
        "OR (to_type = 'claim' AND to_id NOT IN (SELECT id FROM claims)) "
        "OR (from_type = 'document' AND from_id NOT IN (SELECT id FROM documents)) "
        "OR (to_type = 'document' AND to_id NOT IN (SELECT id FROM documents))").rowcount
    out["claims_without_evidence"] = db.execute(
        "DELETE FROM claims WHERE origin = 'source' AND id NOT IN (SELECT claim_id FROM claim_evidence)").rowcount
    return out


def vacuum(db: Database) -> None:
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    for t in ("documents_fts", "claims_fts", "experiments_fts"):
        db.execute(f"INSERT INTO {t}({t}) VALUES ('optimize')")
