"""Per-tool-call JWT (issue/verify) and mTLS client-certificate checks.

JWT model
---------
* HS256, one secret PER ISSUER (kid header = issuer: brain | twin | ui | admin).
  The caller's role is derived from WHICH key verified the token, never from a claim,
  so City Brain cannot mint an admin token with its own key.
* A token authorises exactly ONE tool: claim ``tool`` must equal the JSON-RPC method,
  and that tool must be in ROLE_SCOPES[issuer].
* Short-lived (exp - iat <= JWT_MAX_TTL_S) and single-use (``jti`` cache).

mTLS model
----------
* The TLS server requires a client certificate signed by the triplet CA (handshake level).
* On HTTP, the peer certificate CN is additionally bound to the JWT issuer
  (ALLOWED_CLIENT_CNS="brain.triplet=brain,...").
"""
from __future__ import annotations

import ssl
import threading
import time
import uuid
from typing import Any

import jwt

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "brain": frozenset({"get_trust_verdict", "get_device_status", "health", "timeline"}),
    "twin": frozenset({"get_device_status", "health", "timeline"}),
    "ui": frozenset({"get_device_status", "health", "timeline"}),
    "admin": frozenset({"get_trust_verdict", "get_device_status", "release_device", "health", "timeline"}),
}


class AuthError(Exception):
    """Authentication failed (bad/missing/expired/replayed token or cert)."""


class ForbiddenError(Exception):
    """Authenticated, but not allowed to call this tool."""


def issue_tool_token(secret: str, issuer: str, tool: str, *, subject: str | None = None,
                     audience: str = "guardian", ttl_s: int = 60) -> str:
    """Mint a single-use token for ONE tool call (used by partners and by our mocks)."""
    now = int(time.time())
    claims = {"iss": issuer, "sub": subject or issuer, "aud": audience, "tool": tool,
              "iat": now, "exp": now + ttl_s, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, secret, algorithm="HS256", headers={"kid": issuer})


class JwtVerifier:
    def __init__(self, keys: dict[str, str], audience: str = "guardian", max_ttl_s: int = 120):
        self.keys = dict(keys)
        self.audience = audience
        self.max_ttl_s = max_ttl_s
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, token: str | None, tool: str, *, cert_issuer: str | None = None) -> dict[str, Any]:
        """Return claims if `token` authorises calling `tool`; raise AuthError/ForbiddenError."""
        if not token:
            raise AuthError("missing bearer token")
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed token: {e}") from None
        key = self.keys.get(kid or "")
        if not key:
            raise AuthError("unknown token issuer")
        try:
            claims = jwt.decode(token, key, algorithms=["HS256"], audience=self.audience,
                                options={"require": ["iss", "aud", "exp", "iat", "jti", "tool"]})
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from None
        if claims["iss"] != kid:
            raise AuthError("iss/kid mismatch")
        if claims["exp"] - claims["iat"] > self.max_ttl_s:
            raise AuthError("token lifetime exceeds policy")
        if cert_issuer is not None and cert_issuer != kid:
            raise AuthError("client certificate does not match token issuer")
        if claims["tool"] != tool:
            raise ForbiddenError(f"token is scoped to '{claims['tool']}', not '{tool}'")
        if tool not in ROLE_SCOPES.get(kid, frozenset()):
            raise ForbiddenError(f"role '{kid}' may not call '{tool}'")
        now = time.time()
        with self._lock:
            for j, exp in list(self._seen.items()):
                if exp < now:
                    del self._seen[j]
            if claims["jti"] in self._seen:
                raise AuthError("token already used (jti replay)")
            self._seen[claims["jti"]] = float(claims["exp"])
        claims["role"] = kid
        return claims


def cert_common_name(peercert: dict | None) -> str | None:
    """Extract CN from an ssl.getpeercert() dict."""
    if not peercert:
        return None
    for rdn in peercert.get("subject", ()):
        for k, v in rdn:
            if k == "commonName":
                return v
    return None


def check_client_cert(peercert: dict | None, allowed_cns: dict[str, str]) -> str:
    """mTLS application-level check: cert present and CN allow-listed. Returns mapped issuer."""
    cn = cert_common_name(peercert)
    if cn is None:
        raise AuthError("client certificate required (mTLS)")
    if cn not in allowed_cns:
        raise AuthError(f"client certificate CN '{cn}' not allowed")
    return allowed_cns[cn]


def server_ssl_context(cert: str, key: str, ca: str) -> ssl.SSLContext:
    """TLS server context that REQUIRES a client cert signed by the triplet CA."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    ctx.load_verify_locations(ca)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def client_ssl_context(cert: str, key: str, ca: str) -> ssl.SSLContext:
    """TLS client context presenting our cert and verifying the server against the triplet CA."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca)
    ctx.load_cert_chain(cert, key)
    return ctx


def make_mtls_h11_protocol():
    """uvicorn HTTP protocol class that exposes the peer certificate to the ASGI app as
    scope['extensions']['tls']['peercert'] (uvicorn does not do this natively)."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    class MTLSH11Protocol(H11Protocol):
        def connection_made(self, transport):  # type: ignore[override]
            super().connection_made(transport)
            peercert = transport.get_extra_info("peercert")
            inner = self.app

            async def app(scope, receive, send):
                scope = dict(scope)
                ext = dict(scope.get("extensions") or {})
                ext["tls"] = {"peercert": peercert}
                scope["extensions"] = ext
                await inner(scope, receive, send)

            self.app = app

    return MTLSH11Protocol
