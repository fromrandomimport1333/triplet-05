"""Scripted stand-in for the local LLM (team doc 03-llm section 6).

Modes:
- scripted: answers at once from the playbook of config/llm.yaml (mock.playbook)
- slow:     same answer after mock.slow_s seconds (longer than the budget -> llm_timeout)
- offline:  never answers; the service reports llm-status offline
- invalid:  returns broken JSON (-> llm_invalid_output)
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from src.llm.backends import BackendTimeout, BackendUnavailable
from src.llm.prompt import Prompt

MODES = ("scripted", "slow", "offline", "invalid")


class MockBackend:
    def __init__(self, cfg: dict[str, Any], mode: str | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.mode = mode or cfg.get("mode", "scripted")
        if self.mode not in MODES:
            raise ValueError(f"mock LLM mode must be one of {MODES}, not {self.mode!r}")
        self.slow_s = float(cfg.get("slow_s", 15))
        self.playbook: dict[str, list[list[str]]] = cfg.get("playbook", {})
        self.name = f"mock:{self.mode}"
        self._sleep = sleep

    def set_mode(self, mode: str) -> None:
        """Switch mode at run time (scenario runner, brain/control/llm). Raises ValueError."""
        if mode not in MODES:
            raise ValueError(f"mock LLM mode must be one of {MODES}, not {mode!r}")
        self.mode = mode
        self.name = f"mock:{mode}"

    def health(self) -> tuple[bool, str | None]:
        if self.mode == "offline":
            return False, "mock LLM in offline mode"
        return True, None

    def warmup(self, timeout_s: float) -> None:
        return None

    def generate(self, prompt: Prompt, timeout_s: float) -> str:
        if self.mode == "offline":
            raise BackendUnavailable("mock LLM in offline mode")
        if self.mode == "invalid":
            return '{"actions": [{"target_id": "valve-'          # cut in the middle, on purpose
        if self.mode == "slow":
            if self.slow_s > timeout_s:
                self._sleep(timeout_s)
                raise BackendTimeout(f"mock LLM answers after {self.slow_s:g} s")
            self._sleep(self.slow_s)
        return json.dumps(self.scripted(prompt))

    def scripted(self, prompt: Prompt) -> dict[str, Any]:
        ctx = prompt.context
        actions = []
        for target_type, action in self.playbook.get(ctx.incident.type, []):
            for t in ctx.targets.values():
                if t.type == target_type and action in t.actions:
                    actions.append({"target_id": t.target_id, "action": action, "params": {}})
                    break
        return {
            "actions": actions,
            "justification": f"Scripted mock answer for {ctx.incident.type} in {ctx.incident.zone_id}.",
        }
