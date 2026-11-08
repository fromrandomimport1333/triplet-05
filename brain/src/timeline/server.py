"""HTTP side of the timeline: the page, a Server-Sent Events stream and a small JSON API.

    GET /                     the live screen (built React app from static/, works offline; falls back to page.html)
    GET /static/...           its script, styles and fonts
    GET /events               SSE: `row` events (id = seq) and a `state` event every state_every_s;
                              catches up from ?after=<seq> or the Last-Event-ID header
    GET /api/recent?limit=N   last rows (page reload)
    GET /api/status           header, counters, incidents, decision, city map
    GET /api/chain/{cid}      rows of one chain, plus the graph's GET /chain/{cid} when it answers

stdlib only (http.server.ThreadingHTTPServer): one thread per screen, nothing to install.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .service import TimelineService

PAGE = Path(__file__).with_name("page.html")       # previous single-file screen: fallback when the UI is not built
STATIC = Path(__file__).with_name("static")        # built React screen (brain/ui, `npm run build`), committed
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json", ".map": "application/json"}


class TimelineServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: TimelineService, cfg: dict[str, Any]):
        self.service = service
        self.cfg = cfg
        index = STATIC / "index.html"
        self.page = index.read_bytes() if index.is_file() else PAGE.read_bytes()
        self.stopping = threading.Event()
        super().__init__(address, Handler)

    def stop(self) -> None:
        self.stopping.set()
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    server: TimelineServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:   # quiet: the service has its own log
        pass

    # --- helpers ------------------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    # --- routes -------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        svc = self.server.service
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, self.server.page, "text/html; charset=utf-8")
            elif url.path.startswith("/static/"):
                self._static(url.path[len("/static/"):])
            elif url.path == "/events":
                self._events(qs)
            elif url.path == "/api/recent":
                limit = int(qs.get("limit", ["300"])[0])
                after = int(qs.get("after", ["0"])[0])
                self._json(svc.recent(max(0, min(limit, 5000)), after))
            elif url.path == "/api/status":
                self._json(svc.snapshot())
            elif url.path.startswith("/api/chain/"):
                cid = urllib.parse.unquote(url.path[len("/api/chain/"):])
                self._json(self._chain(cid))
            elif url.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def _static(self, rel: str) -> None:
        target = (STATIC / urllib.parse.unquote(rel)).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():      # no path traversal
            self._json({"error": "not found"}, 404)
            return
        self._send(200, target.read_bytes(), TYPES.get(target.suffix.lower(), "application/octet-stream"))

    def _chain(self, cid: str) -> dict[str, Any]:
        out: dict[str, Any] = {"correlation_id": cid, "local": self.server.service.chain(cid),
                               "graph": None, "graph_error": None}
        g = self.server.cfg.get("graph", {})
        base = (g.get("url") or "").rstrip("/")
        if not base:
            out["graph_error"] = "no graph url configured"
            return out
        try:
            with urllib.request.urlopen(f"{base}/chain/{urllib.parse.quote(cid, safe='')}",
                                        timeout=float(g.get("timeout_s", 1.5))) as r:
                out["graph"] = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["graph_error"] = str(getattr(e, "reason", e))[:200]
        return out

    def _events(self, qs: dict[str, list[str]]) -> None:
        svc = self.server.service
        sse = self.server.cfg.get("sse", {})
        every = float(sse.get("state_every_s", 1))
        after = self.headers.get("Last-Event-ID") or qs.get("after", ["0"])[0]
        try:
            after_seq = int(after)
        except ValueError:
            after_seq = 0
        q = svc.subscribe()                      # subscribe first, then replay: nothing is missed
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = after_seq
        try:
            self.wfile.write(b"retry: 2000\n\n")
            if after_seq:
                for row in svc.recent(0, after_seq):
                    last = self._write_row(row, last)
            self._write("state", svc.snapshot())
            next_state = time.monotonic() + every
            while not self.server.stopping.is_set():
                timeout = max(0.05, next_state - time.monotonic())
                try:
                    row = q.get(timeout=timeout)
                    last = self._write_row(row, last)
                except queue.Empty:
                    pass
                if time.monotonic() >= next_state:
                    if not svc.is_subscribed(q):     # dropped for falling behind: let it reconnect
                        break
                    self._write("state", svc.snapshot())
                    next_state = time.monotonic() + every
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            svc.unsubscribe(q)
            self.close_connection = True

    def _write_row(self, row: dict[str, Any], last: int) -> int:
        if row["seq"] <= last:                   # already sent during the catch-up
            return last
        self._write("row", row, row["seq"])
        return row["seq"]

    def _write(self, event: str, data: Any, event_id: int | None = None) -> None:
        text = json.dumps(data, ensure_ascii=False, default=str)
        msg = (f"id: {event_id}\n" if event_id is not None else "") + f"event: {event}\ndata: {text}\n\n"
        self.wfile.write(msg.encode("utf-8"))
        self.wfile.flush()


def serve(service: TimelineService, cfg: dict[str, Any], host: str | None = None,
          port: int | None = None) -> tuple[TimelineServer, threading.Thread]:
    """Start the server in a background thread; port 0 = any free port (tests)."""
    http = cfg.get("http", {})
    srv = TimelineServer((host or http.get("host", "127.0.0.1"),
                          int(http.get("port", 8090)) if port is None else port), service, cfg)
    t = threading.Thread(target=srv.serve_forever, name="timeline-http", daemon=True)
    t.start()
    return srv, t
