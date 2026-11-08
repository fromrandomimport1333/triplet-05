"""City Brain mock: for every Twin reading it calls Guardian's get_trust_verdict and only
acts when the verdict is NOT 'rejected' (suspicious is acted on, but flagged).

RPC transports: InProcessRpc (FastAPI TestClient) and HttpRpc (httpx + mTLS + per-call JWT).
"""
from __future__ import annotations

import time

from src.core.security import issue_tool_token


class RpcFailure(Exception):
    def __init__(self, error: dict):
        super().__init__(error.get("message"))
        self.error = error


class _RpcBase:
    def __init__(self, jwt_secret: str, issuer: str = "brain"):
        self.secret, self.issuer, self._id = jwt_secret, issuer, 0

    def _request(self, method: str, params: dict) -> tuple[dict, dict]:
        self._id += 1
        token = issue_tool_token(self.secret, self.issuer, method)
        return ({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id},
                {"Authorization": f"Bearer {token}"})

    @staticmethod
    def _unwrap(body: dict) -> dict:
        if "error" in body:
            raise RpcFailure(body["error"])
        return body["result"]


class InProcessRpc(_RpcBase):
    def __init__(self, client, jwt_secret: str, issuer: str = "brain"):
        super().__init__(jwt_secret, issuer)
        self.client = client

    def call(self, method: str, params: dict | None = None) -> dict:
        req, headers = self._request(method, params or {})
        return self._unwrap(self.client.post("/rpc", json=req, headers=headers).json())


class HttpRpc(_RpcBase):  # pragma: no cover - live mode
    def __init__(self, base_url: str, jwt_secret: str, issuer: str = "brain", *,
                 ca: str | None = None, cert: str | None = None, key: str | None = None):
        import ssl

        import httpx

        super().__init__(jwt_secret, issuer)
        ctx = ssl.create_default_context(cafile=ca)
        if cert:
            ctx.load_cert_chain(cert, key)
        self.http = httpx.Client(base_url=base_url, verify=ctx, timeout=5)

    def call(self, method: str, params: dict | None = None) -> dict:
        req, headers = self._request(method, params or {})
        return self._unwrap(self.http.post("/rpc", json=req, headers=headers).json())


class BrainMock:
    FLOOD_THRESHOLD_M = 3.0

    def __init__(self, rpc, twin=None, verdict_timeout_s: float = 5.0):
        self.rpc, self.twin, self.timeout = rpc, twin, verdict_timeout_s
        self.dispatched: list[dict] = []
        self.ignored: list[dict] = []

    def get_verdict(self, event_id: str) -> dict | None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                return self.rpc.call("get_trust_verdict", {"event_id": event_id})
            except RpcFailure as e:
                if e.error.get("code") != -32004 or time.monotonic() > deadline:
                    return None
                time.sleep(0.05)

    def on_twin_event(self, evt: dict) -> dict:
        """Decide whether to act on a Twin reading. Never acts on 'rejected' or missing verdicts."""
        v = self.get_verdict(evt.get("event_id", ""))
        decision = {"event_id": evt.get("event_id"), "verdict": v and v["verdict"]}
        if v is None or v["verdict"] == "rejected":
            decision["action"] = "ignored"
            self.ignored.append(decision)
            return decision
        if evt.get("sensor_type") == "water_level" and evt.get("value", 0) >= self.FLOOD_THRESHOLD_M:
            decision["action"] = "dispatch_flood_response"
            decision["flagged"] = v["verdict"] == "suspicious"
            self.dispatched.append(decision)
            if self.twin is not None:
                decision["twin_result"] = self.twin.execute_command(evt["device_id"], "open_floodgate")
        else:
            decision["action"] = "none"
        return decision
