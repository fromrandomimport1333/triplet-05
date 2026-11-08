"""TimescaleDB schema (exact Phase 1 tables) + hash-chain helpers for audit_log."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.events import canonical_json, iso

GENESIS_HASH = "0" * 64
DEVICE_STATUSES = ("trusted", "suspicious", "quarantined")
VERDICTS = ("trusted", "suspicious", "rejected")

# NOTE on trust_verdicts PK: TimescaleDB requires every unique index on a hypertable to
# include the partitioning column, so the physical PK is (event_id, timestamp).
# Uniqueness of event_id is still guaranteed by Guardian: replays are stored under
# '<event_id>#replay-<n>' (see detection pipeline) and never reuse an event_id.
DDL = r"""
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS device_trust_scores (
    device_id    TEXT PRIMARY KEY,
    trust_score  REAL NOT NULL DEFAULT 1.0,
    status       TEXT NOT NULL DEFAULT 'trusted'
                 CHECK (status IN ('trusted','suspicious','quarantined')),
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trust_verdicts (
    event_id    TEXT NOT NULL,
    device_id   TEXT NOT NULL REFERENCES device_trust_scores(device_id),
    trust_score REAL NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('trusted','suspicious','rejected')),
    reason      TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, timestamp)
);
SELECT create_hypertable('trust_verdicts', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS trust_verdicts_event_idx ON trust_verdicts (event_id);
CREATE INDEX IF NOT EXISTS trust_verdicts_device_idx ON trust_verdicts (device_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS quarantine_list (
    device_id      TEXT PRIMARY KEY,
    quarantined_at TIMESTAMPTZ NOT NULL,
    reason         TEXT NOT NULL,
    released_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id    SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    action    TEXT NOT NULL,
    device_id TEXT,
    details   JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash TEXT NOT NULL,
    this_hash TEXT NOT NULL UNIQUE
);

-- append-only enforcement
CREATE OR REPLACE FUNCTION guardian_audit_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_log_no_mutation ON audit_log;
CREATE TRIGGER audit_log_no_mutation BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION guardian_audit_append_only();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION guardian_audit_append_only();
"""


@dataclass
class AuditRow:
    log_id: int
    timestamp: datetime
    action: str
    device_id: str | None
    details: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    this_hash: str = ""

    def hashed_content(self) -> dict[str, Any]:
        """The canonical row content covered by the hash (everything except this_hash)."""
        return {"log_id": self.log_id, "timestamp": iso(self.timestamp), "action": self.action,
                "device_id": self.device_id, "details": self.details, "prev_hash": self.prev_hash}


def compute_audit_hash(prev_hash: str, row: AuditRow) -> str:
    """this_hash = sha256(prev_hash + canonical_json(this_row))."""
    return hashlib.sha256((prev_hash + canonical_json(row.hashed_content())).encode()).hexdigest()


def verify_chain(rows: list[AuditRow]) -> tuple[bool, int | None]:
    """Recompute the whole chain. Returns (ok, first_broken_log_id)."""
    prev = GENESIS_HASH
    # Deleting a row is detected because the next row's prev_hash no longer links.
    # (log_id gaps alone are NOT treated as tampering: SERIAL can skip on rollback.)
    for r in sorted(rows, key=lambda r: r.log_id):
        if r.prev_hash != prev or compute_audit_hash(prev, r) != r.this_hash:
            return False, r.log_id
        prev = r.this_hash
    return True, None
