"""B1 (actuator/unit consistency) and B2 (guardian/control)."""
from src.adapter.envelope import seal, seal_multi


def _q(s, dev):
    return [m["body"] for m in s.quarantines() if m["body"]["device_id"] == dev]


# ---------------------------------------------------------------- acknowledgements
def test_matching_ack_and_brain_retry_are_not_anomalies(bstack):
    s = bstack
    t, p, cid = s.brain.command("valve-01", "open")
    assert s.send(t, p) == "command_new"
    s.advance(4)
    assert s.send(t, p) == "command_retry"               # Brain retries with the SAME id
    assert s.send(*s.twin.ack("valve-01", cid)) == "ack_ok"
    assert s.send(*s.twin.ack("valve-01", cid)) == "ack_ok"   # repeated ack for a repeated id
    s.advance(30)
    assert s.adapter.inconsistencies == {} and s.quarantines() == []


def test_unknown_correlation_and_wrong_target_count(bstack):
    s = bstack
    s.advance(20)                                         # past the startup grace
    assert s.send(*s.twin.ack("valve-01", "brain-999999")) == "ack_inconsistent"
    assert _q(s, "valve-01") == []                        # threshold 2: one is not enough
    t, p, cid = s.brain.command("signal-01", "priority")
    s.send(t, p)
    assert s.send(*s.twin.ack("valve-01", cid)) == "ack_inconsistent"   # ack for another target
    q = _q(s, "valve-01")
    assert q and q[-1]["state"] == "quarantined" and q[-1]["reason"] == "inconsistent_ack"
    assert s.run(s.store.is_quarantined, "valve-01")


def test_command_without_ack_times_out(bstack):
    s = bstack
    for i in range(2):
        t, p, _ = s.brain.command("unit-rescue-01", "dispatch")
        s.send(t, p)
        s.advance(s.settings.ack_timeout_s + s.settings.ack_grace_s + 1)
    q = _q(s, "unit-rescue-01")
    assert q and q[-1]["reason"] == "inconsistent_ack"
    acts = s.audit_actions()
    assert acts.count("ack_inconsistency") == 2 and "device_quarantined" in acts


def test_late_ack_inside_grace_is_fine(bstack):
    s = bstack
    t, p, cid = s.brain.command("valve-01", "open")
    s.send(t, p)
    s.advance(s.settings.ack_timeout_s + 1)              # after 4 s, but inside the grace
    assert s.send(*s.twin.ack("valve-01", cid)) == "ack_ok"
    s.advance(30)
    assert s.adapter.inconsistencies == {}


def test_unit_ack_and_state_changes(bstack):
    s = bstack
    t, p, cid = s.brain.command("unit-rescue-01", "dispatch")
    s.send(t, p)
    assert s.send(*s.twin.ack("unit-rescue-01", cid, kind="unit")) == "ack_ok"
    b = {"id": "tw-x-1", "timestamp": "2026-11-14T10:00:00.000Z", "source": "twin",
         "unit_id": "unit-rescue-01", "status": "on_scene", "zone_id": "zone-01"}
    assert s.send("twin/unit/unit-rescue-01", seal_multi(b, {"guardian": s.k_tg})) == "state_change"


def test_startup_grace_and_retained_acks_ignored(bstack):
    s = bstack
    assert s.send(*s.twin.ack("valve-01", "brain-000001")) == "ack_ok"   # t < grace
    t, p = s.twin.ack("valve-01", "brain-000002")
    s.advance(20)
    assert s.send(t, p, retained=True) == "retained_ignored"
    assert s.adapter.inconsistencies == {}


def test_forged_command_and_ack_are_not_trusted(bstack):
    s = bstack
    t, p, cid = s.brain.command("valve-01", "open")
    forged = p.replace(s.k_gb, "x")
    b = {"id": "brain-x", "timestamp": "2026-11-14T10:00:00.000Z", "source": "brain",
         "correlation_id": "c", "target_id": "valve-01", "action": "open"}
    assert s.send("brain/cmd/valve-01", seal_multi(b, {"guardian": "wrong"})) == "command_unverified"
    assert s.send("brain/cmd/valve-01", seal(b, s.k_gb)) == "command_unverified"  # single tag: for the Twin
    t2, p2 = s.twin.ack("valve-01", "brain-x")
    assert s.send(t2, p2.replace('"guardian": "', '"guardian": "00')) == "ack_unverified"
    assert forged is not None and "command_unverified" in s.audit_actions()


def test_brain_alert(bstack):
    s = bstack
    assert s.send(*s.brain.alert("valve-99")) == "alert_ignored"          # not in twin/meta
    assert "alert_unknown_target" in s.audit_actions()
    assert s.send(*s.brain.alert("valve-01")) == "alert_ok"
    assert s.adapter.inconsistencies["valve-01"]
    assert s.send(*s.brain.alert("valve-01")) == "alert_ok"
    assert _q(s, "valve-01")[-1]["reason"] == "inconsistent_ack"


def test_alert_counting_can_be_disabled(make_bstack):
    s = make_bstack(alert_counts_as_inconsistency=False)
    for _ in range(3):
        s.send(*s.brain.alert("valve-01"))
    assert s.quarantines() == []


# ---------------------------------------------------------------- guardian/control
def test_control_reset_releases_and_clears(bstack):
    s = bstack
    for _ in range(15):
        s.tick("calm", falsify=(10, 40))
    assert _q(s, "water-01")[-1]["state"] == "quarantined"
    assert s.send(*s.brain.control({"op": "reset"})) == "control_reset"
    rel = _q(s, "water-01")[-1]
    assert rel["state"] == "released" and rel["reason"] == "reset"
    assert not s.run(s.store.is_quarantined, "water-01")
    assert s.adapter.state.pipeline.replay._last_seq == {}
    # after reset the baseline is gone: the real level is judged afresh
    assert set(s.tick("calm")) == {"verdict_trusted"}


def test_control_must_be_sealed_fresh_and_unique(bstack):
    s = bstack
    assert s.send(*s.brain.control({"op": "reset"}, key="wrong")) == "control_rejected"
    t, p = s.brain.control({"op": "reset"})
    assert s.send(t, p) == "control_reset"
    assert s.send(t, p) == "control_replay"
    s.advance(70)                                         # id forgotten after 60 s ...
    assert s.send(t, p) == "control_stale"               # ... but the timestamp is now too old
    assert s.send(*s.brain.control({"op": "mode", "modes": ["x"]})) == "control_unsupported"
    assert s.send(*s.brain.control({"op": "reset"}), retained=True) == "retained_ignored"
