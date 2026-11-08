"""LLM runtimes. The service only knows the LlmBackend protocol, so the runtime
(Ollama today, llama.cpp or the mock tomorrow) is a config change, not a code change."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from .prompt import Prompt


class BackendTimeout(Exception):
    """The runtime did not answer within the time given."""


class BackendUnavailable(Exception):
    """The runtime cannot be reached or refused the request."""


class LlmBackend(Protocol):
    name: str

    def generate(self, prompt: Prompt, timeout_s: float) -> str:
        """Raw answer text. Raises BackendTimeout or BackendUnavailable."""

    def health(self) -> tuple[bool, str | None]:
        """(usable, reason when not usable)."""

    def warmup(self, timeout_s: float) -> None:
        """Load the model so the first real answer is fast. Errors are not fatal."""


class OllamaBackend:
    """Ollama HTTP API (/api/chat), structured output with the answer JSON Schema."""

    def __init__(self, cfg: dict[str, Any]):
        self.url = cfg["url"].rstrip("/")
        self.model = cfg["model"]
        self.digest = str(cfg.get("digest") or "")       # optional pin (prefix of the sha256 digest)
        self.name = f"ollama:{self.model}"
        self.options = {
            "temperature": cfg.get("temperature", 0),
            "seed": cfg.get("seed", 0),
            "num_predict": cfg.get("max_output_tokens", 300),
        }
        self.keep_alive = cfg.get("keep_alive", "30m")

    def generate(self, prompt: Prompt, timeout_s: float) -> str:
        """Streams the answer so the deadline is hard: past it the connection is closed,
        which also makes Ollama stop generating (the GPU is free for the next incident)."""
        deadline = time.monotonic() + timeout_s
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "format": prompt.schema,
            "stream": True,
            "options": self.options,
            "keep_alive": self.keep_alive,
        }
        parts: list[str] = []
        try:
            with self._open("/api/chat", body, timeout_s) as r:
                for line in r:
                    if time.monotonic() > deadline:
                        raise BackendTimeout(f"no complete answer within {timeout_s:.1f} s")
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise BackendUnavailable(str(chunk["error"])[:200])
                    parts.append((chunk.get("message") or {}).get("content") or "")
                    if chunk.get("done"):
                        return "".join(parts)
        except (TimeoutError, socket.timeout) as e:
            raise BackendTimeout(str(e)) from e
        except (OSError, ValueError) as e:
            raise BackendUnavailable(_reason(e)) from e
        raise BackendUnavailable("stream ended before the answer was complete")

    def health(self) -> tuple[bool, str | None]:
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=2) as r:
                tags = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, f"ollama unreachable ({_reason(e)})"
        wanted = {self.model, f"{self.model}:latest"}
        found = [m for m in tags.get("models", []) if {m.get("name"), m.get("model")} & wanted]
        if not found:
            return False, f"model {self.model} not pulled"
        digest = found[0].get("digest") or ""
        if self.digest and not digest.startswith(self.digest):
            # a re-pull can silently change the model under the same tag: do not use an unbenchmarked one
            return False, f"model {self.model} is build {digest[:12]}, config pins {self.digest[:12]}"
        return True, None

    def probe(self, timeout_s: float) -> float:
        """Seconds a tiny real answer takes (loads the model if needed). Raises like generate()."""
        start = time.monotonic()
        self._post("/api/generate", {"model": self.model, "prompt": "Answer with the JSON {\"ok\": true}.",
                                     "format": "json", "stream": False, "keep_alive": self.keep_alive,
                                     "options": {"num_predict": 8, "temperature": 0}}, timeout_s)
        return time.monotonic() - start

    def warmup(self, timeout_s: float) -> None:
        # an empty prompt loads the model into memory without generating anything
        self._post("/api/generate", {"model": self.model, "prompt": "", "keep_alive": self.keep_alive}, timeout_s)

    def _open(self, path: str, body: dict[str, Any], timeout_s: float):
        """Open a POST request; HTTP and connection errors become backend errors."""
        req = urllib.request.Request(
            f"{self.url}{path}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            return urllib.request.urlopen(req, timeout=max(timeout_s, 0.05))
        except (TimeoutError, socket.timeout) as e:
            raise BackendTimeout(str(e)) from e
        except urllib.error.HTTPError as e:
            raise BackendUnavailable(f"HTTP {e.code}: {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise BackendTimeout(str(e.reason)) from e
            raise BackendUnavailable(_reason(e)) from e

    def _post(self, path: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        try:
            with self._open(path, body, timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except (TimeoutError, socket.timeout) as e:
            raise BackendTimeout(str(e)) from e
        except (OSError, ValueError) as e:
            raise BackendUnavailable(_reason(e)) from e


def _reason(e: Exception) -> str:
    return str(getattr(e, "reason", e))
