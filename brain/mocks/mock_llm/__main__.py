"""Run the mock LLM: python -m mocks.mock_llm [scripted|slow|offline|invalid] (from brain/).

Same LLM service, topics and timeline events as python -m src.llm, but answers come
from the scripted backend, so no model or GPU is needed.
"""

from __future__ import annotations

import sys

from src.common import config
from src.common.mqtt import BrainMqtt
from src.llm.__main__ import run

from .backend import MODES, MockBackend
from .control import LlmControl


def start_control(backend: MockBackend) -> BrainMqtt | None:
    """Optional: listen on brain/control/llm so the scenario runner can switch the mode (mock only)."""
    spec = config.load("topics").get("control", {}).get("llm")
    if not spec:
        return None
    control = LlmControl(backend, spec["topic"])
    client = BrainMqtt(config.load("broker")["client_ids"]["default_prefix"] + "llm-control", logger=control.log)
    client.on_message = control.handle
    client.subscribe(spec["topic"], int(spec.get("qos", 1)))
    client.start()
    return client


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode is not None and mode not in MODES:
        sys.exit(f"usage: python -m mocks.mock_llm [{'|'.join(MODES)}]")
    backend = MockBackend(config.load("llm").get("mock", {}), mode)
    control = start_control(backend)
    try:
        run(backend, client_suffix="llm")
    finally:
        if control is not None:
            control.stop()


if __name__ == "__main__":
    main()
