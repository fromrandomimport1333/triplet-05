"""Topic patterns from config/topics.yaml: "city/{zone_id}/call" <-> "city/+/call"."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Route:
    kind: str           # meta, reading, call, actuator, unit, verdict, quarantine, *_status
    source: str         # twin | guardian
    qos: int
    parts: tuple[str, ...]

    @property
    def subscription(self) -> str:
        return "/".join("+" if _is_var(p) else p for p in self.parts)


@dataclass(frozen=True, slots=True)
class Match:
    route: Route
    vars: dict[str, str]


def _is_var(part: str) -> bool:
    return part.startswith("{") and part.endswith("}")


class TopicRouter:
    def __init__(self, external_in: dict[str, dict]):
        self.routes = [
            Route(kind, spec["source"], int(spec.get("qos", 1)), tuple(spec["pattern"].split("/")))
            for kind, spec in external_in.items()
        ]

    def match(self, topic: str) -> Match | None:
        levels = topic.split("/")
        for route in self.routes:
            if len(levels) != len(route.parts):
                continue
            found: dict[str, str] = {}
            for part, level in zip(route.parts, levels, strict=True):   # lengths checked above
                if _is_var(part):
                    found[part[1:-1]] = level
                elif part != level:
                    break
            else:
                return Match(route, found)
        return None


def fill(template: str, **values: str) -> str:
    """brain/internal/trust/{sensor_id} -> brain/internal/trust/water-level-04"""
    return template.format(**values)
