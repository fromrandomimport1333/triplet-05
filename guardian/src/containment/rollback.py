"""Rollback advisory: when a device is quarantined, readings from it that were accepted
recently (trusted/suspicious) become doubtful. Guardian cannot undo City Brain's actions
(not its layer), so it publishes a signed advisory listing those event_ids; City Brain
decides whether to revert actions it took on them."""
from __future__ import annotations

from datetime import timedelta

from ..core.events import VerdictSigner, iso, utcnow


async def build_rollback_advisory(store, signer: VerdictSigner, device_id: str,
                                  window_s: int) -> dict:
    """Return a signed advisory with event_ids accepted from `device_id` in the last `window_s`."""
    since = utcnow() - timedelta(seconds=window_s)
    rows = await store.verdicts_since(device_id, since)
    ids = [r.event_id for r in rows if r.verdict in ("trusted", "suspicious")]
    return signer.sign({
        "schema_version": "1.0",
        "type": "rollback_advisory",
        "device_id": device_id,
        "window_start": iso(since),
        "event_ids": ids,
        "issued_at": iso(utcnow()),
    })


async def publish_rollback(store, publisher, signer: VerdictSigner, topic_verdicts: str,
                           device_id: str, window_s: int) -> dict:
    """Build the advisory, publish it on <topic_verdicts>/rollback/<device_id> and audit-log it."""
    adv = await build_rollback_advisory(store, signer, device_id, window_s)
    publisher.publish(f"{topic_verdicts}/rollback/{device_id}", adv, qos=1)
    await store.append_audit("rollback_advisory", device_id, {"event_ids": adv["event_ids"]})
    return adv
