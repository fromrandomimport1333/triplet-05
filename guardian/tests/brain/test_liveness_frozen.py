"""v1.3: verdict refresh every VERDICT_REFRESH_S while the Twin is silent (30 s liveness rule)
and the frozen-sensor rule by count of identical readings (FROZEN_COUNT)."""
from collections import Counter

import jsonschema

from tests.brain.test_contract import schema


# ---------------------------------------------------------------- liveness refresh
def test_refresh_when_twin_silent(bstack):
    s = bstack
    for _ in range(3):
        s.tick()
    n0 = len(s.verdicts())
    s.advance(19)
    assert len(s.verdicts()) == n0                    # not yet
    s.advance(2)                                      # > 20 s without a verdict
    new = s.verdicts()[n0:]
    assert len(new) == 36                             # every known sensor refreshed once
    for m in new:
        b = m["body"]
        jsonschema.validate(b, schema("external-verdict"))
        assert m["retain"] and m["verified"] and b["details"].startswith("refresh")
    old = {m["body"]["sensor_id"]: m["body"] for m in s.verdicts()[n0 - 36:n0]}
    for m in new:
        o = old[m["body"]["sensor_id"]]
        assert m["body"]["id"] != o["id"] and m["body"]["timestamp"] != o["timestamp"]
        assert (m["body"]["verdict"], m["body"]["reason"], m["body"]["correlation_id"]) == \
               (o["verdict"], o["reason"], o["correlation_id"])


def test_refresh_keeps_gap_below_30s_for_two_minutes(bstack):
    s = bstack
    s.tick()
    times = []
    for _ in range(120):
        before = len(s.verdicts())
        s.advance(1)
        if len(s.verdicts()) > before:
            times.append(s.twin.t)
    gaps = [b - a for a, b in zip([1.0] + times, times)]
    assert times and max(gaps) < 30


def test_no_refresh_while_readings_flow(bstack):
    s = bstack
    for _ in range(60):
        s.tick()
    assert not any(m["body"]["details"].startswith("refresh") for m in s.verdicts())


def test_refresh_never_resends_bad_hmac_and_can_be_disabled(make_bstack):
    s = make_bstack()
    s.tick()
    s.send_all(s.twin.bad("bad_hmac"))
    s.advance(25)
    p3 = [m["body"]["reason"] for m in s.verdicts("power-03")]
    assert p3[-1] == "consistent"                     # the refreshed one is the real state
    off = make_bstack(verdict_refresh_s=0)
    off.tick()
    n = len(off.verdicts())
    off.advance(60)
    assert len(off.verdicts()) == n


def test_nothing_to_refresh_before_any_reading(bstack):
    bstack.advance(60)
    assert bstack.verdicts() == []


# ---------------------------------------------------------------- frozen sensor
def test_frozen_sensor_suspicious_within_count(bstack):
    s = bstack
    for _ in range(40):
        s.tick("calm", freeze=("water-03", 10))
    w = [m["body"] for m in s.verdicts("water-03")]
    frozen = [i for i, b in enumerate(w) if b["reason"] == "frozen"]
    assert frozen and frozen[0] + 1 == 10 + 8 - 1     # 8th identical reading (t=10 is the 1st)
    assert all(w[i]["verdict"] == "suspicious" for i in frozen)
    assert s.quarantines() == []                      # suspicious never quarantines
    others = Counter(m["body"]["reason"] for m in s.verdicts() if m["body"]["sensor_id"] != "water-03")
    assert set(others) == {"consistent"}


def test_frozen_clears_when_values_move_again(bstack):
    s = bstack
    for _ in range(25):
        s.tick("calm", freeze=("air-02", 5))
    s.twin.frozen.clear()
    for _ in range(3):
        s.tick()
    assert s.verdicts("air-02")[-1]["body"]["reason"] == "consistent"


def test_no_frozen_false_positive_on_real_scenarios(bstack):
    s = bstack
    for sc, n in (("rush", 60), ("flood", 60), ("fire", 30)):
        for _ in range(n):
            s.tick(sc)
    assert not [m for m in s.verdicts() if m["body"]["reason"] == "frozen"]


def test_frozen_count_off_keeps_native_window(make_bstack):
    s = make_bstack(frozen_count=0)
    for _ in range(40):
        s.tick("calm", freeze=("water-03", 10))
    assert not [m for m in s.verdicts("water-03") if m["body"]["reason"] == "frozen"]
