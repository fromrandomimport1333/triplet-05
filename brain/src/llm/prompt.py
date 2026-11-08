"""Prompt and answer format for one incident.

The answer format is a JSON Schema given to the runtime's structured-output mode.
Its enums list only the devices and actions of this incident's context, so a
well-behaved runtime cannot even produce an unknown id. The answer is still
validated afterwards (validate.py): the schema is a help, not the safety check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import Context, render


@dataclass(slots=True)
class Prompt:
    version: str
    system: str
    user: str
    schema: dict[str, Any]
    context: Context            # what the prompt was built from (used by the mock LLM)


def build(ctx: Context, cfg: dict[str, Any]) -> Prompt:
    max_actions = int(cfg["validation"]["max_actions"])
    system = cfg["prompt"]["system"].format(max_actions=max_actions).strip()
    body = render(ctx, int(cfg["context"]["max_chars"]), int(cfg["context"]["call_text_max_chars"]))
    user = f"{body}\n\nPropose the actions for incident {ctx.incident.incident_id}. Answer with JSON only."
    return Prompt(cfg["prompt"]["version"], system, user, answer_schema(ctx, cfg), ctx)


def answer_schema(ctx: Context, cfg: dict[str, Any]) -> dict[str, Any]:
    targets = sorted(ctx.targets)
    actions = sorted({a for t in ctx.targets.values() for a in t.actions})
    action_item: dict[str, Any] = {
        "type": "object",
        "properties": {
            # an empty enum is invalid JSON Schema: with no target, no action can be produced
            "target_id": {"type": "string", "enum": targets} if targets else {"type": "string"},
            "action": {"type": "string", "enum": actions} if actions else {"type": "string"},
            "params": {"type": "object"},
        },
        "required": ["target_id", "action"],
    }
    return {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": action_item,
                "maxItems": int(cfg["validation"]["max_actions"]) if targets else 0,
            },
            "justification": {"type": "string"},
        },
        "required": ["actions", "justification"],
    }
