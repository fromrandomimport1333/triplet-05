"""Adversarial: spoofed data, replay, MITM (tampered event, forged verdict, forged control)."""
import copy

from src.core.events import VerdictSigner, sign_event
from tests.conftest import warm


def test_spoof_wrong_key_rejected(stack):
    warm(stack)
    e = stack.twin.make_event("wl-1", 1.2)
    forged = sign_event(dict(e), "attacker-key")
    stack.twin.emit(forged)
    assert stack.verdicts[-1]["verdict"] == "rejected" and "signature" in stack.verdicts[-1]["reason"]


def test_spoof_cannot_get_victim_quarantined(stack):
    warm(stack)
    for _ in range(10):
        stack.twin.tampered("wl-1")
    assert "wl-1" not in stack.twin.quarantined
    assert stack.verdicts[-1]["device_id"] == "_unattributed"


def test_compromised_sensor_physically_impossible(stack):
    warm(stack)
    stack.twin.spoofed("wl-1", value=8.5)
    assert stack.verdicts[-1]["verdict"] == "rejected" and stack.verdicts[-1]["failed_step"] == 3


def test_slow_drift_attack_detected_by_anomaly(stack):
    for _ in range(30):
        stack.twin.normal("wl-d", noise=0.002)
    verdicts = []
    v = 1.2
    for _ in range(20):
        v += 0.4  # within physical rate (0.4m/10s) but statistically abnormal
        stack.twin.tick(10)
        stack.twin.emit(stack.twin.make_event("wl-d", round(v, 3)))
        verdicts.append(stack.verdicts[-1]["verdict"])
    assert "rejected" in verdicts or "suspicious" in verdicts


def test_replay_exact_copy(stack):
    warm(stack)
    e = stack.twin.normal("wl-1")
    first = stack.verdicts[-1]
    stack.twin.replay(e)
    second = stack.verdicts[-1]
    assert first["verdict"] == "trusted" and second["verdict"] == "rejected"
    assert second["failed_step"] == 2 and second["verdict_id"].startswith(e["event_id"] + "#replay-")
    # the ORIGINAL verdict is not overwritten by the replay
    assert stack.rpc("brain").call("get_trust_verdict", {"event_id": e["event_id"]})["verdict"] == "trusted"


def test_replay_with_new_event_id_but_old_seq(stack):
    warm(stack)
    e = stack.twin.normal("wl-1")
    r = dict(e, event_id="fresh-id")
    stack.twin.emit(sign_event(r, stack.hmac_key))  # attacker with key but stale seq
    assert stack.verdicts[-1]["verdict"] == "rejected" and "non-monotonic" in stack.verdicts[-1]["reason"]


def test_mitm_modifies_value_in_transit(stack):
    warm(stack)
    stack.twin.tampered("wl-1", value=1.21)  # even a tiny change
    assert stack.verdicts[-1]["verdict"] == "rejected"


def test_mitm_forges_guardian_verdict(stack):
    warm(stack)
    v = copy.deepcopy(stack.verdicts[-1])
    pub = stack.signer.public_key_pem()
    assert VerdictSigner.verify(v, pub)
    v["verdict"] = "trusted"; v["trust_score"] = 1.0; v["event_id"] = "evil"
    assert not VerdictSigner.verify(v, pub)
    other = VerdictSigner.generate().sign({k: x for k, x in v.items() if k != "sig"})
    assert not VerdictSigner.verify(other, pub)


def test_forged_release_control_ignored_by_twin(stack):
    warm(stack)
    for _ in range(3):
        stack.twin.spoofed("wl-1")
    fake = VerdictSigner.generate().sign({"type": "device_control", "action": "release",
                                          "device_id": "wl-1", "reason": "x"})
    stack.bus.publish("guardian/control/wl-1", fake)
    assert "wl-1" in stack.twin.quarantined and stack.twin.rejected_control


def test_jwt_replay_over_http(stack):
    from src.core.security import issue_tool_token
    t = issue_tool_token(stack.keys["brain"], "brain", "health")
    body = {"jsonrpc": "2.0", "method": "health", "id": 1}
    h = {"Authorization": f"Bearer {t}"}
    assert "result" in stack.client.post("/rpc", json=body, headers=h).json()
    assert stack.client.post("/rpc", json=body, headers=h).json()["error"]["code"] == -32001
