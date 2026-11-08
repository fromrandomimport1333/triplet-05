"""Device quarantine lifecycle: automatic quarantine after N consecutive rejects and the
manual release (the ONLY way out). Every transition is written to the hash-chained audit_log."""
from __future__ import annotations

from ..core.events import VerdictSigner
from .isolate import publish_isolation
from .rollback import publish_rollback


class QuarantineManager:
    """Owns quarantine state transitions for all devices."""

    def __init__(self, store, publisher, signer: VerdictSigner, *, topic_control: str,
                 topic_verdicts: str, rollback_window_s: int = 600, timeline=None) -> None:
        """Wire the manager to storage, the MQTT publisher and Guardian's signer."""
        self.store, self.publisher, self.signer = store, publisher, signer
        self.topic_control, self.topic_verdicts = topic_control, topic_verdicts
        self.rollback_window_s = rollback_window_s
        self.timeline = timeline
        # async callbacks (device_id, state, reason, evidence) -> None; the Brain-profile
        # adapter registers one to publish guardian/quarantine/<device_id>.
        self.listeners: list = []

    async def _notify(self, device_id: str, state: str, reason: str, evidence: list[str]) -> None:
        for cb in list(self.listeners):
            await cb(device_id, state, reason, evidence)

    async def quarantine(self, device_id: str, reason: str, evidence: list[str]) -> dict:
        """Quarantine `device_id`: DB row, device status, audit 'device_quarantined',
        retained signed control message to Twin, rollback advisory to Brain."""
        await self.store.ensure_device(device_id)
        await self.store.upsert_quarantine(device_id, reason)
        dev = await self.store.get_device(device_id)
        await self.store.update_device(device_id, dev.trust_score if dev else 0.0, "quarantined")
        await self.store.append_audit("device_quarantined", device_id,
                                      {"reason": reason, "evidence_event_ids": evidence})
        msg = publish_isolation(self.publisher, self.signer, self.topic_control, device_id,
                                "quarantine", reason)
        await publish_rollback(self.store, self.publisher, self.signer, self.topic_verdicts,
                               device_id, self.rollback_window_s)
        if self.timeline:
            self.timeline.add("quarantine", device_id, reason, "critical")
        await self._notify(device_id, "quarantined", reason, evidence)
        return msg

    async def quarantined_devices(self) -> list[str]:
        """Device ids currently quarantined (used by guardian/control op=reset)."""
        rows = getattr(self.store, "quarantine", None)
        if isinstance(rows, dict):  # MemoryStore
            return [d for d, q in rows.items() if q.released_at is None]
        async with self.store.pool.acquire() as c:  # PgStore
            return [r["device_id"] for r in await c.fetch(
                "SELECT device_id FROM quarantine_list WHERE released_at IS NULL")]

    async def release(self, device_id: str, actor: str) -> dict:
        """Manual/admin release. Always audit-logged (also when the device was not quarantined)."""
        released = await self.store.release_quarantine(device_id)
        await self.store.append_audit("device_released" if released else "release_noop", device_id,
                                      {"actor": actor, "was_quarantined": released})
        if released:
            await self.store.update_device(device_id, 0.5, "suspicious")
            publish_isolation(self.publisher, self.signer, self.topic_control, device_id,
                              "release", f"released by {actor}", actor=actor)
            if self.timeline:
                self.timeline.add("release", device_id, f"released by {actor}", "info")
            await self._notify(device_id, "released", f"released by {actor}", [])
        dev = await self.store.get_device(device_id)
        return {"device_id": device_id, "status": dev.status if dev else "trusted",
                "released": released}
