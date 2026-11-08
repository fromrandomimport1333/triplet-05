"""HTTP + WebSocket transport for JSON-RPC 2.0, the timeline endpoint and the public key."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from ..core.security import AuthError, ForbiddenError, check_client_cert
from .tools import RpcError

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INTERNAL = -32700, -32600, -32601, -32603
UNAUTHORIZED, FORBIDDEN = -32001, -32003


def _err(id_, code, msg, data=None):
    e: dict[str, Any] = {"code": code, "message": msg}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "error": e, "id": id_}


def build_router(app_state) -> APIRouter:
    r = APIRouter()
    s = app_state.settings

    def cert_issuer(scope) -> str | None:
        """mTLS application-level CN check (HTTP). Returns the issuer bound to the cert."""
        if not s.require_mtls:
            return None
        tls = (scope.get("extensions") or {}).get("tls") or {}
        return check_client_cert(tls.get("peercert"), s.allowed_client_cns)

    async def dispatch(req: Any, token: str | None, issuer: str | None) -> dict | None:
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return _err(req.get("id") if isinstance(req, dict) else None, INVALID_REQUEST, "invalid request")
        id_, method = req.get("id"), req["method"]
        params = req.get("params") or {}
        if not isinstance(params, dict):
            return _err(id_, INVALID_REQUEST, "params must be an object")
        params = dict(params)
        token = params.pop("_jwt", None) or token
        fn = app_state.tools.registry().get(method)
        if fn is None:
            return _err(id_, METHOD_NOT_FOUND, f"method '{method}' not found")
        try:
            claims = app_state.jwt.verify(token, method, cert_issuer=issuer)
            result = await fn(params, claims)
        except AuthError as e:
            return _err(id_, UNAUTHORIZED, "unauthorized", str(e))
        except ForbiddenError as e:
            return _err(id_, FORBIDDEN, "forbidden", str(e))
        except RpcError as e:
            return _err(id_, e.code, e.message, e.data)
        except Exception:  # never leak internals
            return _err(id_, INTERNAL, "internal error")
        if "id" not in req:
            return None  # notification
        return {"jsonrpc": "2.0", "result": result, "id": id_}

    def bearer(h: str | None) -> str | None:
        return h[7:] if h and h.lower().startswith("bearer ") else None

    @r.post("/rpc")
    async def rpc(request: Request):
        try:
            issuer = cert_issuer(request.scope)
        except AuthError as e:
            return JSONResponse(_err(None, UNAUTHORIZED, "unauthorized", str(e)), status_code=401)
        try:
            body = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(_err(None, PARSE_ERROR, "parse error"))
        token = bearer(request.headers.get("authorization"))
        if isinstance(body, list):
            if not body:
                return JSONResponse(_err(None, INVALID_REQUEST, "empty batch"))
            out = []
            for b in body:  # one JWT per call: each batch item carries params._jwt
                resp = await dispatch(b, None, issuer)
                if resp is not None:
                    out.append(resp)
            return JSONResponse(out)
        resp = await dispatch(body, token, issuer)
        return JSONResponse(resp) if resp is not None else PlainTextResponse("", status_code=204)

    @r.websocket("/rpc/ws")
    async def rpc_ws(ws: WebSocket):
        # TLS client-cert is enforced at handshake; CN<->issuer binding is HTTP-only (see INTERFACE.md)
        await ws.accept()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json(_err(None, PARSE_ERROR, "parse error"))
                    continue
                resp = await dispatch(req, None, None)
                if resp is not None:
                    await ws.send_json(resp)
        except WebSocketDisconnect:
            return

    @r.get("/timeline")
    async def timeline(request: Request, since: str | None = None, limit: int = 200):
        try:
            issuer = cert_issuer(request.scope)
            app_state.jwt.verify(bearer(request.headers.get("authorization")), "timeline", cert_issuer=issuer)
        except (AuthError, ForbiddenError) as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        return {"layer": "guardian", "entries": app_state.timeline.list(since, max(1, min(limit, 1000)))}

    @r.get("/.well-known/guardian-verdict-key")
    async def pubkey():
        return PlainTextResponse(app_state.signer.public_key_pem(), media_type="application/x-pem-file")

    @r.get("/healthz")
    async def healthz():  # unauthenticated liveness for docker healthcheck only
        return {"status": "ok"}

    return r
