"""Storage back-ends. PgStore (asyncpg + TimescaleDB) is the production store;
MemoryStore implements the same contract for unit tests and the zero-dependency demo."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..core.events import utcnow
from .models import DDL, GENESIS_HASH, AuditRow, compute_audit_hash


@dataclass
class DeviceRow:
    device_id: str
    trust_score: float = 1.0
    status: str = "trusted"
    last_updated: datetime | None = None


@dataclass
class VerdictRow:
    event_id: str
    device_id: str
    trust_score: float
    verdict: str
    reason: str
    timestamp: datetime


@dataclass
class QuarantineRow:
    device_id: str
    quarantined_at: datetime
    reason: str
    released_at: datetime | None = None


class Store(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def ensure_device(self, device_id: str) -> DeviceRow: ...
    async def get_device(self, device_id: str) -> DeviceRow | None: ...
    async def update_device(self, device_id: str, trust_score: float, status: str) -> None: ...
    async def insert_verdict(self, v: VerdictRow) -> None: ...
    async def get_verdict(self, event_id: str) -> VerdictRow | None: ...
    async def count_verdict_ids(self, prefix: str) -> int: ...
    async def verdicts_since(self, device_id: str, since: datetime) -> list[VerdictRow]: ...
    async def upsert_quarantine(self, device_id: str, reason: str) -> None: ...
    async def release_quarantine(self, device_id: str) -> bool: ...
    async def is_quarantined(self, device_id: str) -> bool: ...
    async def append_audit(self, action: str, device_id: str | None, details: dict) -> AuditRow: ...
    async def audit_rows(self) -> list[AuditRow]: ...


class MemoryStore:
    """In-process store with identical semantics (append-only audit enforced by API)."""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceRow] = {}
        self.verdicts: dict[str, VerdictRow] = {}
        self.quarantine: dict[str, QuarantineRow] = {}
        self._audit: list[AuditRow] = []
        self._lock = asyncio.Lock()

    async def connect(self) -> None:  # noqa: D401
        return None

    async def close(self) -> None:
        return None

    async def ensure_device(self, device_id: str) -> DeviceRow:
        if device_id not in self.devices:
            self.devices[device_id] = DeviceRow(device_id, last_updated=utcnow())
        return self.devices[device_id]

    async def get_device(self, device_id: str) -> DeviceRow | None:
        return self.devices.get(device_id)

    async def update_device(self, device_id: str, trust_score: float, status: str) -> None:
        d = await self.ensure_device(device_id)
        d.trust_score, d.status, d.last_updated = trust_score, status, utcnow()

    async def insert_verdict(self, v: VerdictRow) -> None:
        if v.event_id in self.verdicts:
            raise ValueError(f"duplicate event_id {v.event_id}")
        if v.device_id not in self.devices:
            raise ValueError("FK violation: unknown device_id")
        self.verdicts[v.event_id] = v

    async def get_verdict(self, event_id: str) -> VerdictRow | None:
        return self.verdicts.get(event_id)

    async def count_verdict_ids(self, prefix: str) -> int:
        return sum(1 for k in self.verdicts if k.startswith(prefix))

    async def verdicts_since(self, device_id: str, since: datetime) -> list[VerdictRow]:
        return sorted((v for v in self.verdicts.values()
                       if v.device_id == device_id and v.timestamp >= since),
                      key=lambda v: v.timestamp)

    async def upsert_quarantine(self, device_id: str, reason: str) -> None:
        self.quarantine[device_id] = QuarantineRow(device_id, utcnow(), reason, None)

    async def release_quarantine(self, device_id: str) -> bool:
        q = self.quarantine.get(device_id)
        if q is None or q.released_at is not None:
            return False
        q.released_at = utcnow()
        return True

    async def is_quarantined(self, device_id: str) -> bool:
        q = self.quarantine.get(device_id)
        return q is not None and q.released_at is None

    async def append_audit(self, action: str, device_id: str | None, details: dict) -> AuditRow:
        async with self._lock:
            prev = self._audit[-1].this_hash if self._audit else GENESIS_HASH
            row = AuditRow(len(self._audit) + 1, utcnow(), action, device_id,
                           json.loads(json.dumps(details, default=str)), prev)
            row.this_hash = compute_audit_hash(prev, row)
            self._audit.append(row)
            return row

    async def audit_rows(self) -> list[AuditRow]:
        # return copies: callers cannot mutate history through the API
        return [AuditRow(**r.__dict__) for r in self._audit]


class PgStore:
    """PostgreSQL + TimescaleDB store (asyncpg)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: Any = None

    async def connect(self) -> None:
        import asyncpg

        for attempt in range(30):  # wait for the DB container
            try:
                self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
                break
            except (OSError, asyncpg.PostgresError):
                if attempt == 29:
                    raise
                await asyncio.sleep(1)
        async with self.pool.acquire() as c:
            await c.execute("SELECT pg_advisory_lock(424242)")
            try:
                await c.execute(DDL)
            finally:
                await c.execute("SELECT pg_advisory_unlock(424242)")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def ensure_device(self, device_id: str) -> DeviceRow:
        async with self.pool.acquire() as c:
            await c.execute("INSERT INTO device_trust_scores(device_id, last_updated) VALUES($1, now()) "
                            "ON CONFLICT (device_id) DO NOTHING", device_id)
            r = await c.fetchrow("SELECT * FROM device_trust_scores WHERE device_id=$1", device_id)
        return DeviceRow(**dict(r))

    async def get_device(self, device_id: str) -> DeviceRow | None:
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM device_trust_scores WHERE device_id=$1", device_id)
        return DeviceRow(**dict(r)) if r else None

    async def update_device(self, device_id: str, trust_score: float, status: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO device_trust_scores(device_id, trust_score, status, last_updated) "
                "VALUES($1,$2,$3,now()) ON CONFLICT (device_id) DO UPDATE SET "
                "trust_score=EXCLUDED.trust_score, status=EXCLUDED.status, last_updated=now()",
                device_id, trust_score, status)

    async def insert_verdict(self, v: VerdictRow) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO trust_verdicts(event_id, device_id, trust_score, verdict, reason, timestamp) "
                "VALUES($1,$2,$3,$4,$5,$6)", v.event_id, v.device_id, v.trust_score, v.verdict,
                v.reason, v.timestamp)

    async def get_verdict(self, event_id: str) -> VerdictRow | None:
        async with self.pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM trust_verdicts WHERE event_id=$1 "
                                 "ORDER BY timestamp ASC LIMIT 1", event_id)
        return VerdictRow(**dict(r)) if r else None

    async def count_verdict_ids(self, prefix: str) -> int:
        async with self.pool.acquire() as c:
            return int(await c.fetchval(
                "SELECT count(*) FROM trust_verdicts WHERE starts_with(event_id, $1)", prefix))

    async def verdicts_since(self, device_id: str, since: datetime) -> list[VerdictRow]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM trust_verdicts WHERE device_id=$1 AND timestamp >= $2 "
                                 "ORDER BY timestamp", device_id, since)
        return [VerdictRow(**dict(r)) for r in rows]

    async def upsert_quarantine(self, device_id: str, reason: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO quarantine_list(device_id, quarantined_at, reason, released_at) "
                "VALUES($1, now(), $2, NULL) ON CONFLICT (device_id) DO UPDATE SET "
                "quarantined_at=now(), reason=EXCLUDED.reason, released_at=NULL", device_id, reason)

    async def release_quarantine(self, device_id: str) -> bool:
        async with self.pool.acquire() as c:
            res = await c.execute("UPDATE quarantine_list SET released_at=now() "
                                  "WHERE device_id=$1 AND released_at IS NULL", device_id)
        return res.endswith(" 1")

    async def is_quarantined(self, device_id: str) -> bool:
        async with self.pool.acquire() as c:
            return bool(await c.fetchval("SELECT EXISTS(SELECT 1 FROM quarantine_list "
                                         "WHERE device_id=$1 AND released_at IS NULL)", device_id))

    async def append_audit(self, action: str, device_id: str | None, details: dict) -> AuditRow:
        details = json.loads(json.dumps(details, default=str))
        async with self.pool.acquire() as c:
            async with c.transaction():
                # serialise writers so the chain is strictly linear
                await c.execute("SELECT pg_advisory_xact_lock(4242)")
                prev = await c.fetchval("SELECT this_hash FROM audit_log ORDER BY log_id DESC LIMIT 1")
                prev = prev or GENESIS_HASH
                log_id = await c.fetchval("SELECT nextval(pg_get_serial_sequence('audit_log','log_id'))")
                ts = await c.fetchval("SELECT date_trunc('milliseconds', now())")
                row = AuditRow(int(log_id), ts, action, device_id, details, prev)
                row.this_hash = compute_audit_hash(prev, row)
                await c.execute(
                    "INSERT INTO audit_log(log_id, timestamp, action, device_id, details, prev_hash, this_hash) "
                    "VALUES($1,$2,$3,$4,$5::jsonb,$6,$7)", row.log_id, row.timestamp, action, device_id,
                    json.dumps(details), prev, row.this_hash)
        return row

    async def audit_rows(self) -> list[AuditRow]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM audit_log ORDER BY log_id")
        return [AuditRow(r["log_id"], r["timestamp"], r["action"], r["device_id"],
                         json.loads(r["details"]) if isinstance(r["details"], str) else r["details"],
                         r["prev_hash"], r["this_hash"]) for r in rows]


def make_store(database_url: str) -> Store:
    return PgStore(database_url) if database_url else MemoryStore()
