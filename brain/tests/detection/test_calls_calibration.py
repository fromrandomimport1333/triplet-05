"""Flood calls calibrated on NYC 311 (Ida): an hour long, full at two callers; other calls unchanged."""

from __future__ import annotations

import importlib.util

from src.common import config


def test_flood_calls_count_for_an_hour_and_two_are_full(make_city, bus, det_cfg):
    cal = det_cfg["calls"]["by_category"]["flood"]
    city = make_city()
    city.trust_all()
    city.step(30)
    city.call("zone-02", "flood", "water in the street")
    city.step(int(cal["window_s"]) - 120)              # 58 min later: the first call still counts ...
    city.call("zone-02", "flood", "the basement is flooding")
    city.step(3)
    src = city.s.assess("zone-02", "flood", city.clock.t, False, (False, None)).sources
    calls = [s for s in src if s.kind == "call"]
    assert len(calls) == 1 and "2 flood calls in 60 min" in calls[0].desc        # one source, two callers
    assert calls[0].p == min(det_cfg["calls"]["cap"], det_cfg["incidents"]["flood"]["calls"]["flood"])


def test_fire_calls_keep_the_short_window(make_city, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(30)
    city.call("zone-05", "fire", "smoke")
    city.step(int(det_cfg["calls"]["window_s"]) + 5)
    assert not [s for s in city.s.assess("zone-05", "fire", city.clock.t, False, (False, None)).sources
                if s.kind == "call"]


def test_ten_flood_calls_are_still_one_capped_source(make_city, det_cfg):
    city = make_city()
    city.trust_all()
    city.step(30)
    for _ in range(10):
        city.call("zone-02", "flood", "flood!")
        city.step(30)
    calls = [s for s in city.s.assess("zone-02", "flood", city.clock.t, False, (False, None)).sources
             if s.kind == "call"]
    assert len(calls) == 1 and calls[0].p <= det_cfg["calls"]["cap"]


def test_calibration_tool_reproduces_the_config():
    spec = importlib.util.spec_from_file_location("calibrate_calls", config.resolve("tools/calibrate_calls.py"))
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    rows = cc.load(cc.DEFAULT_CSV)
    night = cc.measure(cc.by_area(rows, "storm_night"))
    assert night["areas"] == 536 and round(night["median_gap_min"]) == 19
    assert night["second"][60] >= 0.5 > night["second"][30]                       # -> a 60 min window
    flood = config.load("detection")["calls"]["by_category"]["flood"]
    assert flood == {"window_s": 3600, "full_count": 2}
