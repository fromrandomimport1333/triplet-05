"""Network-side containment: builds and publishes the signed control message that tells
Twin (and City Brain) to stop trusting / commanding a device.

Topic  : <TOPIC_CONTROL>/<device_id>      (default guardian/control/<device_id>)
QoS 1, RETAINED — a Twin instance that (re)connects later still learns the state.
"""
from __future__ import annotations

from ..core.events import VerdictSigner, iso, utcnow


def build_control_message(signer: VerdictSigner, device_id: str, action: str, reason: str,
                          actor: str = "guardian") -> dict:
    """Return a signed control message. `action` is 'quarantine' or 'release'."""
    if action not in ("quarantine", "release"):
        raise ValueError("action must be 'quarantine' or 'release'")
    return signer.sign({
        "schema_version": "1.0",
        "type": "device_control",
        "action": action,
        "device_id": device_id,
        "reason": reason,
        "actor": actor,
        "issued_at": iso(utcnow()),
    })


def publish_isolation(publisher, signer: VerdictSigner, topic_control: str, device_id: str,
                      action: str, reason: str, actor: str = "guardian") -> dict:
    """Publish the signed control message (retained) and return it."""
    msg = build_control_message(signer, device_id, action, reason, actor)
    publisher.publish(f"{topic_control}/{device_id}", msg, retain=True, qos=1)
    return msg
