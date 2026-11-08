"""HTTP context API of the graph (stdlib ThreadingHTTPServer, JSON only, read-only).

    GET /context/{incident_id}   GET /zone/{zone_id}   GET /actuators?zone_id=...
    GET /history   GET /history/{incident_id}   (Ida 2021 reference, see history.py)
    GET /chain/{correlation_id}  GET /graph            GET /health
Unknown paths and ids answer 404 with a JSON body.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .service import GraphService, dumps


def make_server(service: GraphService, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "brain-graph"

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            url = urlsplit(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            try:
                status, doc = route(service, parts, parse_qs(url.query))
            except Exception as e:  # never kill the server thread
                service.log.error("http_handler_failed", path=self.path, error=repr(e)[:200])
                status, doc = 500, {"error": "internal error"}
            body = dumps(doc).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:   # JSON logs only
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def route(service: GraphService, parts: list[str], query: dict[str, list[str]]):
    if len(parts) == 2 and parts[0] == "context":
        return service.context(parts[1])
    if len(parts) == 2 and parts[0] == "zone":
        return service.zone(parts[1])
    if len(parts) == 2 and parts[0] == "chain":
        return service.chain(parts[1])
    if parts == ["actuators"]:
        return service.actuators((query.get("zone_id") or [None])[0])
    if parts == ["history"]:
        return service.history_view()
    if len(parts) == 2 and parts[0] == "history":
        return service.history_view(parts[1])
    if parts == ["graph"]:
        return service.view()
    if parts == ["health"]:
        return service.health()
    return 404, {"error": "unknown path", "path": "/" + "/".join(parts)}
