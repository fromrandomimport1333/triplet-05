from src.core.events import VerdictSigner
from tests.conftest import warm


def test_auto_quarantine_after_three_consecutive_rejects(stack):
    warm(stack)
    stack.twin.spoofed("wl-1"); stack.twin.spoofed("wl-1")
    assert "wl-1" not in stack.twin.quarantined
    stack.twin.spoofed("wl-1")
    assert "wl-1" in stack.twin.quarantined
    assert stack.rpc("brain").call("get_device_status", {"device_id": "wl-1"}) == {"status": "quarantined"}
    rows = stack.run(stack.store.audit_rows)
    q = [r for r in rows if r.action == "device_quarantined"]
    assert len(q) == 1 and len(q[0].details["evidence_event_ids"]) == 3
    ctl = stack.bus.retained["guardian/control/wl-1"]
    assert ctl["action"] == "quarantine" and VerdictSigner.verify(ctl, stack.signer.public_key_pem())
    assert stack.published("guardian/verdicts/rollback/wl-1")


def test_non_consecutive_rejects_do_not_quarantine(stack):
    warm(stack)
    for _ in range(3):
        stack.twin.spoofed("wl-1")
        stack.twin.normal("wl-1")
        stack.twin.normal("wl-1")
    assert "wl-1" not in stack.twin.quarantined


def test_replays_and_unsigned_do_not_count(stack):
    warm(stack)
    e = stack.twin.normal("wl-1")
    for _ in range(5):
        stack.twin.replay(e)
        stack.twin.tampered("wl-1")
    assert "wl-1" not in stack.twin.quarantined
    assert stack.rpc("brain").call("get_device_status", {"device_id": "wl-1"})["status"] == "trusted"


def test_release_is_only_way_out_and_is_logged(stack):
    warm(stack)
    for _ in range(3):
        stack.twin.spoofed("wl-1")
    stack.twin.normal("wl-1")  # good data does NOT un-quarantine
    assert stack.verdicts[-1]["verdict"] == "rejected" and "device_quarantined" in stack.verdicts[-1]["reason"]
    assert stack.rpc("ui").call("get_device_status", {"device_id": "wl-1"})["status"] == "quarantined"
    assert stack.rpc("admin").call("release_device", {"device_id": "wl-1"}) == {"status": "suspicious"}
    assert "wl-1" not in stack.twin.quarantined
    acts = [r.action for r in stack.run(stack.store.audit_rows)]
    assert "device_released" in acts
    assert stack.twin.execute_command("wl-1", "open_floodgate")["accepted"]


def test_release_noop_still_logged(stack):
    stack.rpc("admin").call("release_device", {"device_id": "never-quarantined"})
    assert "release_noop" in [r.action for r in stack.run(stack.store.audit_rows)]
