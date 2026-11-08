"""Each detection pipeline step (1-6) tested independently."""
import math
from datetime import datetime, timedelta, timezone

from src.core.events import VerdictSigner, check_schema, sign_event, verify_event_signature
from src.detection.anomaly import EwmaAnomalyDetector, map_score_to_verdict
from src.detection.plausibility import PlausibilityChecker
from src.detection.replay_guard import ReplayGuard
from src.db.models import verify_chain
from tests.conftest import warm

KEY = "k" * 32
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def evt(**kw):
    e = {"schema_version": "1.0", "event_id": "e1", "seq": 1, "device_id": "wl-1",
         "sensor_type": "water_level", "value": 1.2, "unit": "m", "ts": "2026-10-01T12:00:00Z",
         "signature": ""}
    e.update(kw)
    return sign_event(e, KEY)


# ---- step 1 ----
def test_step1_valid_event_passes():
    e = evt()
    assert check_schema(e) == (True, "ok") and verify_event_signature(e, KEY)


def test_step1_missing_field_and_bad_types():
    e = evt(); del e["seq"]
    assert not check_schema(e)[0]
    assert not check_schema(evt(value="1.2"))[0]
    assert not check_schema(evt(seq=True))[0]
    assert not check_schema(evt(ts="yesterday"))[0]
    assert not check_schema(evt(ts="2026-10-01T12:00:00"))[0]  # naive ts
    assert not check_schema(evt(value=math.inf))[0]
    assert not check_schema("not a dict")[0]


def test_step1_schema_versioning_policy():
    assert check_schema(evt(schema_version="1.7"))[0]          # additive minor accepted
    ok, why = check_schema(evt(schema_version="2.0"))
    assert not ok and "major" in why


def test_step1_signature_wrong_key_or_modified():
    e = evt()
    assert not verify_event_signature(e, "other-key")
    e2 = dict(e, value=9.9)
    assert not verify_event_signature(e2, KEY)
    assert not verify_event_signature(e, "")


def test_step1_pipeline_short_circuits(stack):
    v = stack.process_raw({"garbage": True})
    assert v["verdict"] == "rejected" and v["failed_step"] == 1
    v = stack.process_raw(b"\x00\xff not json")
    assert v["verdict"] == "rejected" and v["event_id"].startswith("invalid:")


# ---- step 2 ----
def test_step2_duplicate_event_id():
    g = ReplayGuard()
    assert g.check_and_record("a", "d", 1, 0)[0]
    ok, why = g.check_and_record("a", "d", 2, 1)
    assert not ok and "duplicate" in why


def test_step2_non_monotonic_seq_even_after_eviction():
    g = ReplayGuard(max_entries=2, window_s=10)
    for i in range(1, 6):
        assert g.check_and_record(f"id{i}", "d", i, i)[0]
    assert "id1" not in g._seen                               # evicted from window
    ok, why = g.check_and_record("id1", "d", 1, 100)          # still caught by seq
    assert not ok and "non-monotonic" in why


def test_step2_window_is_bounded():
    g = ReplayGuard(max_entries=100, window_s=5)
    for i in range(1000):
        g.check_and_record(f"x{i}", f"d{i}", 1, i)
    assert len(g) <= 100


# ---- step 3 ----
def test_step3_range_unit_rate_future():
    p = PlausibilityChecker()
    assert p.check("d", "water_level", "m", 1.2, T0)[0]
    assert not p.check("d", "water_level", "m", 12.0, T0)[0]          # range
    assert not p.check("d", "water_level", "cm", 1.2, T0)[0]          # unit
    p.accept("d", 1.2, T0)
    assert p.check("d", "water_level", "m", 1.25, T0 + timedelta(seconds=10))[0]
    ok, why = p.check("d", "water_level", "m", 8.5, T0 + timedelta(seconds=5))
    assert not ok and "jumped" in why                                  # rate
    assert not p.check("d", "water_level", "m", 1.2, T0 + timedelta(hours=1), now=T0)[0]  # future


# ---- step 4 ----
def test_step4_ewma_zscore_scores():
    a = EwmaAnomalyDetector(warmup=5, stuck_window_s=300)
    t = T0
    for i in range(30):
        t += timedelta(seconds=10)
        a.update("d", 1.2 + (0.01 if i % 2 else -0.01), t)
    assert a.score("d", 1.2, t)[0] == 1.0
    assert a.score("d", 1.9, t)[0] < 0.4
    s, why = a.score("d", 1.2, t)
    assert "z=" in why


def test_step4_stuck_sensor_capped_suspicious():
    a = EwmaAnomalyDetector(stuck_window_s=300)
    t = T0
    for _ in range(31):
        a.update("d", 1.2, t)
        t += timedelta(seconds=10)
    s, why = a.score("d", 1.2, t)
    assert map_score_to_verdict(s) == "suspicious" and "stuck_sensor" in why


# ---- step 5 ----
def test_step5_mapping_boundaries():
    assert map_score_to_verdict(1.0) == "trusted"
    assert map_score_to_verdict(0.8) == "trusted"
    assert map_score_to_verdict(0.79) == "suspicious"
    assert map_score_to_verdict(0.4) == "suspicious"
    assert map_score_to_verdict(0.39) == "rejected"


def test_step5_suspicious_never_quarantines(stack):
    for _ in range(80):
        stack.twin.stuck("wl-s")
    assert stack.verdicts[-1]["verdict"] == "suspicious"
    assert stack.rpc("brain").call("get_device_status", {"device_id": "wl-s"})["status"] == "suspicious"
    assert "wl-s" not in stack.twin.quarantined


# ---- step 6 ----
def test_step6_signed_published_persisted_audited(stack):
    warm(stack)
    v = stack.verdicts[-1]
    assert VerdictSigner.verify(v, stack.signer.public_key_pem())
    assert any(t == "guardian/verdicts/wl-1" and p["event_id"] == v["event_id"]
               for t, p in stack.bus.history)
    assert stack.run(stack.store.get_verdict, v["event_id"]).verdict == v["verdict"]
    rows = stack.run(stack.store.audit_rows)
    assert any(r.details.get("event_id") == v["event_id"] for r in rows)
    assert verify_chain(rows) == (True, None)
