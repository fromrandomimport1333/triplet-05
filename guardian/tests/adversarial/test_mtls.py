"""Real TLS server (uvicorn + Guardian app) proving mTLS: no client cert => handshake fails;
cert from another CA => fails; valid cert with wrong CN => 401; cert CN must match JWT issuer."""
import socket
import ssl
import threading
import time

import httpx
import pytest
import uvicorn

from mocks.gen_dev_certs import generate
from mocks.local_stack import LocalStack
from src.config import Settings
from src.core.events import VerdictSigner
from src.core.security import issue_tool_token, make_mtls_h11_protocol
from src.db.session import MemoryStore
from src.main import build_state, create_app
from src.mqtt.publisher import MemoryBus


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    d = generate(tmp_path_factory.mktemp("certs"))
    other = generate(tmp_path_factory.mktemp("rogue"))
    keys = {"brain": "b" * 32, "admin": "a" * 32}
    st = Settings(mqtt_enabled=False, require_mtls=True, jwt_keys=keys, twin_event_hmac_key="k",
                  allowed_client_cns={"brain.triplet": "brain", "admin.triplet": "admin"})
    state = build_state(st, store=MemoryStore(), publisher=MemoryBus(), signer=VerdictSigner.generate())
    port = _free_port()
    cfg = uvicorn.Config(create_app(st, state=state), host="127.0.0.1", port=port, log_level="error",
                         ssl_certfile=str(d / "guardian.pem"), ssl_keyfile=str(d / "guardian.key"),
                         ssl_ca_certs=str(d / "ca.pem"), ssl_cert_reqs=ssl.CERT_REQUIRED,
                         http=make_mtls_h11_protocol())
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"https://localhost:{port}", d, other, keys
    srv.should_exit = True; th.join(5)


def _call(url, d, cert_stem, keys, role, tool="health", certdir=None):
    certdir = certdir or d
    ctx = ssl.create_default_context(cafile=str(d / "ca.pem"))
    if cert_stem:
        ctx.load_cert_chain(str(certdir / f"{cert_stem}.pem"), str(certdir / f"{cert_stem}.key"))
    with httpx.Client(verify=ctx, timeout=5) as c:
        return c.post(f"{url}/rpc", json={"jsonrpc": "2.0", "method": tool, "id": 1},
                      headers={"Authorization": "Bearer " + issue_tool_token(keys[role], role, tool)})


def test_valid_client_cert(server):
    url, d, _, keys = server
    assert _call(url, d, "brain", keys, "brain").json()["result"] == {"status": "ok"}


def test_no_client_cert_fails_handshake(server):
    url, d, _, keys = server
    with pytest.raises((httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, ssl.SSLError)):
        _call(url, d, None, keys, "brain").json()


def test_cert_from_rogue_ca_fails(server):
    url, d, other, keys = server
    with pytest.raises((httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, ssl.SSLError)):
        _call(url, d, "brain", keys, "brain", certdir=other).json()


def test_valid_cert_not_allowlisted(server):
    url, d, _, keys = server
    assert _call(url, d, "twin", keys, "brain").status_code == 401


def test_stolen_admin_token_on_brain_cert(server):
    url, d, _, keys = server
    r = _call(url, d, "brain", keys, "admin", tool="release_device").json()
    assert r["error"]["code"] == -32001
