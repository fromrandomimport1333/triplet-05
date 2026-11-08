from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from src.common import config
from src.common.envelope import body_text, seal_multi
from src.common.log import EventLogger
from src.scoring.service import ScoringService
from tests.scoring.helpers import T0, Bus, Clock, meta_message


@pytest.fixture
def scoring_cfg() -> dict[str, Any]:
    cfg = copy.deepcopy(config.load("scoring"))
    cfg["logging"] = {"file": None, "level": "ERROR"}
    return cfg


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
def make_service(scoring_cfg, clock, bus):
    def make(meta: bool = True, **kw) -> ScoringService:
        svc = ScoringService(bus, clock=clock, scoring_cfg=kw.pop("cfg", scoring_cfg),
                             logger=EventLogger("test-scoring", None, "ERROR"), **kw)
        if meta:
            svc.handle("brain/internal/meta", json.dumps(meta_message()), True)
        return svc
    return make


@pytest.fixture
def svc(make_service) -> ScoringService:
    return make_service()


def sealed_event(body: dict[str, Any], keys: dict[str, bytes] | None) -> str:
    return seal_multi(body, keys) if keys else body_text(body)
