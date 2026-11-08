"""The four Guardian JSON-RPC 2.0 tools (Phase 1 API contract)."""
from __future__ import annotations

from ..core.events import VerdictSigner


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


INVALID_PARAMS = -32602
NOT_FOUND = -32004


def _str_param(params: dict, name: str) -> str:
    v = params.get(name) if isinstance(params, dict) else None
    if not isinstance(v, str) or not v or len(v) > 256:
        raise RpcError(INVALID_PARAMS, f"param '{name}' must be a non-empty string (<=256 chars)")
    return v


class GuardianTools:
    def __init__(self, store, signer: VerdictSigner, quarantine, pipeline) -> None:
        self.store, self.signer, self.quarantine, self.pipeline = store, signer, quarantine, pipeline

    async def get_trust_verdict(self, params: dict, caller: dict) -> dict:
        """-> {event_id, trust_score, verdict, reason, sig}. Signed by Guardian."""
        eid = _str_param(params, "event_id")
        v = await self.store.get_verdict(eid)
        if v is None:
            raise RpcError(NOT_FOUND, f"no verdict for event_id {eid}")
        return self.signer.sign({"event_id": eid, "trust_score": round(float(v.trust_score), 4),
                                 "verdict": v.verdict, "reason": v.reason})

    async def get_device_status(self, params: dict, caller: dict) -> dict:
        """-> {status: trusted|suspicious|quarantined}. Unknown devices are 'trusted' (no evidence)."""
        did = _str_param(params, "device_id")
        if await self.store.is_quarantined(did):
            return {"status": "quarantined"}
        d = await self.store.get_device(did)
        return {"status": d.status if d else "trusted"}

    async def release_device(self, params: dict, caller: dict) -> dict:
        """Admin-only manual override. Always writes audit_log. -> {status}."""
        did = _str_param(params, "device_id")
        actor = f"{caller.get('role')}:{caller.get('sub')}"
        res = await self.quarantine.release(did, actor)
        if res["released"]:
            self.pipeline.reset_device(did)
        return {"status": res["status"]}

    async def health(self, params: dict, caller: dict) -> dict:
        """-> {status: ok|degraded}."""
        try:
            await self.store.get_device("__healthcheck__")
            return {"status": "ok"}
        except Exception:
            return {"status": "degraded"}

    def registry(self):
        return {"get_trust_verdict": self.get_trust_verdict,
                "get_device_status": self.get_device_status,
                "release_device": self.release_device,
                "health": self.health}
