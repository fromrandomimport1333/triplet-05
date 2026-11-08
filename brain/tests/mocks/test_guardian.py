"""The Guardian mock between the Twin mock and the Brain's real input module.

Twin readings go to the Guardian and to the Brain; the Guardian's verdicts, quarantine notices and
status go to the Brain. Everything the Guardian sends must pass the input checks with no special case.
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from mocks.guardian.core import GuardianCore, parse_mode
from mocks.guardian.service import GuardianService
from src.common import config
from src.common.envelope import seal
from src.common.log import EventLogger
from tests.mocks.conftest import GUARDIAN_KEY, TWIN_KEY, Bus, Rig


class GuardianRig(Rig):
    def __init__(self, tmp_path, guardian_cfg=None, faults=None, **sim):
        super().__init__(tmp_path, faults=faults, **sim)
        cfg = deepcopy(config.load("guardian-mock"))
        cfg.update(guardian_cfg or {})
        self.g_bus = Bus()
        self.guardian = GuardianService(self.g_bus, key=GUARDIAN_KEY, twin_key=TWIN_KEY, clock=self.clock,
                                        guardian_cfg=cfg, logger=EventLogger(f"gd-test-{id(tmp_path)}"))
        self.g_fed = 0

    def feed(self) -> None:
        for topic, payload, _qos, retain in self.twin_bus.sent[self.fed:]:
            self.brain.handle(topic, payload, retain)
            self.guardian.handle(topic, payload, retain)
        self.fed = len(self.twin_bus.sent)
        self.feed_guardian()

    def feed_guardian(self) -> None:
        for topic, payload, _qos, _retain in self.g_bus.sent[self.g_fed:]:
            self.brain.handle(topic, payload, False)     # live delivery: subscribers get retain=0
        self.g_fed = len(self.g_bus.sent)

    def connect(self) -> None:
        self.twin.on_connect()
        self.guardian.on_connect()
        self.feed()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.clock.t += 1
            self.twin.tick()
            self.feed()
            self.guardian.tick()
            self.feed_guardian()
            self.brain.tick()

    def control(self, body: dict, key: bytes = GUARDIAN_KEY) -> None:
        self.guardian.handle("guardian/control", seal(body, key))
        self.feed_guardian()

    def verdicts(self, sensor: str | None = None) -> list[dict]:
        return [b for t, b in self.g_bus.bodies("guardian/verdict/") if sensor is None or b["sensor_id"] == sensor]

    def trust(self, sensor: str) -> list[dict]:
        return self.internal(f"brain/internal/trust/{sensor}")

    def guardian_layer(self) -> list[str]:
        return [m["status"] for m in self.internal("brain/internal/layer-status/guardian")]


@pytest.fixture
def grig(tmp_path):
    def make(**kw) -> GuardianRig:
        return GuardianRig(tmp_path, **kw)
    return make


def test_every_guardian_message_passes_the_brain_input_checks(grig):
    rig = grig(scenario="fire", scenario_after_ticks=3)
    rig.connect()
    rig.tick(12)
    assert rig.errors() == []
    assert rig.g_bus.sent[0][0] == "guardian/status" and all(r for *_, r in rig.g_bus.sent)   # all retained
    v = rig.verdicts("water-01")[0]
    assert v["source"] == "guardian" and v["correlation_id"].endswith("water-01-000001")
    assert v["verdict"] == "trusted" and 0 <= v["trust_score"] <= 1
    assert len({b["sensor_id"] for b in rig.verdicts()}) == 44   # 6 types x 6 zones + 4 fire-signature types x 2 zones
    assert rig.trust("water-01")[0]["verdict"] == "trusted"
    assert rig.guardian_layer() == ["online"]
    # the fire scenario (air 12 -> 250 in one tick) is a real event: nothing rejected
    assert {b["verdict"] for b in rig.verdicts()} == {"trusted"}


def test_hmacs_envelope_also_passes(grig):
    rig = grig(guardian_cfg={"envelope": "hmacs"})
    rig.connect()
    rig.tick(2)
    assert rig.errors() == [] and rig.trust("water-01")
    assert "hmacs" in json.loads(rig.g_bus.sent[-1][1])


def test_fake_flood_t5_rejected_within_two_readings_and_isolated(grig):
    """Organizer example T5: the port sensor says 1.8 m while the real level is 0.4 m."""
    rig = grig(faults=[{"mode": "falsify", "tick": 8, "end_tick": 30, "device_id": "water-01", "value": 1.8}])
    rig.connect()
    rig.tick(7)
    assert rig.verdicts("water-01")[-1]["verdict"] == "trusted"
    rig.tick(1)                                           # first fake reading
    last = rig.verdicts("water-01")[-1]
    assert (last["verdict"], last["reason"]) == ("rejected", "implausible_jump")
    assert last["correlation_id"].endswith("water-01-000008")
    q = [b for _, b in rig.g_bus.bodies("guardian/quarantine/water-01")]
    assert q and q[-1]["state"] == "quarantined" and q[-1]["reason"] == "implausible_jump"
    assert rig.internal("brain/internal/quarantine/water-01")[-1]["state"] == "quarantined"
    assert any(e["event"] == "device_quarantined" for _, e in rig.brain_bus.bodies("brain/event"))
    before = len(rig.verdicts("water-01")) - 1
    rig.tick(20)                                          # the forged constant never becomes a new level
    assert {v["verdict"] for v in rig.verdicts("water-01")[before:]} == {"rejected"}
    assert rig.trust("water-01")[-1]["verdict"] == "rejected"
    rig.tick(8)                                           # fault over: trusted again, then released
    assert rig.verdicts("water-01")[-1]["verdict"] == "trusted"
    assert [b for _, b in rig.g_bus.bodies("guardian/quarantine/water-01")][-1]["state"] == "released"
    assert rig.errors() == []


def test_isolation_can_be_turned_off(grig):
    rig = grig(guardian_cfg={"isolate_on_reject": False},
               faults=[{"mode": "falsify", "tick": 4, "end_tick": 10, "device_id": "water-01", "value": 1.8}])
    rig.connect()
    rig.tick(6)
    assert rig.verdicts("water-01")[-1]["verdict"] == "rejected"
    assert rig.g_bus.bodies("guardian/quarantine/") == []


def test_real_flood_stays_trusted(grig):
    rig = grig(scenario="flood", scenario_after_ticks=3)
    rig.connect()
    rig.tick(20)
    assert {v["verdict"] for v in rig.verdicts("water-01")} == {"trusted"}
    assert rig.trust("water-01")[-1]["trust_score"] >= 0.85
    assert rig.g_bus.bodies("guardian/quarantine/") == []


def test_end_of_a_real_flood_stays_trusted(grig):
    """The Twin drops the level back to 0.4 m in one tick when a flood ends: a fall may be faster than a rise."""
    rig = grig(scenario="flood", scenario_after_ticks=2)
    rig.connect()
    rig.tick(15)
    rig.twin.handle("twin/control", seal({"op": "trigger", "scenario": "calm", "zone": "zone-01"}, TWIN_KEY))
    rig.tick(5)
    assert {v["verdict"] for v in rig.verdicts("water-01")} == {"trusted"}


def test_real_flood_with_valve_opened_stays_trusted(grig):
    rig = grig(scenario="flood", scenario_after_ticks=2)
    rig.connect()
    rig.tick(5)
    rig.command("valve-01", "open", cid="brain-000001")
    rig.tick(10)
    assert {v["verdict"] for v in rig.verdicts("water-01")} == {"trusted"}


def test_frozen_sensor_suspicious_then_rejected(grig):
    rig = grig(guardian_cfg={"isolate_on_reject": False},
               faults=[{"mode": "freeze", "tick": 5, "end_tick": 30, "device_id": "air-03"}])
    rig.connect()
    rig.tick(10)
    assert rig.verdicts("air-03")[-1]["verdict"] == "suspicious" and rig.verdicts("air-03")[-1]["reason"] == "frozen"
    assert rig.trust("air-03")[-1]["trust_score"] == 0.5
    rig.tick(5)
    assert (rig.verdicts("air-03")[-1]["verdict"], rig.verdicts("air-03")[-1]["reason"]) == ("rejected", "frozen")
    assert rig.errors() == []


def test_out_of_range_reading_rejects_the_sensor(grig):
    rig = grig(faults=[{"mode": "impossible", "tick": 4, "end_tick": 4, "device_id": "water-02"}])
    rig.connect()
    rig.tick(4)
    assert rig.verdicts("water-02")[-1]["reason"] == "out_of_range"
    assert [e["code"] for e in rig.errors()] == ["E_RANGE"]        # the reading itself, refused by the Brain
    assert rig.trust("water-02")[-1]["verdict"] == "rejected"      # the verdict passed


def test_bad_hmac_and_unknown_sensor_readings_are_ignored(grig):
    rig = grig(faults=[{"mode": "bad_hmac", "tick": 3, "end_tick": 3, "device_id": "water-01"},
                       {"mode": "unknown_id", "tick": 3, "end_tick": 3, "device_id": "water-02"}])
    rig.connect()
    rig.tick(4)
    assert rig.guardian.ignored == 1                  # bad HMAC; the ghost sensor is simply unknown
    assert all(v["sensor_id"] != "ghost-01" for v in rig.verdicts())
    assert sorted(e["code"] for e in rig.errors()) == ["E_AUTH", "E_UNKNOWN_ID"]


def test_duplicate_reading_gives_no_second_verdict(grig):
    rig = grig(guardian_cfg={"refresh_s": 100})
    rig.connect()
    rig.tick(1)
    n = len(rig.verdicts())
    topic, payload, *_ = next(m for m in rig.twin_bus.sent if m[0].endswith("/water-01"))
    rig.guardian.handle(topic, payload)
    assert len(rig.g_bus.sent) == n + 1               # + the status message, nothing new


# --- forced modes (team plan 06, section 5) ----------------------------------------------------


def test_trust_all(grig):
    rig = grig(guardian_cfg={"modes": ["trust_all"]},
               faults=[{"mode": "falsify", "tick": 3, "end_tick": 10, "device_id": "water-01", "value": 1.8}])
    rig.connect()
    rig.tick(5)
    v = rig.verdicts("water-01")[-1]
    assert (v["verdict"], v["reason"], v["trust_score"]) == ("trusted", "trust_all", 0.95)


def test_reject_and_suspicious_by_control_take_effect_at_once(grig):
    rig = grig()
    rig.connect()
    rig.tick(2)
    rig.control({"op": "mode", "modes": ["reject:water-01", "suspicious:air-05"]})
    assert rig.verdicts("water-01")[-1]["verdict"] == "rejected"
    assert rig.verdicts("water-01")[-1]["reason"] == "out_of_range"
    assert (rig.verdicts("air-05")[-1]["verdict"], rig.verdicts("air-05")[-1]["trust_score"]) == ("suspicious", 0.5)
    assert rig.trust("water-01")[-1]["verdict"] == "rejected"
    rig.control({"op": "mode", "mode": "auto"})
    assert rig.verdicts("water-01")[-1]["verdict"] == "trusted"
    assert rig.errors() == []


def test_flip_flop_alternates(grig):
    rig = grig()
    rig.connect()
    rig.control({"op": "mode", "mode": "flip_flop:power-03"})
    rig.tick(4)
    assert [v["verdict"] for v in rig.verdicts("power-03")] == ["rejected", "trusted", "rejected", "trusted"]
    assert rig.errors() == []


def test_quarantine_and_release_any_device(grig):
    rig = grig()
    rig.connect()
    rig.tick(1)
    rig.control({"op": "mode", "modes": ["quarantine:valve-01", "quarantine:unit-ambulance-02"]})
    assert rig.internal("brain/internal/quarantine/valve-01")[-1]["state"] == "quarantined"
    assert rig.internal("brain/internal/quarantine/unit-ambulance-02")[-1]["reason"] == "forced"
    rig.control({"op": "mode", "mode": "release:valve-01"})
    assert rig.internal("brain/internal/quarantine/valve-01")[-1]["state"] == "released"
    assert rig.errors() == []


def test_quarantine_in_config_goes_out_on_connect(grig):
    rig = grig(guardian_cfg={"modes": ["auto", "quarantine:valve-01"]})
    rig.connect()
    assert rig.internal("brain/internal/quarantine/valve-01")[-1]["state"] == "quarantined"


def test_slow_mode_delays_verdicts_by_slow_s(grig):
    rig = grig(guardian_cfg={"modes": ["slow"]},
               faults=[{"mode": "falsify", "tick": 5, "end_tick": 20, "device_id": "water-01", "value": 1.8}])
    rig.connect()
    rig.tick(5)                                       # rejected at tick 5, published 5 s later
    assert all(v["verdict"] == "trusted" for v in rig.verdicts("water-01"))
    rig.tick(4)
    assert all(v["verdict"] == "trusted" for v in rig.verdicts("water-01"))
    rig.tick(1)
    assert rig.verdicts("water-01")[-1]["verdict"] == "rejected"
    assert rig.errors() == []                         # stamped when sent: never stale


def test_offline_mode_then_online(grig):
    rig = grig()
    rig.connect()
    rig.tick(2)
    rig.control({"op": "mode", "mode": "offline"})
    assert rig.guardian_layer()[-1] == "offline"
    n = len(rig.g_bus.sent)
    rig.tick(10)
    assert len(rig.g_bus.sent) == n                   # silent
    rig.control({"op": "mode", "mode": "online"})
    assert rig.guardian_layer()[-1] == "online"
    assert len(rig.verdicts()) > 36
    assert rig.errors() == []


def test_refresh_keeps_guardian_alive_and_silence_marks_it_offline(grig):
    rig = grig()
    rig.connect()
    rig.tick(45)
    assert rig.guardian_layer() == ["online"]
    silent = grig(guardian_cfg={"refresh_s": 1000})
    silent.connect()
    silent.tick(45)
    assert silent.guardian_layer()[-1] == "offline"   # nothing changed, nothing said: the Brain gives up after 30 s


def test_reset_releases_quarantines_and_goes_back_to_config(grig):
    rig = grig()
    rig.connect()
    rig.control({"op": "mode", "modes": ["quarantine:valve-02", "offline"]})
    rig.control({"op": "reset"})
    assert rig.internal("brain/internal/quarantine/valve-02")[-1]["state"] == "released"
    assert rig.guardian_layer()[-1] == "online"
    rig.tick(2)
    assert rig.verdicts("water-01")[-1]["verdict"] == "trusted"
    assert rig.errors() == []


def test_control_with_wrong_key_or_bad_mode_is_ignored(grig):
    rig = grig()
    rig.connect()
    rig.control({"op": "mode", "mode": "offline"}, key=b"wrong")
    rig.control({"op": "mode", "mode": "explode"})
    rig.control({"op": "mode", "mode": "reject:ghost-01"})
    assert rig.guardian.ignored == 3 and not rig.guardian.core.offline


@pytest.mark.parametrize("spec,expected", [
    ("auto", ("auto", None)), ("reject:water-01", ("reject", "water-01")), ("flip_flop", ("flip_flop", None)),
    ("quarantine:valve-01", ("quarantine", "valve-01")),
])
def test_parse_mode(spec, expected):
    assert parse_mode(spec) == expected


@pytest.mark.parametrize("spec", ["", "slow:water-01", "nope", 3])
def test_parse_mode_refuses(spec):
    with pytest.raises(ValueError):
        parse_mode(spec)


def test_core_jump_rebases_on_a_real_noisy_step():
    core = GuardianCore({"checks": {"max_step": {"water_level": 0.5}, "rebase_n": 3}, "isolate_on_reject": False},
                        {"w": {"type": "water_level", "min": 0, "max": 5}})
    def feed(i, v):
        return core.observe({"id": f"r{i}", "sensor_id": "w", "value": v, "seq": i}, float(i))
    feed(1, 0.40)
    assert feed(2, 1.50)[0]["reason"] == "implausible_jump"
    feed(3, 1.52)
    out = feed(4, 1.49)
    assert out[0]["verdict"] == "trusted" and "new level" in out[0]["details"]


def test_core_flatline_at_bound():
    core = GuardianCore({"checks": {"flatline_n": 3, "frozen_suspicious_n": 50, "frozen_rejected_n": 50}},
                        {"s": {"type": "traffic_speed", "min": 0, "max": 200}})
    outs = [core.observe({"id": f"r{i}", "sensor_id": "s", "value": v}, float(i)) for i, v in enumerate([0, 0, 0])]
    assert outs[-1][0]["reason"] == "flatline"


def test_core_forged_noisy_level_stays_rejected_without_rebase():
    """T5: a falsified level carries realistic noise; with rebase_n 0 (the configured default) it is never
    accepted, while the real level coming back is trusted again."""
    core = GuardianCore({"checks": {"max_step": {"water_level": {"rise": 1.2, "fall": 2.5}}, "rebase_n": 0},
                         "isolate_on_reject": False},
                        {"w": {"type": "water_level", "min": 0, "max": 5}})
    def feed(i, v):
        return core.observe({"id": f"r{i}", "sensor_id": "w", "value": v, "seq": i}, float(i))
    feed(1, 0.40)
    published = [m for i in range(2, 30) for m in feed(i, 1.80 + 0.01 * (i % 3)) if "verdict" in m]
    assert published and {m["verdict"] for m in published} == {"rejected"}    # never trusted
    back = [m for m in feed(30, 0.41) if "verdict" in m]
    assert back and back[-1]["verdict"] == "trusted"


def test_configured_guardian_does_not_rebase():
    from src.common import config
    assert config.load("guardian-mock")["checks"]["rebase_n"] == 0
