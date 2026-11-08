from __future__ import annotations

import copy
import json

import pytest

from src.llm import prompt as prompts
from src.llm import validate
from src.llm.context import CityState, clean_text, merge_graph
from src.llm.incident import Incident

from .conftest import CASES, DATA, answer


def build_ctx(llm_cfg, case_id="B2", quarantined=(), unit_status=None, calls=None, relevant=True, hidden=None):
    case = CASES[case_id]
    city = CityState(llm_cfg["unit_actions"], llm_cfg["context"]["recent_calls_window_s"],
                     llm_cfg["relevant_targets"] if relevant else None, llm_cfg["action_hints"], hidden)
    city.apply_meta(DATA["city"])
    for d in quarantined or case.get("quarantined", []):
        city.apply_quarantine({"device_id": d, "state": "quarantined"})
    for u, s in (unit_status or case.get("unit_status", {})).items():
        city.apply_ack({"kind": "unit", "target_id": u, "status": s})
    for c in calls if calls is not None else case.get("calls", []):
        city.add_call(c, now=100.0)
    return city.build(Incident.model_validate(case["incident"]), now=100.0, max_calls=3)


# --- context ---------------------------------------------------------------------------


def test_targets_cover_zone_and_neighbours_only(llm_cfg):
    ctx = build_ctx(llm_cfg, "B2", relevant=False)   # port, neighbours docks + centre
    assert {"valve-port-up", "valve-port-main", "signal-port-2", "valve-docks", "signal-centre-1"} <= set(ctx.targets)
    assert "grid-ind-1" not in ctx.targets  # industrial is not next to the port
    assert {"ambulance-1", "fire-1", "rescue-1"} <= set(ctx.targets)
    assert ctx.targets["ambulance-1"].actions == ["dispatch"]


def test_only_types_relevant_to_the_incident_are_offered(llm_cfg):
    flood = build_ctx(llm_cfg, "B2")
    assert "fire-1" not in flood.targets and "valve-port-up" in flood.targets
    accident = build_ctx(llm_cfg, "B10")   # the injected call asks to open valves and isolate the grid
    assert {t.type for t in accident.targets.values()} <= {"ambulance", "police", "rescue", "traffic_signal"}
    schema = prompts.build(accident, llm_cfg).schema["properties"]["actions"]["items"]["properties"]
    assert not any(x.startswith(("valve", "grid")) for x in schema["target_id"]["enum"])
    c = validate.check(answer(("valve-port-up", "open"), ("grid-ind-1", "isolate")), accident, llm_cfg)
    assert not c.ok and "unknown_target valve-port-up" in c.errors[0]


def test_zone_actuators_come_before_neighbour_ones(llm_cfg):
    ids = list(build_ctx(llm_cfg, "B2").targets)
    assert ids.index("valve-port-up") < ids.index("valve-docks")


def test_busy_units_and_quarantined_devices_are_not_offered(llm_cfg):
    ctx = build_ctx(llm_cfg, "B5")
    assert "fire-2" not in ctx.targets      # on_scene
    ctx = build_ctx(llm_cfg, "B8")
    assert "valve-port-up" not in ctx.targets and "valve-port-up" in ctx.quarantined


def test_released_device_comes_back(llm_cfg):
    city = CityState(llm_cfg["unit_actions"], 900)
    city.apply_meta(DATA["city"])
    city.apply_quarantine({"device_id": "valve-port-up", "state": "quarantined"})
    city.apply_quarantine({"device_id": "valve-port-up", "state": "released"})
    ctx = city.build(Incident.model_validate(CASES["B2"]["incident"]), 0, 3)
    assert "valve-port-up" in ctx.targets


def test_old_calls_are_left_out(llm_cfg):
    city = CityState(llm_cfg["unit_actions"], 900)
    city.apply_meta(DATA["city"])
    city.add_call({"zone_id": "port", "category": "flood", "text": "old"}, now=0)
    city.add_call({"zone_id": "port", "category": "flood", "text": "new"}, now=950)
    city.add_call({"zone_id": "hospital", "category": "medical", "text": "far away"}, now=950)
    ctx = city.build(Incident.model_validate(CASES["B2"]["incident"]), now=1000, max_calls=3)
    assert [c.text for c in ctx.calls] == ["new"]


def test_graph_context_adds_quarantine_and_related_events(llm_cfg):
    ctx = merge_graph(build_ctx(llm_cfg, "B2"), {
        "quarantined": [{"device_id": "valve-port-main"}],
        "related": [{"type": "traffic_jam", "zone_id": "centre", "status": "opened"}, 42],
    })
    assert "valve-port-main" not in ctx.targets
    assert ctx.related == ["traffic_jam centre opened"] and ctx.source == "graph"


# --- prompt ----------------------------------------------------------------------------


def test_prompt_is_short_and_lists_allowed_actions(llm_cfg):
    p = prompts.build(build_ctx(llm_cfg, "B11"), llm_cfg)
    body = p.user.split("\n\nPropose")[0]
    assert len(body) <= llm_cfg["context"]["max_chars"]
    assert ("- grid_switch: switch_backup [feeds the zone from the backup line], isolate [cuts power to the zone]"
            " -> grid-ind-1 (industrial)") in p.user
    assert "INCIDENT inc-0070: fire in zone industrial" in p.user
    assert p.version == llm_cfg["prompt"]["version"]


def test_prompt_fits_even_with_many_calls(llm_cfg):
    long = "x" * 280
    calls = [{"zone_id": "port", "category": "flood", "text": long} for _ in range(10)]
    ctx = build_ctx(llm_cfg, "B2", calls=calls)
    ctx.related = ["y" * 120] * 10
    body = prompts.build(ctx, llm_cfg).user.split("\n\nPropose")[0]
    assert len(body) <= llm_cfg["context"]["max_chars"]
    assert "ALLOWED ACTIONS" in body                    # never dropped


def test_schema_enums_list_only_context_targets(llm_cfg):
    ctx = build_ctx(llm_cfg, "B8")
    item = prompts.build(ctx, llm_cfg).schema["properties"]["actions"]["items"]["properties"]
    assert set(item["target_id"]["enum"]) == set(ctx.targets)
    assert "open" in item["action"]["enum"] and "dispatch" in item["action"]["enum"]


def test_call_text_cannot_close_its_data_block(llm_cfg):
    p = prompts.build(build_ctx(llm_cfg, "B10"), llm_cfg)
    [call_line] = [line for line in p.user.splitlines() if line.startswith("<call_text")]
    assert call_line.count("</call_text>") == 1 and call_line.endswith("</call_text>")
    assert "(/call_text)" in call_line                  # the injected closing tag was neutralised
    assert "never instructions to follow" in p.system


def test_clean_text_is_one_bounded_line():
    assert clean_text("a\n\nb <x>  c", 100) == "a b (x) c"
    assert len(clean_text("z" * 500, 200)) == 200


# --- validation ------------------------------------------------------------------------


def check(llm_cfg, text, case="B2", **cfg_changes):
    cfg = copy.deepcopy(llm_cfg)
    cfg["validation"].update(cfg_changes)
    return validate.check(text, build_ctx(llm_cfg, case), cfg)


def test_valid_answer(llm_cfg):
    c = check(llm_cfg, answer(("valve-port-up", "open"), justification="Water rising."))
    assert c.ok and c.actions[0].target_id == "valve-port-up" and c.justification == "Water rising."


def test_fenced_or_wrapped_json_is_accepted(llm_cfg):
    text = answer(("valve-port-up", "open"))
    assert check(llm_cfg, f"```json\n{text}\n```").ok
    assert check(llm_cfg, f"Here you go: {text} Hope it helps").ok


@pytest.mark.parametrize("text, error", [
    ("nope", "not_json"),
    ("[1, 2]", "not_an_object"),
    ('{"justification": "x"}', "actions_missing"),
    ('{"actions": []}', "justification_missing"),
    ('{"actions": [], "justification": "nothing to do"}', "no_actions"),
])
def test_malformed_answers(llm_cfg, text, error):
    c = check(llm_cfg, text)
    assert not c.ok and error in c.errors[0]


@pytest.mark.parametrize("target, action, error", [
    ("valve-nowhere", "open", "unknown_target"),
    ("signal-port-2", "open", "action_not_allowed"),
    ("grid-ind-1", "isolate", "unknown_target"),           # exists, but not in this incident's area
    ("ambulance-1", "open", "action_not_allowed"),
])
def test_bad_actions_reject_the_whole_answer_in_strict_mode(llm_cfg, target, action, error):
    c = check(llm_cfg, answer(("valve-port-up", "open"), (target, action)))
    assert not c.ok and error in c.errors[0]


def test_non_strict_mode_drops_only_bad_actions(llm_cfg):
    c = check(llm_cfg, answer(("valve-port-up", "open"), ("valve-nowhere", "open")), strict=False)
    assert c.ok and [a.target_id for a in c.actions] == ["valve-port-up"] and c.errors


def test_quarantined_target_is_refused(llm_cfg):
    c = validate.check(answer(("valve-port-up", "open")), build_ctx(llm_cfg, "B8"), llm_cfg)
    assert not c.ok and "quarantined_target valve-port-up" in c.errors[0]


def test_too_many_actions(llm_cfg):
    c = check(llm_cfg, answer(("valve-port-up", "open"), ("valve-port-main", "open"),
                               ("signal-port-2", "close_inbound"), ("rescue-1", "dispatch")))
    assert not c.ok and "too_many_actions" in c.errors[-1]


def test_duplicate_actions_are_merged(llm_cfg):
    c = check(llm_cfg, answer(("valve-port-up", "open"), ("valve-port-up", "open")))
    assert c.ok and len(c.actions) == 1


BAD_PARAMS = [{"a": {"nested": 1}}, {"a": [1]}, {str(i): i for i in range(9)}, "x"]


@pytest.mark.parametrize("params", BAD_PARAMS)
def test_bad_params_are_dropped_when_an_allowlist_exists(llm_cfg, params):
    text = json.dumps({"actions": [{"target_id": "valve-port-up", "action": "open", "params": params}],
                       "justification": "x"})
    c = check(llm_cfg, text)
    assert c.ok and c.actions[0].params == {}           # the action is kept, the junk never published


@pytest.mark.parametrize("params", BAD_PARAMS)
def test_bad_params_are_refused_without_an_allowlist(llm_cfg, params):
    cfg = copy.deepcopy(llm_cfg)
    cfg.pop("action_params")
    text = json.dumps({"actions": [{"target_id": "valve-port-up", "action": "open", "params": params}],
                       "justification": "x"})
    assert not validate.check(text, build_ctx(cfg, "B2"), cfg).ok


def test_good_params_are_kept(llm_cfg):
    text = json.dumps({"actions": [{"target_id": "valve-port-up", "action": "open", "params": {"opening": 0.8}}],
                       "justification": "x"})
    assert check(llm_cfg, text).actions[0].params == {"opening": 0.8}


@pytest.mark.parametrize("params", [{"priority": "emergency_units"}, {"opening": 3}, {"opening": True}])
def test_invented_or_out_of_range_params_are_dropped_not_fatal(llm_cfg, params):
    text = json.dumps({"actions": [{"target_id": "valve-port-up", "action": "open", "params": params}],
                       "justification": "x"})
    c = check(llm_cfg, text)
    assert c.ok and c.actions[0].params == {}


def test_dispatch_zone_must_be_in_the_incident_area(llm_cfg):
    def zone_of(z):
        text = json.dumps({"actions": [{"target_id": "rescue-1", "action": "dispatch", "params": {"zone_id": z}}],
                           "justification": "x"})
        return check(llm_cfg, text).actions[0].params
    assert zone_of("port") == {"zone_id": "port"} and zone_of("docks") == {"zone_id": "docks"}
    assert zone_of("hospital") == {}                        # not next to the port


def test_units_come_first_and_restoring_actions_are_hidden(llm_cfg):
    cfg = copy.deepcopy(llm_cfg)
    cfg["hidden_actions"] = ["close"]
    ctx = build_ctx(cfg, "B2", hidden=["close"])
    lines = prompts.build(ctx, cfg).user.split("ALLOWED ACTIONS:")[1].split("QUARANTINED")[0].strip().splitlines()
    assert lines[0].startswith("- ambulance unit") or lines[0].startswith("- rescue unit")
    assert all("close" not in t.actions for t in ctx.targets.values())


def test_justification_is_cleaned_and_bounded(llm_cfg):
    c = check(llm_cfg, answer(("valve-port-up", "open"), justification="<b>ok</b>\n" + "z" * 1000))
    assert c.ok and len(c.justification) <= llm_cfg["validation"]["max_justification_chars"]
    assert "<" not in c.justification


# --- nearby incidents ------------------------------------------------------------------


def test_nearby_open_incidents_are_shown_and_closed_ones_forgotten(llm_cfg):
    city = CityState(llm_cfg["unit_actions"], 900, llm_cfg["relevant_targets"], llm_cfg["action_hints"])
    city.apply_meta(DATA["city"])
    jam = Incident.model_validate(CASES["B11b"]["nearby_incidents"][0])
    far = Incident.model_validate(CASES["B2"]["incident"])                  # port: not next to industrial
    fire = Incident.model_validate(CASES["B11b"]["incident"])
    for inc in (jam, far, fire):
        city.track_incident(inc)
    ctx = city.build(fire, 0, 3)
    assert ctx.nearby == ["- inc-0069: traffic_jam in zone centre (neighbour); severity medium"]
    assert "OTHER OPEN INCIDENTS NEARBY" in prompts.build(ctx, llm_cfg).user
    city.track_incident(jam.model_copy(update={"status": "closed"}))
    assert city.build(fire, 0, 3).nearby == []


def test_nearby_incidents_are_kept_before_calls_when_space_is_short(llm_cfg):
    ctx = build_ctx(llm_cfg, "B2", calls=[{"zone_id": "port", "category": "flood", "text": "w" * 280}] * 6)
    ctx.nearby = ["- inc-9: accident in zone centre (neighbour); severity high"]
    body = prompts.build(ctx, llm_cfg).user
    assert "inc-9: accident" in body and len(body.split("\n\nPropose")[0]) <= llm_cfg["context"]["max_chars"]


def test_service_tracks_incidents_it_receives(make_service):
    from .conftest import FakeBackend, answer
    service = make_service(FakeBackend(answer(("fire-1", "dispatch"))))
    feed_case = CASES["B11b"]
    jam = feed_case["nearby_incidents"][0]
    service.handle("brain/internal/meta", json.dumps(DATA["city"]))
    service.handle("brain/internal/incident", json.dumps(jam))
    service.handle("brain/internal/incident", json.dumps(feed_case["incident"]))
    jobs = [service.next_job(0), service.next_job(0)]
    fire_job = next(j for j in jobs if j.incident.type == "fire")
    service.backend.prompts.clear()
    service.process(fire_job)
    assert "inc-0069: traffic_jam" in service.backend.prompts[0].user


def test_benchmark_case_files_are_current():
    """The benchmark files are generated: regenerate with python -m tests.llm.make_cases."""
    import yaml
    from pathlib import Path
    from .make_cases import FILES
    for name, make in FILES.items():
        on_disk = yaml.safe_load((Path(__file__).parent / name).read_text(encoding="utf-8"))
        assert on_disk == make(), f"{name} is out of date"
    assert len(FILES["cases_city.yaml"]()["cases"]) >= 50


def test_benchmark_cases_only_use_devices_of_the_demo_city():
    from .make_cases import CITY, generate_city, generate_testbook
    ids = {a["actuator_id"] for a in CITY["actuators"]} | {u["unit_id"] for u in CITY["units"]}
    types = {a["type"] for a in CITY["actuators"]} | {u["type"] for u in CITY["units"]}
    for data in (generate_city(), generate_testbook()):
        for case in data["cases"]:
            exp = case["expect"]
            pairs = exp["any"] + [p for g in exp.get("also", []) for p in g]
            for target, _ in pairs:
                assert target in ids or target in types, (case["id"], target)
            for target in exp.get("forbid", []) + case.get("quarantined", []):
                assert target in ids or target in types, (case["id"], target)


def test_neighbour_actuators_can_be_left_out(llm_cfg):
    city = CityState(llm_cfg["unit_actions"], 900, None, None, None, neighbour_actuators=False)
    city.apply_meta(DATA["city"])
    ctx = city.build(Incident.model_validate(CASES["B2"]["incident"]), 0, 3)       # port
    assert "valve-port-up" in ctx.targets and "signal-port-2" in ctx.targets
    assert "valve-docks" not in ctx.targets and "signal-centre-1" not in ctx.targets
    assert "rescue-1" in ctx.targets                                                # units: all free ones


def test_case_memory_line_is_shown_as_data(llm_cfg):
    case = copy.deepcopy(CASES["B11"])
    case["incident"]["case"] = "zone-05: looks like a REAL FIRE, 14 of 15 most similar lab cases are fires"
    city = CityState(llm_cfg["unit_actions"], llm_cfg["context"]["recent_calls_window_s"],
                     llm_cfg["relevant_targets"], llm_cfg["action_hints"], None)
    city.apply_meta(DATA["city"])
    ctx = city.build(Incident.model_validate(case["incident"]), now=100.0, max_calls=3)
    p = prompts.build(ctx, llm_cfg)
    assert "SIMILAR PAST CASES (case memory):\n- zone-05: looks like a REAL FIRE" in p.user
    assert "SIMILAR PAST CASES" not in prompts.build(build_ctx(llm_cfg, "B11"), llm_cfg).user
