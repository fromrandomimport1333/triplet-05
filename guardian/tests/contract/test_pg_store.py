"""PgStore against a real TimescaleDB (CI service container). Skipped when
GUARDIAN_TEST_DATABASE_URL is not set."""
import asyncio
import os
import uuid

import pytest

from src.core.events import utcnow
from src.db.models import verify_chain
from src.db.session import PgStore, VerdictRow

DSN = os.environ.get("GUARDIAN_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="GUARDIAN_TEST_DATABASE_URL not set")


def test_pg_schema_chain_and_append_only():
    async def go():
        import asyncpg
        s = PgStore(DSN)
        await s.connect()
        await s.connect()  # DDL is idempotent
        dev = f"pg-{uuid.uuid4().hex[:6]}"
        await s.ensure_device(dev)
        eid = f"e-{uuid.uuid4().hex}"
        await s.insert_verdict(VerdictRow(eid, dev, 0.9, "trusted", "ok", utcnow()))
        assert (await s.get_verdict(eid)).verdict == "trusted"
        for i in range(5):
            await s.append_audit("test", dev, {"i": i, "nested": {"b": 1, "a": [1.5, "x"]}})
        rows = await s.audit_rows()
        assert verify_chain(rows) == (True, None)
        async with s.pool.acquire() as c:
            ht = await c.fetchval("SELECT count(*) FROM timescaledb_information.hypertables "
                                  "WHERE hypertable_name='trust_verdicts'")
            assert ht == 1
            for sql in ("UPDATE audit_log SET action='x' WHERE log_id=$1",
                        "DELETE FROM audit_log WHERE log_id=$1"):
                with pytest.raises(asyncpg.RaiseError):
                    await c.execute(sql, rows[-1].log_id)
            with pytest.raises(asyncpg.RaiseError):
                await c.execute("TRUNCATE audit_log")
        await s.upsert_quarantine(dev, "t")
        assert await s.is_quarantined(dev)
        assert await s.release_quarantine(dev) and not await s.is_quarantined(dev)
        await s.close()
    asyncio.run(go())
