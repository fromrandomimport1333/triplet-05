"""Proves audit_log is hash-chained: tampering with ANY past row breaks verification."""
import copy
from datetime import timedelta

import pytest

from src.db.models import GENESIS_HASH, compute_audit_hash, verify_chain
from src.db.session import MemoryStore
from tests.conftest import warm


@pytest.fixture
def rows(stack):
    warm(stack)
    for _ in range(3):
        stack.twin.spoofed("wl-1")
    r = stack.run(stack.store.audit_rows)
    assert len(r) > 10 and verify_chain(r) == (True, None)
    return r


def test_chain_formula(rows):
    assert rows[0].prev_hash == GENESIS_HASH
    for prev, cur in zip(rows, rows[1:]):
        assert cur.prev_hash == prev.this_hash
        assert cur.this_hash == compute_audit_hash(prev.this_hash, cur)


@pytest.mark.parametrize("field", ["action", "device_id", "details", "timestamp", "prev_hash", "this_hash"])
def test_tampering_any_field_of_any_row_breaks_chain(rows, field):
    for i in range(len(rows) - 1):  # every past row
        t = copy.deepcopy(rows)
        r = t[i]
        if field == "details":
            r.details = {**r.details, "reason": "nothing to see"}
        elif field == "timestamp":
            r.timestamp = r.timestamp + timedelta(seconds=1)
        elif field in ("prev_hash", "this_hash"):
            setattr(r, field, "f" * 64)
        else:
            setattr(r, field, "forged")
        ok, broken = verify_chain(t)
        assert not ok and broken is not None and broken <= t[i + 1].log_id


def test_deleting_a_row_breaks_chain(rows):
    t = rows[:3] + rows[4:]
    assert verify_chain(t)[0] is False


def test_recomputing_one_hash_still_detected(rows):
    """Attacker rewrites row 2 AND recomputes its hash: row 3's prev_hash no longer links."""
    t = copy.deepcopy(rows)
    t[2].action = "forged"
    t[2].this_hash = compute_audit_hash(t[2].prev_hash, t[2])
    assert verify_chain(t) == (False, t[3].log_id)


def test_store_api_is_append_only():
    import asyncio
    s = MemoryStore()
    asyncio.run(s.append_audit("a", None, {}))
    got = asyncio.run(s.audit_rows())
    got[0].action = "mutated"
    assert asyncio.run(s.audit_rows())[0].action == "a"
    assert not hasattr(s, "update_audit") and not hasattr(s, "delete_audit")
