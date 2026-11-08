"""Incident history: what a real storm looked like, as a reference for a live incident.

The library (data/history/nyc311-ida.json) is built offline by tools/build_history.py from the
NYC 311 calls of Hurricane Ida (2021), the port of the notebooks in brain/notebooks/. It holds
surge levels per incident type, the storm's surge waves and the co-surge measured in the data
(traffic signal failures per hour inside vs outside flood waves).

For a live incident the graph matches its type and severity to a surge level and returns a
short, factual hint (what share of such calls came inside a wave, how long the waves lasted,
what else rose with them). The notebook's Qdrant search embeds a templated sentence (call type
+ surge level + count), so its nearest neighbours are exactly these calls; matching on type and
level gives the same answer without a vector database, a model download or the network.

The hint is context, never an instruction: it is data from another city and storm.
"""

from __future__ import annotations

import json
from typing import Any

from ..common import config


class History:
    def __init__(self, cfg: dict[str, Any] | None, log: Any = None):
        self.cfg = cfg or {}
        self.lib: dict[str, Any] | None = None
        if not self.cfg.get("enabled", False):
            return
        path = config.resolve(self.cfg.get("file", "data/history/nyc311-ida.json"))
        try:
            self.lib = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            if log is not None:
                log.warning("history_not_loaded", file=str(path), error=str(e)[:200])
            return
        if log is not None:
            log.info("history_loaded", source=self.lib.get("source"), calls=self.lib.get("calls"),
                     waves=len(self.lib.get("waves") or []))

    @property
    def loaded(self) -> bool:
        return self.lib is not None

    def level_for(self, severity: str | None) -> str | None:
        return (self.cfg.get("severity_level") or {}).get(severity or "")

    def match(self, incident_type: str, severity: str | None, live_calls_60m: int | None = None) -> dict[str, Any] | None:
        """The reference for one live incident, or None when the library has nothing comparable."""
        if self.lib is None:
            return None
        ref_type = (self.cfg.get("types") or {}).get(incident_type)
        level = self.level_for(severity)
        t = (self.lib.get("types") or {}).get(ref_type or "")
        if not t or not level or level not in t.get("levels", {}):
            return None
        lv = t["levels"][level]
        waves = self.lib.get("waves") or []
        out: dict[str, Any] = {"source": self.lib.get("source"), "match": f"{ref_type}, {level}",
                               "past_calls": lv["calls"], "in_wave": lv["share_in_wave"]}
        if live_calls_60m is not None:
            out["live_calls_60m"] = live_calls_60m
        out["hint"] = self._hint(ref_type, level, lv, waves)
        return out

    def _hint(self, ref_type: str, level: str, lv: dict[str, Any], waves: list[dict[str, Any]]) -> str:
        name = ref_type.replace("_", " ")
        s = f"Ida 2021 (NYC): {round(lv['share_in_wave'] * 100)}% of {name} calls at '{level}' came inside a surge wave"
        if waves and lv["share_in_wave"] >= float(self.cfg.get("wave_share_min", 0.5)):
            hours = sorted(w["hours"] for w in waves)
            s += f" ({len(waves)} waves of {hours[0]}-{hours[-1]} h, peak {max(w['peak_calls_per_h'] for w in waves)} calls/h)"
            for cs in self.lib.get("co_surge") or []:
                if cs.get("when") == ref_type and cs.get("ratio"):
                    s += f"; {cs['also_rises'].replace('_', ' ')} ran {cs['ratio']}x higher during waves"
        return s[: int(self.cfg.get("hint_chars", 260))]

    def summary(self) -> dict[str, Any]:
        """GET /history: the library itself (no per-call data)."""
        if self.lib is None:
            return {"loaded": False}
        keep = ("source", "title", "calls", "period", "surge_levels", "wave_rule", "baseline", "waves", "co_surge", "types")
        return {"loaded": True, "mapping": {"types": self.cfg.get("types"), "severity_level": self.cfg.get("severity_level")}} \
            | {k: self.lib[k] for k in keep if k in self.lib}
