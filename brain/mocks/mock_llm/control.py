"""Test control of the mock LLM: brain/control/llm {"mode": "offline" | "scripted" | "slow" | "invalid"}.

Mock only (the real LLM module never reads this topic). Published by the scenario runner
(scenarios/run.py), plain JSON like brain/control/run: the topic is inside the Brain's own namespace,
which only Brain accounts may use (broker ACL).
"""

from __future__ import annotations

import json

from src.common.log import EventLogger

from .backend import MockBackend


class LlmControl:
    def __init__(self, backend: MockBackend, topic: str, logger: EventLogger | None = None):
        self.backend = backend
        self.topic = topic
        self.log = logger or EventLogger("mock-llm-control")

    def handle(self, topic: str, payload: bytes | str, retained: bool = False) -> None:
        """Never raises; a bad message is logged and ignored."""
        if topic != self.topic:
            return
        try:
            body = json.loads(payload)
            if not isinstance(body, dict):
                raise ValueError("body is not a JSON object")
            old = self.backend.mode
            self.backend.set_mode(body["mode"])
            self.log.info("mock_llm_mode", old=old, mode=self.backend.mode, retained=retained)
        except (ValueError, KeyError, TypeError) as e:
            self.log.warning("mock_llm_control_ignored", error=str(e)[:200])
