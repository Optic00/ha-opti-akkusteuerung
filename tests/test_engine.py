"""Exercise the real pure engine, including time, persistence and action effects."""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

# Import by path: proves that this module works without importing HA or the
# integration lifecycle. It also keeps these tests runnable with Jinja2 only.
_SPEC = importlib.util.spec_from_file_location(
    "opti_pure_engine", Path(__file__).parents[1] / "custom_components/opti_akku/engine.py"
)
_ENGINE_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _ENGINE_MODULE
_SPEC.loader.exec_module(_ENGINE_MODULE)
StrategyEngine = _ENGINE_MODULE.StrategyEngine
load_resources = _ENGINE_MODULE.load_resources

TZ = ZoneInfo("Europe/Berlin")
NOW = dt.datetime(2026, 1, 15, 12, 0, tzinfo=TZ)
MODE = "input_select.akkusteuerung_modus"
MASTER = "input_boolean.akku_opti_automatik"
SOC = "sensor.opti_soc"
CAPACITY = "sensor.opti_battery_capacity_kwh"
TEMP = "sensor.opti_battery_temp"
TARGET = "sensor.opti_target_soc"
DECKEL = "binary_sensor.opti_ladedeckel_aktiv"
POWER = "sensor.opti_charge_power_w"
MINUTES = "counter.opti_balancing_done_minuten"
DAYS = "counter.tage_seit_akku100"
DONE_AT = "input_datetime.opti_balancing_letzter_abschluss"
DONE_VALID = "input_boolean.opti_balancing_abschluss_gueltig"


def measurements(**overrides):
    states = {
        MASTER: "on", SOC: 50, CAPACITY: 10, TEMP: 25,
        "sensor.opti_house_consumption_w": 400,
        "sensor.opti_forecast_today_kwh": 20,
        "sensor.opti_forecast_tomorrow_kwh": 20,
        "sensor.opti_forecast_remaining_today_kwh": 20,
        "sensor.opti_grid_export_w": 0,
        "sensor.opti_grid_import_w": 0,
        "sensor.opti_battery_power_w": 0,
        "sensor.opti_pv_power_w": 1000,
        "sun.sun": "above_horizon",
    }
    states.update(overrides)
    return states


def solar_attrs(now=NOW):
    return {"sun.sun": {
        "next_setting": now.replace(hour=18).isoformat(),
        "next_rising": (now + dt.timedelta(days=1)).replace(hour=8).isoformat(),
    }}


def peak_maxsoc_inputs(soc, **overrides):
    states = measurements(**{
        SOC: soc,
        CAPACITY: 1,
        MODE: "Akku nur Laden",
        "input_number.maxsoc": 95,
        "sensor.opti_house_consumption_w": 5000,
        "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_price_current_ct_kwh": 10,
        "sun.sun": "below_horizon",
        **overrides,
    })
    attributes = solar_attrs()
    attributes["sensor.opti_price_series"] = {
        "today": [10] * 18 + [50] * 6,
        "tomorrow": [10] * 24,
    }
    return states, attributes


def evaluate(engine=None, states=None, attributes=None, now=NOW):
    return (engine or StrategyEngine()).evaluate(
        measurements() if states is None else states,
        solar_attrs(now) if attributes is None else attributes,
        now,
    )


def test_real_pipeline_has_no_template_errors_and_sources_are_not_mutated():
    states, attrs = measurements(), solar_attrs()
    before = copy.deepcopy((states, attrs))
    result = evaluate(states=states, attributes=attrs)
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    assert result.mode == "Akku Dynamisch"
    assert result.reason == "dyn bis Ziel (tag)"
    assert float(result.states[POWER]) == 2000
    assert float(result.states[TARGET]) == 70
    assert (states, attrs) == before


def test_first_start_defaults_to_disabled_automation():
    states = measurements()
    states.pop(MASTER)
    result = evaluate(states=states)
    assert result.mode == "Akku Pause"
    assert "ausgeschaltet" in result.reason
    assert result.states["input_boolean.opti_balancing_netzladen"] == "off"


def test_master_off_overrides_valid_grid_charging_opportunity():
    result = evaluate(states=measurements(**{
        MASTER: "off", "sensor.opti_price_current_ct_kwh": -10,
        "sensor.opti_forecast_remaining_today_kwh": 0,
    }))
    assert result.mode == "Akku Pause"


@pytest.mark.parametrize("entity,bad", [
    (SOC, "unknown"), (SOC, "unavailable"), (SOC, None), (SOC, "invalid"),
    (SOC, -1), (SOC, 101), (SOC, float("nan")), (SOC, float("inf")),
    (CAPACITY, "unavailable"), (CAPACITY, 0), (CAPACITY, -1),
    (CAPACITY, "nan"), (CAPACITY, "-inf"),
])
def test_core_failure_pauses_immediately(entity, bad):
    engine = StrategyEngine()
    evaluate(engine, measurements(**{MODE: "Akku Netzladen"}))
    result = evaluate(engine, measurements(**{entity: bad}), now=NOW + dt.timedelta(seconds=1))
    assert result.mode == "Akku Pause"
    assert result.attributes["sensor.opti_engine_diagnostics"]["core_valid"] is False


def test_omitted_source_does_not_reuse_persisted_value_and_recovers():
    engine = StrategyEngine()
    evaluate(engine)
    states = measurements()
    states.pop(SOC)
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=30)).mode == "Akku Pause"
    assert evaluate(engine, now=NOW + dt.timedelta(seconds=60)).mode == "Akku Dynamisch"


def test_minsoc_precedes_forecast_rules_but_not_grid_charging():
    states = {SOC: 5, "sensor.opti_forecast_remaining_today_kwh": 0}
    result = evaluate(states=measurements(**states))
    assert result.mode == "Akku nur Laden"
    assert result.reason.startswith("MinSOC")
    # Issue #154: grid charging never discharges, so it may run at the floor.
    result = evaluate(states=measurements(**states, **{"sensor.opti_price_current_ct_kwh": -10}))
    assert result.mode == "Akku Netzladen"
    assert result.decision_id == "negative_price"


@pytest.mark.parametrize("minimum", [0, 5, 10])
@pytest.mark.parametrize("daylight", ["above_horizon", "below_horizon"])
@pytest.mark.parametrize("as_text", [False, True])
def test_minsoc_stops_discharge_at_the_exact_configured_floor(minimum, daylight, as_text):
    engine = StrategyEngine()
    states = measurements(**{
        SOC: minimum + 1,
        MODE: "Akku Dynamisch",
        "input_number.minsoc": str(minimum) if as_text else minimum,
        "sun.sun": daylight,
    })
    evaluate(engine, states=states)
    states[SOC] = str(minimum) if as_text else minimum
    result = evaluate(engine, states=states, now=NOW + dt.timedelta(seconds=30))
    assert result.mode == "Akku nur Laden"
    assert result.reason.startswith("MinSOC")
    assert result.decision_id == "minimum_soc"
    assert result.states["sensor.opti_strategie_vorschau"] == "Akku nur Laden"


def test_minsoc_protection_releases_when_soc_recovers_above_floor():
    engine = StrategyEngine()
    states = measurements(**{SOC: 5, "input_number.minsoc": 5})
    assert evaluate(engine, states=states).mode == "Akku nur Laden"
    states[SOC] = 8
    result = evaluate(engine, states=states, now=NOW + dt.timedelta(seconds=30))
    assert result.mode == "Akku Dynamisch"
    assert result.reason == "dyn bis Ziel (tag)"


@pytest.mark.parametrize("temperature", [-10, -5, 50, 55])
def test_temperature_cutoffs_survive_complete_pipeline(temperature):
    result = evaluate(states=measurements(**{TEMP: temperature}))
    assert float(result.states[POWER]) == 0


@pytest.mark.parametrize("temperature,expected", [(25, 2000), (45, 1000), (0, 500), (-4, 500)])
def test_temperature_derating(temperature, expected):
    result = evaluate(states=measurements(**{TEMP: temperature}))
    assert float(result.states[POWER]) == expected


def test_charge_power_respects_exact_configured_limit():
    result = evaluate(states=measurements(**{
        "input_number.akkusteuerung_max_ladestaerke": 1501,
    }))
    assert float(result.states[POWER]) == 1501


@pytest.mark.parametrize("forecast", [0, 5, 20, 80])
def test_manual_charge_limit_replaces_forecast_pacing(forecast):
    states = measurements(**{
        "input_boolean.opti_manuelle_ladegrenze": "on",
        "input_number.akkusteuerung_max_ladestaerke": 4001,
        "sensor.opti_forecast_remaining_today_kwh": forecast,
    })
    assert float(evaluate(states=states).states[POWER]) == 4001


@pytest.mark.parametrize("temperature,expected", [(-5, 0), (50, 0), (-4, 1000), (0, 1000), (45, 2000), (25, 4000)])
def test_manual_charge_limit_keeps_temperature_protection(temperature, expected):
    states = measurements(**{
        TEMP: temperature, "input_boolean.opti_manuelle_ladegrenze": "on",
        "input_number.akkusteuerung_max_ladestaerke": 4000,
    })
    assert float(evaluate(states=states).states[POWER]) == expected


@pytest.mark.parametrize("soc,limit,expected", [(96, 4000, 4000), (97, 4000, 500), (99, 4000, 500), (97, 300, 300), (50, 0, 0)])
def test_manual_charge_limit_keeps_upper_soc_taper_and_explicit_ceiling(soc, limit, expected):
    states = measurements(**{
        SOC: soc, "input_boolean.opti_manuelle_ladegrenze": "on",
        "input_number.akkusteuerung_max_ladestaerke": limit,
    })
    assert float(evaluate(states=states).states[POWER]) == expected


def test_manual_charge_limit_off_matches_original_default():
    default = evaluate()
    explicitly_off = evaluate(states=measurements(**{"input_boolean.opti_manuelle_ladegrenze": "off"}))
    assert float(default.states[POWER]) == float(explicitly_off.states[POWER]) == 2000


def charge_power_branch_end(result):
    branch = result.attributes[POWER]["branch"]
    return float(branch.rsplit("→ Ergebnis", 1)[1].removesuffix("W").strip())


@pytest.mark.parametrize("soc", [10, 50, 80, 92, 96, 98])
@pytest.mark.parametrize("temperature", [-5, 0, 25, 45, 50, None])
@pytest.mark.parametrize("forecast", [0, 5, 80])
@pytest.mark.parametrize("limit,manual", [(3000, "off"), (700, "off"), (4000, "on")])
@pytest.mark.parametrize("balancing", [False, True])
def test_charge_power_branch_ends_with_the_state(soc, temperature, forecast, limit, manual, balancing):
    overrides = {
        SOC: soc, TEMP: temperature,
        "sensor.opti_forecast_remaining_today_kwh": forecast,
        "input_number.akkusteuerung_max_ladestaerke": limit,
        "input_boolean.opti_manuelle_ladegrenze": manual,
    }
    if temperature is None:
        overrides[TEMP] = "unavailable"
    if balancing:
        overrides.update({DAYS: 20, "input_number.opti_balancing_intervall_tage": 14,
                          "input_boolean.opti_prognose_netzladen": "off"})
    result = evaluate(states=measurements(**overrides))
    if temperature is None:
        assert result.states[POWER] == "unavailable"
        assert "branch" not in result.attributes[POWER]
        return
    assert charge_power_branch_end(result) == float(result.states[POWER])


def test_charge_power_branch_names_the_binding_steps():
    paced = evaluate(states=measurements(**{SOC: 80, TEMP: 45}))
    assert "SoC 80 %" in paced.attributes[POWER]["branch"]
    assert "Akkutemperatur 45" in paced.attributes[POWER]["branch"]

    capped = evaluate(states=measurements(**{"input_number.akkusteuerung_max_ladestaerke": 700}))
    assert "Obergrenze Max. Ladeleistung 700 W" in capped.attributes[POWER]["branch"]

    balancing = evaluate(states=measurements(**{
        SOC: 97, DAYS: 20, "input_number.opti_balancing_intervall_tage": 14,
        "input_boolean.opti_prognose_netzladen": "off",
    }))
    assert "Balancing ab 96 % SoC" in balancing.attributes[POWER]["branch"]

    cold = evaluate(states=measurements(**{TEMP: -6}))
    assert cold.attributes[POWER]["branch"].startswith("Akkutemperatur -6")


def test_target_hysteresis_persists_and_attributes_use_same_old_snapshot():
    engine = StrategyEngine()
    states = measurements(**{"sensor.opti_house_consumption_w": 0})
    states["sensor.opti_forecast_remaining_today_kwh"] = 10
    first = evaluate(engine, states)
    assert float(first.states[TARGET]) == 80
    assert first.attributes[TARGET]["level"] == 2
    states["sensor.opti_forecast_remaining_today_kwh"] = 8.5
    second = evaluate(engine, states, now=NOW + dt.timedelta(seconds=30))
    assert float(second.states[TARGET]) == 80
    assert second.attributes[TARGET]["level"] == 2
    assert "gehalten" in second.attributes[TARGET]["branch"]
    snapshot = json.loads(json.dumps(engine.snapshot(), allow_nan=False))
    restored = StrategyEngine()
    restored.restore(snapshot)
    third = evaluate(restored, states, now=NOW + dt.timedelta(minutes=20))
    assert float(third.states[TARGET]) == 80
    states["sensor.opti_forecast_remaining_today_kwh"] = 7
    fourth = evaluate(restored, states, now=NOW + dt.timedelta(minutes=21))
    assert float(fourth.states[TARGET]) == 90
    assert fourth.attributes[TARGET]["level"] == 1


def test_target_missing_forecast_preserves_hysteresis_memory():
    engine = StrategyEngine()
    states = measurements(**{"sensor.opti_house_consumption_w": 0, "sensor.opti_forecast_remaining_today_kwh": 10})
    evaluate(engine, states)
    states["sensor.opti_forecast_remaining_today_kwh"] = "unavailable"
    missing = evaluate(engine, states, now=NOW + dt.timedelta(seconds=30))
    assert missing.states[TARGET] == "unavailable"
    assert missing.attributes[TARGET]["level"] == 2
    states["sensor.opti_forecast_remaining_today_kwh"] = 8.5
    assert float(evaluate(engine, states, now=NOW + dt.timedelta(minutes=1)).states[TARGET]) == 80


def test_ready_remaining_day_profile_prevents_short_load_from_becoming_all_day_load():
    states = measurements(**{
        SOC: 50,
        "sensor.opti_forecast_remaining_today_kwh": 10,
        "sensor.opti_house_consumption_w": 3200,
        "sensor.opti_house_consumption_60min_w": 1800,
        "sensor.opti_forecast_remaining_load_profile_kwh": 3,
    })
    profiled = evaluate(states=states)
    assert profiled.states["sensor.opti_forecast_score"] == "10"
    assert profiled.attributes["sensor.opti_forecast_score"]["projected_load_kwh"] == 3
    assert profiled.attributes["sensor.opti_forecast_score"]["load_source"] == "online_profile"

    states["sensor.opti_forecast_remaining_load_profile_kwh"] = "unavailable"
    legacy = evaluate(states=states)
    assert legacy.states["sensor.opti_forecast_score"] == "0"
    assert legacy.attributes["sensor.opti_forecast_score"]["load_source"] == "legacy_60min_extrapolation"


def test_persistent_extra_base_load_is_included_by_profile_input():
    states = measurements(**{
        SOC: 50,
        "sensor.opti_forecast_remaining_today_kwh": 10,
        "sensor.opti_house_consumption_60min_w": 1800,
        "sensor.opti_forecast_remaining_load_profile_kwh": 7,
    })
    result = evaluate(states=states)
    assert result.states["sensor.opti_forecast_score"] == "6"
    assert result.attributes["sensor.opti_forecast_score"]["pv_surplus_kwh"] == 3


@pytest.mark.parametrize("profile_kwh,expected_load,expected_source", [
    ("unavailable", 2.1, "ev_adjusted_60min_extrapolation"),
    (3, 3, "online_profile"),
])
def test_day_score_and_surplus_attributes_use_ev_adjusted_load(profile_kwh, expected_load, expected_source):
    states = measurements(**{
        SOC: 50, "sensor.opti_forecast_remaining_today_kwh": 10,
        "sensor.opti_house_consumption_w": 2200,
        "sensor.opti_house_consumption_60min_w": 2200,
        "sensor.opti_forecast_house_60min_w": 350,
        "input_boolean.opti_score_house_includes_ev": "on",
        "sensor.opti_forecast_remaining_load_profile_kwh": profile_kwh,
    })
    result = evaluate(states=states)
    attrs = result.attributes["sensor.opti_forecast_score"]
    assert result.states["sensor.opti_forecast_score"] == "10"
    assert attrs["projected_load_kwh"] == expected_load
    assert attrs["pv_surplus_kwh"] == pytest.approx(10 - expected_load)
    assert attrs["ueberschuss_ueber_voll_kwh"] == pytest.approx(5 - expected_load)
    assert attrs["load_source"] == expected_source


def test_invalid_ev_score_source_uses_raw_house_even_with_stale_profile():
    states = measurements(**{
        SOC: 50, "sensor.opti_forecast_remaining_today_kwh": 10,
        "sensor.opti_house_consumption_w": 2200,
        "sensor.opti_house_consumption_60min_w": 1800,
        "sensor.opti_forecast_house_60min_w": "unavailable",
        "input_boolean.opti_score_house_includes_ev": "on",
        "sensor.opti_forecast_remaining_load_profile_kwh": 3,
    })
    result = evaluate(states=states)
    attrs = result.attributes["sensor.opti_forecast_score"]
    assert result.states["sensor.opti_forecast_score"] == "0"
    assert attrs["projected_load_kwh"] == pytest.approx(10.8)
    assert attrs["pv_surplus_kwh"] == 0
    assert attrs["ueberschuss_ueber_voll_kwh"] == 0
    assert attrs["load_source"] == "raw_60min_ev_fallback"
    states["sensor.opti_forecast_tomorrow_kwh"] = 5
    low_pv = evaluate(states=states)
    assert low_pv.states["sensor.opti_forecast_score_tomorrow"] == "1"
    assert low_pv.states["sensor.opti_forecast_score_sonnentag"] == "1"

    states["sensor.opti_house_consumption_60min_w"] = "unavailable"
    current = evaluate(states=states)
    assert current.attributes["sensor.opti_forecast_score"]["projected_load_kwh"] == pytest.approx(13.2)
    assert current.attributes["sensor.opti_forecast_score"]["load_source"] == "current_house_ev_fallback"
    states["sensor.opti_house_consumption_w"] = "unavailable"
    missing = evaluate(states=states)
    for entity in ("sensor.opti_forecast_score", "sensor.opti_forecast_score_tomorrow",
                   "sensor.opti_forecast_score_sonnentag"):
        assert missing.states[entity] == "unavailable"


def test_day_score_opt_out_keeps_raw_load_and_existing_profile_priority():
    states = measurements(**{
        SOC: 50, "sensor.opti_forecast_remaining_today_kwh": 10,
        "sensor.opti_house_consumption_w": 2200,
        "sensor.opti_house_consumption_60min_w": 1800,
        "sensor.opti_forecast_house_60min_w": 350,
        "input_boolean.opti_score_house_includes_ev": "off",
        "sensor.opti_forecast_remaining_load_profile_kwh": "unavailable",
    })
    raw = evaluate(states=states)
    assert raw.attributes["sensor.opti_forecast_score"]["projected_load_kwh"] == pytest.approx(10.8)
    assert raw.attributes["sensor.opti_forecast_score"]["load_source"] == "legacy_60min_extrapolation"
    states["sensor.opti_forecast_remaining_load_profile_kwh"] = 3
    profiled = evaluate(states=states)
    assert profiled.attributes["sensor.opti_forecast_score"]["projected_load_kwh"] == 3


def test_winter_soc15_keeps_forecast_reserve_after_ev_power_becomes_invalid():
    states = measurements(**{
        SOC: 15, "input_number.minsoc": 5,
        "input_boolean.opti_prognose_netzladen": "on",
        "binary_sensor.opti_winter_charging_allowed": "on",
        "sensor.opti_price_current_ct_kwh": 20,
        "sensor.opti_forecast_today_kwh": 1,
        "sensor.opti_forecast_tomorrow_kwh": 1,
        "sensor.opti_forecast_remaining_today_kwh": 1,
        "sensor.opti_house_consumption_w": 2200,
        "sensor.opti_house_consumption_60min_w": 2200,
        "sensor.opti_forecast_house_60min_w": 350,
        "input_boolean.opti_score_house_includes_ev": "on",
    })
    attrs = solar_attrs()
    attrs["sensor.opti_price_series"] = {"today": [20] * 24, "tomorrow": [20] * 24}
    valid = evaluate(states=states, attributes=attrs)
    assert valid.decision_id == "reserve_low"
    states["sensor.opti_forecast_house_60min_w"] = "unavailable"
    invalid = evaluate(states=states, attributes=attrs)
    assert invalid.states["sensor.opti_forecast_score"] == "0"
    assert invalid.states["sensor.opti_forecast_score_tomorrow"] == "0"
    assert invalid.states["sensor.opti_forecast_score_sonnentag"] == "0"
    assert invalid.decision_id == "reserve_low"
    assert invalid.mode == "Akku nur Laden"


def test_maxsoc_latch_requires_real_entry_and_preserves_sensor_gap():
    engine = StrategyEngine()
    for minute, (soc, expected) in enumerate([(93, "off"), (95, "on"), (94, "on"), (92, "on"), ("unavailable", "on"), (91.9, "off"), (93, "off")]):
        result = evaluate(engine, measurements(**{SOC: soc}), now=NOW + dt.timedelta(minutes=minute))
        assert result.states[DECKEL] == expected
    evaluate(engine, measurements(**{SOC: 95}), now=NOW + dt.timedelta(minutes=10))
    changed_limit = evaluate(engine, measurements(**{SOC: 98, "input_number.maxsoc": 100}), now=NOW + dt.timedelta(minutes=11))
    assert changed_limit.states[DECKEL] == "off"
    assert changed_limit.attributes[DECKEL]["maxsoc"] == 100


def test_surplus_delay_is_elapsed_time_not_number_of_evaluations():
    engine = StrategyEngine()
    entity = "binary_sensor.opti_ueberschuss_70_aktiv"
    states = measurements(**{
        "input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": 5000,
        "sensor.opti_grid_export_w": 5001,
    })
    for second in [0, 1, 1, 10, 29]:
        assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=second)).states[entity] == "off"
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=30)).states[entity] == "on"
    # A 1 kW Schmitt band keeps the signal on without restarting a timer.
    states["sensor.opti_grid_export_w"] = 4001
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=31)).states[entity] == "on"
    states["sensor.opti_grid_export_w"] = 3999
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=40)).states[entity] == "on"
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=70)).states[entity] == "off"


def test_pending_surplus_delay_does_not_count_offline_time():
    entity = "binary_sensor.opti_ueberschuss_70_aktiv"
    states = measurements(**{
        "input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": 5000,
        "sensor.opti_grid_export_w": 6000,
    })
    original = StrategyEngine()
    evaluate(original, states)
    restored = StrategyEngine()
    restored.restore(original.snapshot())
    assert evaluate(restored, states, now=NOW + dt.timedelta(hours=1)).states[entity] == "off"
    assert evaluate(restored, states, now=NOW + dt.timedelta(hours=1, seconds=30)).states[entity] == "on"


def test_ev_lock_immediate_on_unknown_holds_and_off_waits_five_minutes():
    engine = StrategyEngine()
    entity = "binary_sensor.opti_ev_schnellladung"
    states = measurements(**{
        "input_boolean.opti_ev_akku_pause": "on",
        "binary_sensor.opti_ev_lp1_schnell": "on",
        "binary_sensor.opti_ev_lp2_schnell": "off",
    })
    first = evaluate(engine, states)
    assert first.mode == "Akku nur Laden"
    assert first.states[entity] == "on"
    states["binary_sensor.opti_ev_lp1_schnell"] = "unavailable"
    assert evaluate(engine, states, now=NOW + dt.timedelta(minutes=10)).states[entity] == "on"
    states["binary_sensor.opti_ev_lp1_schnell"] = "off"
    assert evaluate(engine, states, now=NOW + dt.timedelta(minutes=11)).states[entity] == "on"
    assert evaluate(engine, states, now=NOW + dt.timedelta(minutes=15, seconds=59)).states[entity] == "on"
    assert evaluate(engine, states, now=NOW + dt.timedelta(minutes=16)).states[entity] == "off"


def test_ev_feature_off_removes_gate_immediately_despite_delay():
    engine = StrategyEngine()
    states = measurements(**{
        "input_boolean.opti_ev_akku_pause": "on",
        "binary_sensor.opti_ev_lp1_schnell": "on",
        "binary_sensor.opti_ev_lp2_schnell": "off",
    })
    assert evaluate(engine, states).mode == "Akku nur Laden"
    states["input_boolean.opti_ev_akku_pause"] = "off"
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=1)).mode == "Akku Dynamisch"


def test_balancing_counts_observed_minute_boundaries_and_restores_29_minutes():
    engine = StrategyEngine()
    states = measurements(**{SOC: 99, "input_number.opti_balancing_intervall_tage": 14})
    evaluate(engine, states)
    for minute in range(1, 30):
        now = NOW + dt.timedelta(minutes=minute)
        result = evaluate(engine, states, now=now)
        assert int(result.states[MINUTES]) == minute
        again = evaluate(engine, states, now=now + dt.timedelta(seconds=10))
        assert int(again.states[MINUTES]) == minute
    restored = StrategyEngine()
    restored.restore(engine.snapshot())
    start = NOW + dt.timedelta(hours=1)
    result = evaluate(restored, states, now=start)
    assert int(result.states[MINUTES]) == 29
    assert result.states[DONE_VALID] == "off"
    complete = evaluate(restored, states, now=start + dt.timedelta(minutes=1))
    assert int(complete.states[MINUTES]) == 0
    assert int(complete.states[DAYS]) == 0
    assert complete.states[DONE_VALID] == "on"
    assert complete.states[DONE_AT] == (start + dt.timedelta(minutes=1)).isoformat()
    later = evaluate(restored, states, now=start + dt.timedelta(minutes=2))
    assert int(later.states[MINUTES]) == 0
    assert later.states[DONE_AT] == complete.states[DONE_AT]


@pytest.mark.parametrize("bad", [98.5, 98, "unavailable"])
def test_balancing_brief_dip_or_source_error_discards_confirmations(bad):
    engine = StrategyEngine()
    states = measurements(**{SOC: 99})
    evaluate(engine, states)
    result = evaluate(engine, states, now=NOW + dt.timedelta(minutes=1))
    assert int(result.states[MINUTES]) == 1
    states[SOC] = bad
    assert int(evaluate(engine, states, now=NOW + dt.timedelta(minutes=1, seconds=5)).states[MINUTES]) == 0
    states[SOC] = 99
    assert int(evaluate(engine, states, now=NOW + dt.timedelta(minutes=1, seconds=6)).states[MINUTES]) == 0


def test_balancing_offline_gap_never_adds_multiple_minutes():
    engine = StrategyEngine()
    states = measurements(**{SOC: 99})
    evaluate(engine, states)
    result = evaluate(engine, states, now=NOW + dt.timedelta(hours=3))
    assert int(result.states[MINUTES]) == 1
    assert result.states[DONE_VALID] == "off"


def test_daily_balancing_counter_increments_once_and_skips_completed_day():
    engine = StrategyEngine()
    start = NOW.replace(hour=23, minute=58)
    states = measurements(**{SOC: 50})
    evaluate(engine, states, now=start)
    first = evaluate(engine, states, now=start + dt.timedelta(minutes=1))
    assert int(first.states[DAYS]) == 1
    again = evaluate(engine, states, now=start + dt.timedelta(minutes=1, seconds=10))
    assert int(again.states[DAYS]) == 1
    second_engine = StrategyEngine()
    states.update({DONE_VALID: "on", DONE_AT: NOW.isoformat()})
    evaluate(second_engine, states, now=start)
    assert int(evaluate(second_engine, states, now=start + dt.timedelta(minutes=1)).states[DAYS]) == 0


@pytest.mark.parametrize("override", ["off", "on"])
def test_selected_balancing_branch_applies_high_soc_charge_taper(override):
    result = evaluate(states=measurements(**{
        SOC: 97, DAYS: 20, "input_number.opti_balancing_intervall_tage": 14,
        "input_boolean.opti_prognose_netzladen": "off",
        "input_boolean.opti_manuelle_ladegrenze": override,
    }))
    assert result.reason.startswith("Balancing-Watchdog")
    assert result.mode == "Akku nur Laden"
    assert float(result.states[POWER]) == 200


def test_booster_cleanup_changes_internal_helpers_after_mode_selection():
    result = evaluate(states=measurements(**{
        SOC: 100, "input_boolean.hausakku_aus_netz_laden": "on",
        "sensor.opti_price_current_ct_kwh": 25,
    }))
    assert result.mode == "Akku nur Entladen"
    assert result.helper_updates["input_boolean.hausakku_aus_netz_laden"] == "off"
    assert float(result.helper_updates["input_number.ladepreis"]) == 0.25


def test_60_minute_arithmetic_mean_deduplicates_timestamp_and_expires_samples():
    engine = StrategyEngine()
    first = evaluate(engine, measurements(**{"sensor.opti_house_consumption_w": 400}))
    assert float(first.states["sensor.opti_house_consumption_60min_w"]) == 400
    second = evaluate(engine, measurements(**{"sensor.opti_house_consumption_w": 800}), now=NOW + dt.timedelta(minutes=1))
    assert float(second.states["sensor.opti_house_consumption_60min_w"]) == 600
    same_time = evaluate(engine, measurements(**{"sensor.opti_house_consumption_w": 800}), now=NOW + dt.timedelta(minutes=1))
    assert float(same_time.states["sensor.opti_house_consumption_60min_w"]) == 600
    expired = evaluate(engine, measurements(**{"sensor.opti_house_consumption_w": 1000}), now=NOW + dt.timedelta(minutes=62))
    assert float(expired.states["sensor.opti_house_consumption_60min_w"]) == 1000


def test_price_series_absent_is_unavailable_and_releases_paid_grid_mode():
    result = evaluate(states=measurements(**{MODE: "Akku Netzladen", "sensor.opti_price_current_ct_kwh": 25}))
    assert result.states["sensor.opti_price_level"] == "unavailable"
    assert result.states["sensor.opti_peak_reserve_soc"] == "unavailable"
    assert result.mode == "Akku Dynamisch"


def test_negative_price_network_rule_requires_its_own_gate():
    states = measurements(**{
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_price_current_ct_kwh": -5,
    })
    enabled = evaluate(states=states)
    assert enabled.mode == "Akku Netzladen"
    states["input_boolean.opti_prognose_netzladen"] = "off"
    assert evaluate(states=states).mode != "Akku Netzladen"


def test_sonnentag_score_uses_today_after_midnight_and_peak_horizon_holds():
    engine = StrategyEngine()
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    states = measurements(**{
        "sun.sun": "below_horizon",
        "sensor.opti_forecast_today_kwh": 20,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
    })
    attrs = {"sun.sun": {"next_rising": "2026-07-27T05:45:00+02:00", "next_setting": "2026-07-27T21:00:00+02:00"}}
    result = evaluate(engine, states, attrs, now)
    assert result.states["sensor.opti_forecast_score_sonnentag"] == "10"
    assert result.attributes["sensor.opti_forecast_score_sonnentag"]["quelle"] == "sensor.opti_forecast_today_kwh"
    assert result.states["binary_sensor.opti_peak_horizont_lang"] == "off"
    assert result.states["binary_sensor.opti_pv_reichtag"] == "on"


@pytest.mark.parametrize("length", [24, 96])
def test_peak_reserve_pipeline_accepts_supported_price_grids(length):
    states = measurements(**{
        "sun.sun": "below_horizon", "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_price_current_ct_kwh": 10,
    })
    attrs = solar_attrs()
    attrs["sensor.opti_price_series"] = {"today": [10] * (length * 3 // 4) + [50] * (length // 4), "tomorrow": [10] * length}
    result = evaluate(states=states, attributes=attrs)
    assert result.states["binary_sensor.opti_peak_reserve_aktiv"] == "on"
    assert float(result.states["sensor.opti_peak_reserve_soc"]) > 10
    assert result.mode == "Akku Netzladen"


@pytest.mark.parametrize(
    ("soc", "expected_mode", "reason_prefix"),
    [
        (94.9, "Akku nur Laden", "Peak-Leiter L4"),
        (95, "Akku Pause", "Ladedeckel"),
        (96, "Akku Pause", "Ladedeckel"),
        (98, "Akku Pause", "Ladedeckel"),
    ],
)
def test_maxsoc_precedes_peak_reserve_holding(soc, expected_mode, reason_prefix):
    states, attributes = peak_maxsoc_inputs(soc)
    result = evaluate(states=states, attributes=attributes)

    assert result.states["binary_sensor.opti_peak_reserve_aktiv"] == "on"
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == 95
    assert result.mode == expected_mode
    assert result.reason.startswith(reason_prefix)
    assert result.states["sensor.opti_strategie_vorschau"] == result.mode
    assert result.attributes["sensor.opti_strategie_vorschau"]["grund"] == result.reason
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"


def test_maxsoc_peak_latch_survives_restore_and_releases_below_band():
    engine = StrategyEngine()
    for minute, (soc, expected_deckel, expected_mode) in enumerate([
        (95, "on", "Akku Pause"),
        (94, "on", "Akku Pause"),
    ]):
        states, attributes = peak_maxsoc_inputs(soc)
        result = evaluate(engine, states, attributes, NOW + dt.timedelta(minutes=minute))
        assert result.states[DECKEL] == expected_deckel
        assert result.mode == expected_mode

    restored = StrategyEngine()
    restored.restore(json.loads(json.dumps(engine.snapshot(), allow_nan=False)))
    for minute, (soc, expected_deckel, expected_mode) in enumerate([
        (92, "on", "Akku Pause"),
        (91.9, "off", "Akku Netzladen"),
    ], start=2):
        states, attributes = peak_maxsoc_inputs(soc)
        result = evaluate(restored, states, attributes, NOW + dt.timedelta(minutes=minute))
        assert result.states[DECKEL] == expected_deckel
        assert result.mode == expected_mode


@pytest.mark.parametrize("override", ["off", "on"])
def test_maxsoc_with_ev_discharge_block_uses_pause(override):
    states, attributes = peak_maxsoc_inputs(
        95,
        **{
            "input_boolean.opti_ev_akku_pause": "on",
            "binary_sensor.opti_ev_schnellladung": "on",
            "input_boolean.opti_manuelle_ladegrenze": override,
        },
    )
    attributes["sensor.opti_price_series"] = {
        "today": [10] * 24,
        "tomorrow": [10] * 24,
    }
    result = evaluate(states=states, attributes=attributes)

    assert result.states["binary_sensor.opti_peak_reserve_aktiv"] == "off"
    assert result.mode == "Akku Pause"
    assert result.reason == "Ladedeckel (maxsoc erreicht; EV-Entladesperre)"
    assert result.states["sensor.opti_strategie_vorschau"] == result.mode


@pytest.mark.parametrize(
    ("states_override", "expected_watchdog", "expected_mode"),
    [
        ({"sun.sun": "above_horizon"}, "pv", "Akku nur Laden"),
        ({"input_boolean.opti_balancing_netzladen": "on", "sensor.opti_price_current_ct_kwh": -1}, "netz", "Akku Netzladen"),
    ],
)
@pytest.mark.parametrize("soc", [95, 99, 100])
def test_balancing_still_precedes_maxsoc_at_upper_soc_boundary(
    soc, states_override, expected_watchdog, expected_mode
):
    states, attributes = peak_maxsoc_inputs(
        soc,
        **{
            DAYS: 7,
            "input_number.opti_balancing_intervall_tage": 7,
            **states_override,
        },
    )
    result = evaluate(states=states, attributes=attributes)

    if soc < 100:
        assert result.states["binary_sensor.opti_peak_reserve_aktiv"] == "on"
    assert result.states["sensor.opti_balancing_watchdog"] == expected_watchdog
    assert result.mode == expected_mode
    assert result.reason.startswith("Balancing-Watchdog")


def test_malformed_price_grid_disables_peak_reserve():
    attrs = solar_attrs()
    attrs["sensor.opti_price_series"] = {"today": [10, 20, 30, 40, 50], "tomorrow": []}
    result = evaluate(attributes=attrs)
    assert result.states["sensor.opti_peak_reserve_soc"] == "unavailable"
    assert result.states["binary_sensor.opti_peak_reserve_aktiv"] == "off"


def test_render_failure_reports_entity_and_blocks_invalid_derived_value():
    resources = load_resources()
    for block in resources["template_blocks"]:
        for definition in block.get("sensor", []):
            if definition["unique_id"] == "opti_target_soc":
                definition["state"] = "{{ 1 / 0 }}"
    result = evaluate(StrategyEngine(resources))
    assert result.states[TARGET] == "unavailable"
    assert result.attributes["sensor.opti_engine_diagnostics"]["template_errors"][TARGET] == "ZeroDivisionError"


def test_display_attribute_failure_keeps_controlled_value_and_decision():
    reference = evaluate()
    resources = load_resources()
    for block in resources["template_blocks"]:
        for definition in block.get("sensor", []):
            if definition["unique_id"] == "opti_charge_power_w":
                definition["attributes"]["branch"] = "{{ 1 / 0 }}"
    result = evaluate(StrategyEngine(resources))
    assert result.states[POWER] == reference.states[POWER]
    assert "branch" not in result.attributes[POWER]
    assert result.decision_id == reference.decision_id != "unknown"
    diagnostics = result.attributes["sensor.opti_engine_diagnostics"]
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    assert "template_errors" not in diagnostics
    assert diagnostics["display_errors"] == {f"{POWER}:branch": "ZeroDivisionError"}


def test_unavailable_value_drops_stale_explanation_but_keeps_hysteresis_memory():
    engine = StrategyEngine()
    assert "branch" in evaluate(engine).attributes[POWER]
    missing = measurements()
    del missing[TEMP]
    result = evaluate(engine, states=missing)
    assert result.states[POWER] == "unavailable"
    assert "branch" not in result.attributes[POWER]

    engine = StrategyEngine()
    level = evaluate(engine).attributes[TARGET]["level"]
    engine._entities[TARGET]["definition"]["state"] = "{{ 1 / 0 }}"
    failed = evaluate(engine)
    assert failed.states[TARGET] == "unavailable"
    assert failed.attributes[TARGET]["level"] == level
    assert "branch" not in failed.attributes[TARGET]


def test_naive_clock_is_rejected_instead_of_silent_wrong_solar_day():
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate(now=dt.datetime(2026, 1, 1))


def test_cyclic_resource_graph_rejected_before_evaluation():
    resources = {"schema_version": 1, "template_blocks": [{"sensor": [
        {"unique_id": "one", "state": "{{ states('sensor.two') }}"},
        {"unique_id": "two", "state": "{{ states('sensor.one') }}"},
    ]}]}
    with pytest.raises(ValueError, match="Cyclic"):
        StrategyEngine(resources)


def test_surplus_veto_requires_forecast_scarcity_and_holds_20_percent_band():
    engine = StrategyEngine()
    entity = "binary_sensor.opti_ueberschuss_veto_aktiv"
    states = measurements(**{
        SOC: 70, "sensor.opti_house_consumption_w": 0,
        "sensor.opti_grid_export_w": 600,
        "sensor.opti_forecast_remaining_today_kwh": 4,
    })
    assert evaluate(engine, states).states[entity] == "off"  # 4 > 3 kWh needed
    states["sensor.opti_forecast_remaining_today_kwh"] = 2.9
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=1)).states[entity] == "off"
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=61)).states[entity] == "on"
    states["sensor.opti_forecast_remaining_today_kwh"] = 3.5
    states["sensor.opti_grid_export_w"] = 100
    states["sensor.opti_battery_power_w"] = 500
    held = evaluate(engine, states, now=NOW + dt.timedelta(seconds=62))
    assert held.states[entity] == "on"  # 3.5 < 3 * 1.2; net export + battery = 600 W
    assert held.attributes[entity]["knappheit_gate_offen"] is True
    states["sensor.opti_forecast_remaining_today_kwh"] = 3.7
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=63)).states[entity] == "on"
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=123)).states[entity] == "off"


def test_ev_latch_survives_restore_but_missing_initial_ev_data_does_not_enable_it():
    entity = "binary_sensor.opti_ev_schnellladung"
    original = StrategyEngine()
    states = measurements(**{
        "input_boolean.opti_ev_akku_pause": "on",
        "binary_sensor.opti_ev_lp1_schnell": "on",
        "binary_sensor.opti_ev_lp2_schnell": "off",
    })
    evaluate(original, states)
    restored = StrategyEngine()
    restored.restore(original.snapshot())
    states["binary_sensor.opti_ev_lp1_schnell"] = "unavailable"
    assert evaluate(restored, states, now=NOW + dt.timedelta(hours=1)).states[entity] == "on"
    assert evaluate(StrategyEngine(), states).states[entity] == "off"


def test_peak_horizon_score_two_keeps_previous_short_horizon():
    engine = StrategyEngine()
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    states = measurements(**{
        "sun.sun": "below_horizon",
        "sensor.opti_house_consumption_w": 1000,
        "sensor.opti_forecast_today_kwh": 7.2,  # score 3 against 24 kWh/day
    })
    attrs = {"sun.sun": {"next_rising": "2026-07-27T05:45:00+02:00", "next_setting": "2026-07-27T21:00:00+02:00"}}
    short = evaluate(engine, states, attrs, now)
    assert short.states["binary_sensor.opti_peak_horizont_lang"] == "off"
    states["sensor.opti_forecast_today_kwh"] = 4.8
    held = evaluate(engine, states, attrs, now + dt.timedelta(minutes=1))
    assert held.states["sensor.opti_forecast_score_sonnentag"] == "2"
    assert held.states["binary_sensor.opti_peak_horizont_lang"] == "off"
    states["sensor.opti_forecast_today_kwh"] = 2.4
    long = evaluate(engine, states, attrs, now + dt.timedelta(minutes=2))
    assert long.states["binary_sensor.opti_peak_horizont_lang"] == "on"
    states["sensor.opti_forecast_today_kwh"] = 4.8
    assert evaluate(engine, states, attrs, now + dt.timedelta(minutes=3)).states["binary_sensor.opti_peak_horizont_lang"] == "on"
    restored = StrategyEngine()
    restored.restore(engine.snapshot())
    assert evaluate(restored, states, attrs, now + dt.timedelta(minutes=4)).states["binary_sensor.opti_peak_horizont_lang"] == "on"


def test_fresh_peak_horizon_score_two_starts_short_but_missing_score_starts_long():
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    states = measurements(**{"sun.sun": "below_horizon", "sensor.opti_house_consumption_w": 1000,
                             "sensor.opti_forecast_today_kwh": 4.8})
    attrs = {"sun.sun": {"next_rising": "2026-07-27T05:45:00+02:00",
                          "next_setting": "2026-07-27T21:00:00+02:00"}}
    fresh = evaluate(StrategyEngine(), states, attrs, now)
    assert fresh.states["sensor.opti_forecast_score_sonnentag"] == "2"
    assert fresh.states["binary_sensor.opti_peak_horizont_lang"] == "off"
    states["sensor.opti_forecast_today_kwh"] = "unavailable"
    assert evaluate(StrategyEngine(), states, attrs, now).states["binary_sensor.opti_peak_horizont_lang"] == "on"


def _night_horizon_states(load_w, forecast_kwh=2.5, sun="below_horizon"):
    # Issue #124: with a constant forecast of 2.5 kWh the sun-day score is
    # 1 at 700 W, 3 at 407 W and 2 at 530 W house load.
    return measurements(**{
        "sun.sun": sun,
        "sensor.opti_house_consumption_w": load_w,
        "sensor.opti_house_consumption_60min_w": load_w,
        "sensor.opti_forecast_today_kwh": forecast_kwh,
    })


_NIGHT_ATTRS = {"sun.sun": {"next_rising": "2026-07-27T05:45:00+02:00",
                            "next_setting": "2026-07-27T21:00:00+02:00"}}


def test_peak_horizon_night_latch_ignores_load_driven_score_jumps():
    engine = StrategyEngine()
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    entity = "binary_sensor.opti_peak_horizont_lang"
    first = evaluate(engine, _night_horizon_states(700), _NIGHT_ATTRS, now)
    assert first.states["sensor.opti_forecast_score_sonnentag"] == "1"
    assert first.states[entity] == "on"
    second = evaluate(engine, _night_horizon_states(407), _NIGHT_ATTRS, now + dt.timedelta(minutes=1))
    assert second.states["sensor.opti_forecast_score_sonnentag"] == "3"
    assert second.states[entity] == "on"
    assert "Nacht: Vorzustand" in second.attributes[entity]["branch"]
    third = evaluate(engine, _night_horizon_states(530), _NIGHT_ATTRS, now + dt.timedelta(minutes=2))
    assert third.states["sensor.opti_forecast_score_sonnentag"] == "2"
    assert third.states[entity] == "on"
    restored = StrategyEngine()
    restored.restore(engine.snapshot())
    assert evaluate(restored, _night_horizon_states(407), _NIGHT_ATTRS,
                    now + dt.timedelta(minutes=3)).states[entity] == "on"
    # A night-time short horizon only becomes long at score <= 1, as before.
    short = StrategyEngine()
    assert evaluate(short, _night_horizon_states(530), _NIGHT_ATTRS, now).states[entity] == "off"
    assert evaluate(short, _night_horizon_states(407), _NIGHT_ATTRS, now + dt.timedelta(minutes=1)).states[entity] == "off"
    assert evaluate(short, _night_horizon_states(700), _NIGHT_ATTRS, now + dt.timedelta(minutes=2)).states[entity] == "on"


def test_peak_horizon_day_hysteresis_still_switches_off_at_score_three():
    engine = StrategyEngine()
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    entity = "binary_sensor.opti_peak_horizont_lang"
    assert evaluate(engine, _night_horizon_states(700), _NIGHT_ATTRS, now).states[entity] == "on"
    assert evaluate(engine, _night_horizon_states(407), _NIGHT_ATTRS, now + dt.timedelta(minutes=1)).states[entity] == "on"
    sunrise = dt.datetime(2026, 7, 27, 6, 0, tzinfo=TZ)
    day_attrs = {"sun.sun": {"next_rising": "2026-07-28T05:46:00+02:00",
                             "next_setting": "2026-07-27T21:00:00+02:00"}}
    # After sunrise the sun-day score reads tomorrow's forecast; score 2 is still held.
    states = _night_horizon_states(530, sun="above_horizon")
    states["sensor.opti_forecast_tomorrow_kwh"] = 2.5
    held = evaluate(engine, states, day_attrs, sunrise)
    assert held.states["sensor.opti_forecast_score_sonnentag"] == "2"
    assert held.states[entity] == "on"
    states = _night_horizon_states(407, sun="above_horizon")
    states["sensor.opti_forecast_tomorrow_kwh"] = 2.5
    released = evaluate(engine, states, day_attrs, sunrise + dt.timedelta(minutes=1))
    assert released.states["sensor.opti_forecast_score_sonnentag"] == "3"
    assert released.states[entity] == "off"


def test_ev_score_statistic_is_separate_and_invalid_sample_hides_history():
    engine = StrategyEngine()
    states = measurements(**{"sensor.opti_house_consumption_w": 2200,
                             "sensor.opti_forecast_house_instant_w": 350,
                             "input_boolean.opti_score_house_includes_ev": "on"})
    first = evaluate(engine, states)
    assert float(first.states["sensor.opti_forecast_house_60min_w"]) == 350
    assert float(first.states["sensor.opti_house_consumption_60min_w"]) == 2200
    assert first.states["sensor.opti_forecast_score_tomorrow"] == "10"
    assert first.states["sensor.opti_forecast_score_sonnentag"] == "10"
    states["sensor.opti_forecast_house_instant_w"] = "unavailable"
    invalid = evaluate(engine, states, now=NOW + dt.timedelta(minutes=1))
    assert invalid.states["sensor.opti_forecast_house_60min_w"] == "unavailable"
    assert invalid.states["sensor.opti_forecast_score_tomorrow"] == "4"
    assert invalid.states["sensor.opti_forecast_score_sonnentag"] == "4"
    assert len(engine.snapshot()["samples"]["sensor.opti_forecast_house_60min_w"]) == 1
    states["sensor.opti_forecast_house_instant_w"] = 500
    recovered = evaluate(engine, states, now=NOW + dt.timedelta(minutes=2))
    assert float(recovered.states["sensor.opti_forecast_house_60min_w"]) == 425
    engine.reset_score_statistics()
    assert "sensor.opti_forecast_house_60min_w" not in engine.snapshot()["samples"]
    assert "sensor.opti_house_consumption_60min_w" in engine.snapshot()["samples"]


def test_ev_score_opt_out_keeps_existing_raw_house_denominator():
    states = measurements(**{"sensor.opti_house_consumption_w": 2200,
                             "sensor.opti_forecast_house_instant_w": 350,
                             "input_boolean.opti_score_house_includes_ev": "off"})
    result = evaluate(StrategyEngine(), states)
    assert result.states["sensor.opti_forecast_score_tomorrow"] == "4"
    assert result.states["sensor.opti_forecast_score_sonnentag"] == "4"


def test_balancing_does_not_taper_when_higher_priority_peak_wins():
    now = NOW.replace(hour=19)
    states = measurements(**{
        SOC: 97, DAYS: 20, "input_number.opti_balancing_intervall_tage": 14,
        "sensor.opti_price_current_ct_kwh": 50,
        "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sun.sun": "below_horizon",
    })
    attrs = solar_attrs(now)
    attrs["sensor.opti_price_series"] = {
        "today": [10] * 18 + [50] * 4 + [10] * 2,
        "tomorrow": [10] * 24,
    }
    result = evaluate(states=states, attributes=attrs, now=now)
    assert result.reason.startswith("Peak-Leiter L1")
    assert result.mode == "Akku nur Entladen"
    assert float(result.states[POWER]) == 500  # normal .05C, not balancing .02C


def test_forecast_optimism_blend_drives_current_and_tomorrow_scores():
    states = measurements(**{
        "sensor.opti_forecast_remaining_today_kwh": 20,
        "sensor.opti_forecast_tomorrow_kwh": 20,
        "input_number.opti_forecast_optimismus": 50,
    })
    attrs = solar_attrs()
    attrs["sensor.opti_forecast_remaining_today_kwh"] = {"estimate10": 10}
    attrs["sensor.opti_forecast_tomorrow_kwh"] = {"estimate10": 4}
    result = evaluate(states=states, attributes=attrs)
    assert float(result.states["sensor.opti_forecast_effective_remaining_kwh"]) == 15
    assert result.attributes["sensor.opti_forecast_score_tomorrow"]["alpha"] == 0.5


@pytest.mark.parametrize("p10,expected", [
    (0, "0"),        # a true P10 of zero is pessimistic, not "missing"
    (0.001, "0"),    # tiny positive P10 behaves as before
    (None, "10"),    # missing attribute falls back to the median
    (-1, "10"),      # invalid (negative) attribute falls back to the median
    ("abc", "10"),   # non-numeric attribute falls back to the median
])
def test_zero_p10_counts_as_valid_for_tomorrow_and_sun_day_scores(p10, expected):
    states = measurements(**{"sensor.opti_forecast_tomorrow_kwh": 20, "input_number.opti_forecast_optimismus": 0})
    attrs = solar_attrs()
    if p10 is not None:
        attrs["sensor.opti_forecast_tomorrow_kwh"] = {"estimate10": p10}
    result = evaluate(states=states, attributes=attrs)
    assert result.states["sensor.opti_forecast_score_tomorrow"] == expected
    assert result.states["sensor.opti_forecast_score_sonnentag"] == expected
    assert float(result.attributes["sensor.opti_forecast_score_tomorrow"]["p10_kwh"]) == (
        20 if p10 in (None, -1, "abc") else round(p10, 2))


def test_day_score_is_unavailable_without_house_load_instead_of_assuming_400_w():
    states = measurements(**{"sensor.opti_house_consumption_w": "unavailable",
                             "sensor.opti_house_consumption_60min_w": "unavailable"})
    result = evaluate(states=states)
    assert result.states["sensor.opti_forecast_score"] == "unavailable"
    assert result.states["sensor.opti_forecast_score_sonnentag"] == "unavailable"
    states["sensor.opti_house_consumption_w"] = 400
    assert evaluate(states=states).states["sensor.opti_forecast_score"] == "10"


def test_minimum_discharge_price_is_display_only():
    """The tariff margin feeds one sensor; no decision branch or template reads it."""
    resources = load_resources()
    text = json.dumps(resources["strategy"])
    assert "opti_mindestentladepreis_ct_kwh" not in text.replace(
        '"sensor.opti_mindestentladepreis_ct_kwh"', "")  # only the trigger entity list
    others = json.dumps({key: value for key, value in resources.items() if key != "strategy"})
    assert others.count("opti_mindestentladepreis_ct_kwh") == 1  # the sensor definition itself
    for soc, price in ((50, 10), (50, 60), (20, 35), (90, 5)):
        base = measurements(**{SOC: soc, "sensor.opti_price_current_ct_kwh": price,
                               "input_number.ladepreis": 0.2})
        cheap = evaluate(states={**base, "input_number.mindestpreisdifferenz_lade_entladepreis": 0})
        dear = evaluate(states={**base, "input_number.mindestpreisdifferenz_lade_entladepreis": 0.3})
        assert (cheap.mode, cheap.reason) == (dear.mode, dear.reason)
        assert float(cheap.states["sensor.opti_mindestentladepreis_ct_kwh"]) == 20
        assert float(dear.states["sensor.opti_mindestentladepreis_ct_kwh"]) == 50


def test_zero_p10_drives_effective_remaining_target_soc_and_preview():
    """Median 20, P10 0, optimism 0: effectively nothing is left, not 20 kWh."""
    states = measurements(**{"sensor.opti_forecast_remaining_today_kwh": 20,
                             "input_number.opti_forecast_optimismus": 0})
    attrs = solar_attrs()
    attrs["sensor.opti_forecast_remaining_today_kwh"] = {"estimate10": 0}
    pessimistic = evaluate(states=states, attributes=attrs)
    assert float(pessimistic.states["sensor.opti_forecast_effective_remaining_kwh"]) == 0
    assert float(pessimistic.attributes["sensor.opti_forecast_effective_remaining_kwh"]["p10_kwh"]) == 0
    empty = evaluate(states=measurements(**{"sensor.opti_forecast_remaining_today_kwh": 0,
                                            "input_number.opti_forecast_optimismus": 0}))
    assert pessimistic.states[TARGET] == empty.states[TARGET]
    assert pessimistic.states["sensor.opti_forecast_score"] == empty.states["sensor.opti_forecast_score"]
    assert (pessimistic.mode, pessimistic.reason) == (empty.mode, empty.reason)
    assert pessimistic.states["sensor.opti_strategie_vorschau"] == pessimistic.mode
    assert pessimistic.attributes["sensor.opti_strategie_vorschau"]["grund"] == pessimistic.reason
    optimistic = evaluate(states=states, attributes=solar_attrs())  # attribute missing -> median
    assert float(optimistic.states["sensor.opti_forecast_effective_remaining_kwh"]) == 20
    assert float(optimistic.states[TARGET]) < float(pessimistic.states[TARGET])


def test_clock_going_backwards_restarts_pending_delay():
    engine = StrategyEngine()
    entity = "binary_sensor.opti_ueberschuss_70_aktiv"
    states = measurements(**{
        "input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": 5000,
        "sensor.opti_grid_export_w": 6000,
    })
    evaluate(engine, states)
    assert evaluate(engine, states, now=NOW - dt.timedelta(seconds=20)).states[entity] == "off"
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=9)).states[entity] == "off"
    assert evaluate(engine, states, now=NOW + dt.timedelta(seconds=10)).states[entity] == "on"


@pytest.mark.parametrize("day,hours,slots_per_hour", [
    (dt.date(2026, 3, 29), 23, 1), (dt.date(2026, 10, 25), 25, 1),
    (dt.date(2026, 3, 29), 23, 4), (dt.date(2026, 10, 25), 25, 4),
])
def test_dst_days_keep_two_expensive_hours_exact(day, hours, slots_per_hour):
    now = dt.datetime.combine(day, dt.time(0, 5), tzinfo=TZ)
    count = hours * slots_per_hour
    states = measurements(**{
        "sun.sun": "below_horizon", "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_price_current_ct_kwh": 10,
    })
    attrs = solar_attrs(now)
    attrs["sensor.opti_price_series"] = {"today": [10] * (count - 2 * slots_per_hour) + [50] * (2 * slots_per_hour), "tomorrow": []}
    result = evaluate(states=states, attributes=attrs, now=now)
    peak = result.attributes["sensor.opti_peak_reserve_soc"]
    assert peak["peak_stunden_ve"] == 2.0
    assert peak["peak_stunden_exp"] == 0.0
    assert peak["benoetigt_kwh"] == 1.78  # 2 h * .8 kW / .9 efficiency
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"


@pytest.mark.parametrize("slots_per_hour", [1, 4])
def test_repeated_dst_hour_does_not_reinclude_expired_first_occurrence(slots_per_hour):
    # Oct 25 has 02:00 CEST at slot 2 and 02:00 CET at slot 3. Only the
    # first occurrence is expensive. At fold=1 it is genuinely in the past.
    count = 25 * slots_per_hour
    prices = [10] * count
    prices[2 * slots_per_hour:3 * slots_per_hour] = [50] * slots_per_hour
    states = measurements(**{
        "sun.sun": "below_horizon", "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_price_current_ct_kwh": 10,
    })
    for fold, expected in [(0, 1.0), (1, 0.0)]:
        now = dt.datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=fold)
        attrs = solar_attrs(now)
        attrs["sensor.opti_price_series"] = {"today": prices, "tomorrow": []}
        result = evaluate(states=states, attributes=attrs, now=now)
        assert result.attributes["sensor.opti_peak_reserve_soc"]["peak_stunden_ve"] == expected


@pytest.mark.parametrize("count", [23, 25, 92, 100])
def test_dst_shaped_list_on_normal_day_is_rejected_instead_of_shifted(count):
    attrs = solar_attrs()
    attrs["sensor.opti_price_series"] = {"today": [10] * count, "tomorrow": []}
    result = evaluate(attributes=attrs)
    assert result.states["sensor.opti_peak_reserve_soc"] == "unavailable"
    assert result.states["binary_sensor.opti_peak_reserve_aktiv"] == "off"


def test_tomorrow_list_is_validated_against_tomorrows_short_calendar_day():
    now = dt.datetime(2026, 3, 28, 23, 30, tzinfo=TZ)
    states = measurements(**{
        "sun.sun": "below_horizon", "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_price_current_ct_kwh": 10,
    })
    attrs = solar_attrs(now)
    attrs["sensor.opti_price_series"] = {"today": [10] * 24, "tomorrow": [10] * 21 + [50] * 2}
    result = evaluate(states=states, attributes=attrs, now=now)
    assert result.attributes["sensor.opti_peak_reserve_soc"]["peak_stunden_ve"] == 2.0
    horizon = dt.datetime.fromisoformat(result.attributes["sensor.opti_peak_reserve_soc"]["horizont_ende"])
    assert horizon.timestamp() - now.timestamp() == 36 * 3600


def test_all_templates_are_precompiled_and_syntax_errors_fail_during_setup():
    resources = load_resources()
    for block in resources["template_blocks"]:
        for definition in block.get("sensor", []):
            if definition["unique_id"] == "opti_target_soc":
                definition["state"] = "{{ broken( }}"
    with pytest.raises(_ENGINE_MODULE.jinja2.TemplateSyntaxError):
        StrategyEngine(resources)


def test_load_source_change_discards_only_house_history():
    engine = StrategyEngine()
    evaluate(engine=engine, states=measurements(**{"sensor.opti_house_consumption_w": 1000}))
    before = engine.snapshot()
    assert "sensor.house_battery_load_30_mins" in before["samples"]
    engine.reset_load_statistics()
    result = evaluate(engine=engine, states=measurements(**{"sensor.opti_house_consumption_w": 3000}), now=NOW+dt.timedelta(minutes=1))
    assert float(result.states["sensor.opti_house_consumption_60min_w"]) == 3000
    assert len(engine.snapshot()["samples"]["sensor.house_battery_load_30_mins"]) == 2


@pytest.mark.parametrize("price", [-5, 10])
def test_new_integration_defaults_do_not_opt_into_automatic_grid_charging(price):
    import runpy

    definitions = runpy.run_path(Path(__file__).parents[1] / "custom_components/opti_akku/definitions.py")
    DEFINITIONS = {**definitions["NUMBER_DEFINITIONS"], **definitions["SWITCH_DEFINITIONS"]}

    states = measurements(**{
        "sun.sun": "below_horizon", "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_price_current_ct_kwh": price,
    })
    states.update({key: definition["default"] for key, definition in DEFINITIONS.items()})
    states["input_boolean.akku_opti_automatik"] = "on"
    attrs = solar_attrs()
    attrs["sensor.opti_price_series"] = {"today": [10] * 18 + [50] * 6, "tomorrow": [10] * 24}
    assert evaluate(states=states, attributes=attrs).mode != "Akku Netzladen"
    states["input_boolean.opti_prognose_netzladen"] = True
    assert evaluate(states=states, attributes=attrs).mode == "Akku Netzladen"


def test_invalid_bundled_resource_shape_is_rejected_before_use():
    with pytest.raises(ValueError, match="Unsupported strategy resource version"):
        StrategyEngine({"schema_version": 2})

    duplicate = {
        "schema_version": 1,
        "template_blocks": [
            {
                "sensor": [
                    {"unique_id": "duplicate", "state": "1"},
                    {"unique_id": "duplicate", "state": "2"},
                ]
            }
        ],
    }
    with pytest.raises(ValueError, match="Duplicate internal entity"):
        StrategyEngine(duplicate)


def test_restore_ignores_invalid_sample_collections():
    engine = StrategyEngine()
    engine.restore(
        {
            "version": 1,
            "helpers": {},
            "states": {},
            "attributes": {},
            "samples": {"not-a-list": "bad", "mixed": [[1, 2], ["bad", 3], [4]]},
        }
    )

    assert "not-a-list" not in engine.snapshot()["samples"]
    assert engine.snapshot()["samples"]["mixed"] == [[1.0, 2.0]]


def test_explicit_winter_permission_overrides_derived_template():
    states = measurements(**{"binary_sensor.opti_winter_charging_allowed": "on"})

    result = evaluate(states=states)

    assert result.states["binary_sensor.opti_winter_charging_allowed"] == "on"


def test_failed_strategy_gate_and_action_both_pause_with_diagnostics():
    gated = load_resources()
    gated["strategy"]["conditions"] = ["{{ false }}"]
    result = evaluate(StrategyEngine(gated))
    assert result.mode == "Akku Pause"
    assert result.reason == "Strategie-Eingangsbedingungen nicht erfüllt"

    broken = load_resources()
    broken["strategy"]["actions"] = [
        {
            "action": "unsupported.service",
            "target": {"entity_id": MODE},
        }
    ]
    result = evaluate(StrategyEngine(broken))
    assert result.mode == "Akku Pause"
    assert result.reason == "Fehler bei der Strategieauswertung"
    assert result.attributes["sensor.opti_engine_diagnostics"]["template_errors"] == {
        "strategy": "ValueError"
    }


def test_template_runtime_helpers_fail_closed_on_malformed_resources():
    engine = StrategyEngine()
    engine._now = NOW
    engine._states = {
        "sensor.value": "unavailable",
        "input_number.threshold": "unavailable",
        "sun.sun": "below_horizon",
    }

    with pytest.raises(ValueError, match="Non-numeric"):
        _ENGINE_MODULE._float(None)
    assert _ENGINE_MODULE._truth("yes") is True
    assert engine._as_datetime(NOW) == NOW
    assert engine._as_datetime("2026-01-15T12:00:00").tzinfo == TZ
    assert engine._as_timestamp("invalid", 7) == 7
    assert engine._duration("01:02:03") == 3723
    assert engine._render(["plain", "{{ 1 + 1 }}"]) == ["plain", 2]
    assert engine._all_conditions("{{ true }}") is True
    assert engine._condition("{{ true }}") is True
    assert (
        engine._condition(
            {"condition": "numeric_state", "entity_id": "sensor.value", "above": 0}
        )
        is False
    )
    engine._states["sensor.value"] = "5"
    assert (
        engine._condition(
            {
                "condition": "numeric_state",
                "entity_id": "sensor.value",
                "above": "input_number.threshold",
            }
        )
        is False
    )
    with pytest.raises(ValueError, match="sun condition"):
        engine._condition({"condition": "sun", "after": "sunset"})
    with pytest.raises(ValueError, match="Unsupported bundled condition"):
        engine._condition({"condition": "device"})

    engine._actions(
        [
            {"condition": "template", "value_template": "{{ false }}"},
            {"action": "unsupported.service"},
        ]
    )
    with pytest.raises(ValueError, match="Unsupported bundled action"):
        engine._actions([{}])
    with pytest.raises(ValueError, match="non-helper"):
        engine._actions(
            [
                {
                    "action": "input_boolean.turn_on",
                    "target": {"entity_id": "input_boolean.not_bundled"},
                }
            ]
        )


def test_extreme_reserve_full_pipeline_holds_before_peak_and_releases_during_it():
    now = NOW.replace(hour=18)
    states = measurements(**{
        SOC: 15,
        "input_number.minsoc": 5,
        "input_number.opti_peak_verbrauch_kw": 0.9,
        "input_boolean.opti_prognose_netzladen": "off",
        "sensor.opti_price_current_ct_kwh": 50,
        "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sun.sun": "below_horizon",
    })
    attrs = solar_attrs(now)
    prices = [10] * 24
    prices[18:20] = [50, 100]
    attrs["sensor.opti_price_series"] = {"today": prices, "tomorrow": [10] * 24}
    engine = StrategyEngine()
    before = evaluate(engine, states, attrs, now)
    assert before.states["sensor.opti_price_level"] == "VERY_EXPENSIVE"
    assert before.decision_id == "extreme_peak_hold"
    assert before.mode == "Akku nur Laden"
    assert before.attributes["sensor.opti_peak_reserve_soc"]["extreme_buffer_kwh"] == pytest.approx(0.25)

    states["sensor.opti_price_current_ct_kwh"] = 100
    during = evaluate(engine, states, attrs, now + dt.timedelta(hours=1))
    assert during.decision_id == "peak_l1"
    assert during.mode == "Akku nur Entladen"
    states["sensor.opti_price_current_ct_kwh"] = 10
    after = evaluate(engine, states, attrs, now + dt.timedelta(hours=2))
    assert after.states["binary_sensor.opti_extreme_price_hold"] == "off"
    assert after.attributes["sensor.opti_peak_reserve_soc"]["extreme_buffer_kwh"] == 0
    for result in (before, during, after):
        assert result.states["sensor.opti_engine_diagnostics"] == "ok"
        assert result.mode == result.states["sensor.opti_strategie_vorschau"]
        assert result.mode != "Akku Netzladen"


@pytest.mark.parametrize("price, grid_enabled, soc, charging", [
    (60, True, 13, False),
    (100, True, 13, True),
    (100, False, 13, False),
    (100, True, 15, False),
])
def test_extreme_buffer_can_extend_only_authorized_peak_precharge(price, grid_enabled, soc, charging):
    now = NOW.replace(hour=17)
    states = measurements(**{
        SOC: soc,
        "input_number.minsoc": 5,
        "input_number.opti_peak_verbrauch_kw": 0.9,
        "input_boolean.opti_prognose_netzladen": "on" if grid_enabled else "off",
        "sensor.opti_price_current_ct_kwh": 10,
        "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sun.sun": "below_horizon",
    })
    attrs = solar_attrs(now)
    prices = [10] * 24
    prices[19] = price
    attrs["sensor.opti_price_series"] = {"today": prices, "tomorrow": [10] * 24}
    result = evaluate(states=states, attributes=attrs, now=now)
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == (15 if price == 60 else 17.5)
    assert (result.decision_id == "peak_precharge") is charging
    assert (result.mode == "Akku Netzladen") is charging
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"


def _daily_load_buckets(now, load_w, hours=12.0, interval_s=60, gap_at_hours=None, gap_hours=0.0):
    """Hourly snapshot buckets of a constant load ending one interval before ``now``.

    ``gap_at_hours``/``gap_hours`` punch a measurement gap into the history.
    """
    end = now.timestamp() - interval_s
    count = int(hours * 3600 / interval_s)
    stamps = [end - (count - 1 - index) * interval_s for index in range(count)]
    if gap_at_hours is not None:
        gap_start = end - gap_at_hours * 3600
        stamps = [stamp for stamp in stamps if not gap_start <= stamp < gap_start + gap_hours * 3600]
    buckets: dict[int, list] = {}
    for stamp in stamps:
        bucket = buckets.get(int(stamp // 3600))
        if bucket is None:
            buckets[int(stamp // 3600)] = [stamp, stamp, float(load_w), 1, 0.0]
        else:
            bucket[4] = max(bucket[4], stamp - bucket[1])
            bucket[1], bucket[2], bucket[3] = stamp, bucket[2] + load_w, bucket[3] + 1
    return [row for _, row in sorted(buckets.items())]


def _restored_engine(now, **histories):
    engine = StrategyEngine()
    engine.restore({"version": 1, "buckets": histories})
    return engine


DAILY = "sensor.opti_house_consumption_24h_w"
DAILY_EV = "sensor.opti_forecast_house_24h_w"
SUNNY = "sensor.opti_forecast_score_sonnentag"
HORIZON = "binary_sensor.opti_peak_horizont_lang"


def test_statistics_sample_interval_rasters_samples_and_reports_coverage():
    engine = StrategyEngine()
    for offset_s, load_w in ((0, 400), (15, 800), (30, 800), (60, 800), (3600, 1000)):
        result = evaluate(engine, measurements(**{"sensor.opti_house_consumption_w": load_w}),
                          now=NOW + dt.timedelta(seconds=offset_s))
    snapshot = engine.snapshot()
    assert DAILY not in snapshot["samples"]  # rastered windows are stored as hourly buckets only
    assert snapshot["buckets"][DAILY] == [
        [round(NOW.timestamp()), round(NOW.timestamp()) + 60, 1200.0, 2, 60],
        [round(NOW.timestamp()) + 3600, round(NOW.timestamp()) + 3600, 1000.0, 1, 0],
    ]  # 15 s and 30 s fell inside the 60 s raster
    assert float(result.states[DAILY]) == pytest.approx(733, abs=1)
    attrs = result.attributes[DAILY]
    assert attrs["sample_count"] == 3
    assert attrs["age_coverage_ratio"] == pytest.approx(3600 / 86400, abs=0.001)
    assert attrs["sample_coverage_ratio"] == pytest.approx(3 / 1440, abs=0.001)
    assert attrs["max_gap_seconds"] == 3540
    assert attrs["coverage_ok"] is False
    # The 60-minute statistic keeps sampling every evaluation and stays raw in the snapshot.
    assert len(snapshot["samples"]["sensor.opti_house_consumption_60min_w"]) == 5
    assert "coverage_ok" not in result.attributes["sensor.opti_house_consumption_60min_w"]


def test_night_scores_use_daily_mean_once_coverage_is_real():
    # Issue #124 (tatec22): forecast 2.48 kWh, house load 700 -> 407 -> 530 W at night.
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    legacy = StrategyEngine()
    seen = []
    for minute, load_w in enumerate((700, 407, 530)):
        result = evaluate(legacy, _night_horizon_states(load_w, 2.48), _NIGHT_ATTRS, now + dt.timedelta(minutes=minute))
        seen.append(result.states[SUNNY])
        assert result.attributes[SUNNY]["load_source"] == "legacy_60min_extrapolation"
    assert seen == ["1", "3", "2"]  # 60-min extrapolation: the base load alone flips the score

    engine = _restored_engine(now, **{DAILY: _daily_load_buckets(now, 700)})
    seen, horizons = [], []
    for minute, load_w in enumerate((700, 407, 530)):
        result = evaluate(engine, _night_horizon_states(load_w, 2.48), _NIGHT_ATTRS, now + dt.timedelta(minutes=minute))
        seen.append(result.states[SUNNY])
        horizons.append(result.states[HORIZON])
        for score in (SUNNY, "sensor.opti_forecast_score_tomorrow"):
            assert result.attributes[score]["load_source"] == "tagesmittel_24h"
            assert float(result.attributes[score]["tagesmittel_24h_w"]) == pytest.approx(700, abs=1)
            assert float(result.attributes[score]["tagesbedarf_w"]) == pytest.approx(700, abs=1)
        assert result.attributes[DAILY]["coverage_ok"] is True
    # 2.48 kWh / (0.7 kW x 24 h) = 15 % -> score 1 regardless of the current hour's load.
    assert seen == ["1", "1", "1"]
    assert horizons == ["on", "on", "on"]


@pytest.mark.parametrize("history", [
    pytest.param({"hours": 11}, id="under_half_a_day"),
    pytest.param({"hours": 14, "gap_at_hours": 7, "gap_hours": 2.5}, id="gap_over_two_hours"),
    pytest.param({"hours": 12, "interval_s": 12 * 3600 - 60}, id="two_samples_twelve_hours_apart"),
])
def test_unreliable_daily_mean_keeps_the_legacy_extrapolation(history):
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    engine = _restored_engine(now, **{DAILY: _daily_load_buckets(now, 700, **history)})
    result = evaluate(engine, _night_horizon_states(407, 2.48), _NIGHT_ATTRS, now)
    assert result.attributes[DAILY]["coverage_ok"] is False
    assert result.attributes[SUNNY]["load_source"] == "legacy_60min_extrapolation"
    assert result.states[SUNNY] == "3"


def test_gap_up_to_now_counts_against_the_daily_mean():
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    engine = _restored_engine(now - dt.timedelta(hours=3), **{DAILY: _daily_load_buckets(now - dt.timedelta(hours=3), 700, hours=14)})
    result = evaluate(engine, _night_horizon_states(407, 2.48), _NIGHT_ATTRS, now)
    assert result.attributes[DAILY]["max_gap_seconds"] == pytest.approx(3 * 3600 + 60, abs=1)
    assert result.attributes[DAILY]["coverage_ok"] is False
    assert result.states[SUNNY] == "3"


def test_daily_mean_samples_the_base_load_like_the_sixty_minute_chain():
    # 2200 W house load, 700 W base load (excluded EV charger), 6 kWh forecast: score 4 before and after.
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    states = _night_horizon_states(2200, 6.0)
    states["sensor.opti_base_load_raw_w"] = 700
    states["sensor.opti_house_consumption_60min_w"] = 700  # supplied by the coordinator in the plant modes
    fresh = StrategyEngine()
    assert evaluate(fresh, states, _NIGHT_ATTRS, now).states[SUNNY] == "4"
    assert fresh.snapshot()["buckets"][DAILY][0][2] == 700.0  # sampled the base load, not the house load
    engine = _restored_engine(now, **{DAILY: _daily_load_buckets(now, 700)})
    result = evaluate(engine, states, _NIGHT_ATTRS, now)
    assert result.attributes[SUNNY]["load_source"] == "tagesmittel_24h"
    assert result.states[SUNNY] == "4"
    # Legacy plants never supply the base load: the house load is the fallback source.
    legacy = StrategyEngine()
    evaluate(legacy, _night_horizon_states(2200, 6.0), _NIGHT_ATTRS, now)
    assert legacy.snapshot()["buckets"][DAILY][0][2] == 2200.0
    # An unavailable primary source yields no sample instead of silently mixing semantics.
    states["sensor.opti_base_load_raw_w"] = "unavailable"
    unavailable = StrategyEngine()
    evaluate(unavailable, states, _NIGHT_ATTRS, now)
    assert DAILY not in unavailable.snapshot()["buckets"]


def test_configured_minimum_load_applies_to_the_daily_mean():
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    engine = _restored_engine(now, **{DAILY: _daily_load_buckets(now, 100)})
    states = _night_horizon_states(100, 2.48)
    attrs = {**_NIGHT_ATTRS, "sensor.opti_house_raw_w": {"forecast_min_load_w": 1000}}
    result = evaluate(engine, states, attrs, now)
    assert float(result.attributes[SUNNY]["tagesmittel_24h_w"]) == pytest.approx(100, abs=1)
    assert float(result.attributes[SUNNY]["tagesbedarf_w"]) == 1000
    assert result.states[SUNNY] == "1"  # 2.48 / 24 kWh; without the floor it would be 10
    assert result.states["binary_sensor.opti_pv_reichtag"] == "off"
    without_floor = evaluate(_restored_engine(now, **{DAILY: _daily_load_buckets(now, 100)}), states, _NIGHT_ATTRS, now)
    assert without_floor.states[SUNNY] == "10"


def test_ev_option_uses_the_ev_adjusted_daily_mean_and_reset_drops_it():
    now = dt.datetime(2026, 7, 27, 0, 5, tzinfo=TZ)
    engine = _restored_engine(now, **{DAILY: _daily_load_buckets(now, 2200), DAILY_EV: _daily_load_buckets(now, 700)})
    states = _night_horizon_states(2200, 2.48)
    states.update({"sensor.opti_forecast_house_instant_w": 700,
                   "input_boolean.opti_score_house_includes_ev": "on"})
    result = evaluate(engine, states, _NIGHT_ATTRS, now)
    assert result.states[SUNNY] == "1"
    assert float(result.attributes[SUNNY]["tagesmittel_24h_w"]) == pytest.approx(700, abs=1)
    states["sensor.opti_forecast_house_instant_w"] = "unavailable"
    invalid = evaluate(engine, states, _NIGHT_ATTRS, now + dt.timedelta(minutes=1))
    assert invalid.states[DAILY_EV] == "unavailable"
    assert invalid.attributes[SUNNY]["load_source"] == "legacy_60min_extrapolation"
    engine.reset_score_statistics()
    assert DAILY_EV not in engine.snapshot()["buckets"]
    assert DAILY in engine.snapshot()["buckets"]
    engine.reset_load_statistics()
    assert DAILY not in engine.snapshot()["buckets"]


def test_daily_statistics_snapshot_stays_small_and_restores_mean_and_coverage():
    resource = load_resources()
    resource["template_blocks"] = []
    resource["balancing_automations"] = []
    engine = StrategyEngine(resource)
    start = NOW - dt.timedelta(hours=25)
    for minute in range(25 * 60 + 1):
        engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500 + minute % 7,
                                        "sensor.opti_forecast_house_instant_w": 300 + minute % 5,
                                        "input_boolean.opti_score_house_includes_ev": "on"}),
                        solar_attrs(), start + dt.timedelta(minutes=minute))
    snapshot = engine.snapshot()
    for entity in (DAILY, DAILY_EV):
        assert entity not in snapshot["samples"]
        assert len(snapshot["buckets"][entity]) <= 26
    extra = json.dumps({key: snapshot["buckets"][key] for key in (DAILY, DAILY_EV)}, indent=2)
    assert len(extra.encode()) < 5000  # was ~200 kB with 2 x 1440 raw minute samples (HA JSON formatting)
    assert len(json.dumps(snapshot["samples"]["sensor.opti_house_consumption_60min_w"])) < 2000
    before = engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), NOW)
    restored = StrategyEngine(resource)
    restored.restore(snapshot)
    after = restored.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), NOW)
    for entity in (DAILY, DAILY_EV):
        assert after.states[entity] == before.states[entity]
        assert after.attributes[entity]["sample_count"] == before.attributes[entity]["sample_count"]
        assert after.attributes[entity]["coverage_ok"] is True
        assert after.attributes[entity]["sample_coverage_ratio"] >= 0.99
    # The raster continues after a restore and later snapshots merge new minutes into the hour buckets.
    # A restored bucket counts only while it lies entirely inside the window: once
    # its first sample expires the whole hour drops out (<= 1 h underhang, never
    # expired samples in mean or coverage). The raster still holds: no new
    # sample 30 s after the last one, one more a minute later.
    mid = restored.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), NOW + dt.timedelta(seconds=30))
    assert mid.attributes[DAILY]["sample_count"] == after.attributes[DAILY]["sample_count"] - 60
    later = restored.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), NOW + dt.timedelta(seconds=60))
    assert later.attributes[DAILY]["sample_count"] == mid.attributes[DAILY]["sample_count"] + 1
    assert later.attributes[DAILY]["sample_coverage_ratio"] >= 0.95
    assert later.attributes[DAILY]["coverage_ok"] is True
    merged = restored.snapshot()["buckets"][DAILY]
    assert len(merged) <= 26 and sum(row[3] for row in merged) == later.attributes[DAILY]["sample_count"]


def test_restore_never_reports_more_coverage_than_the_live_engine():
    """Alternating one hour of samples and one hour of gap must stay gated after a restore."""
    resource = load_resources()
    resource["template_blocks"] = []
    resource["balancing_automations"] = []
    engine = StrategyEngine(resource)
    start = NOW - dt.timedelta(hours=26, minutes=30)
    for minute in range(26 * 60 + 31):
        # 50 minutes of samples per two hours: ~42 % coverage, gaps of 70 minutes.
        load = 500 if minute % 120 < 50 else "unavailable"
        engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": load}), solar_attrs(),
                        start + dt.timedelta(minutes=minute))
    live = engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), NOW)
    restored = StrategyEngine(resource)
    restored.restore(engine.snapshot())
    after = restored.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), NOW)
    assert live.attributes[DAILY]["coverage_ok"] is False
    assert after.attributes[DAILY]["coverage_ok"] is False
    assert after.attributes[DAILY]["sample_coverage_ratio"] <= live.attributes[DAILY]["sample_coverage_ratio"]
    assert after.attributes[DAILY]["sample_count"] <= live.attributes[DAILY]["sample_count"]


def test_restore_keeps_sub_second_timestamps_so_no_extra_sample_becomes_due():
    """719 samples at hh:mm:00.4, evaluated 59.8 s after the last one: live and restored agree."""
    resource = load_resources()
    resource["template_blocks"] = []
    resource["balancing_automations"] = []
    engine = StrategyEngine(resource)
    start = NOW - dt.timedelta(hours=12) + dt.timedelta(milliseconds=400)
    last = start
    for minute in range(719):
        last = start + dt.timedelta(minutes=minute)
        engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), last)
    snapshot = engine.snapshot()
    probe = last + dt.timedelta(seconds=59.8)
    live = engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), probe)
    restored = StrategyEngine(resource)
    restored.restore(snapshot)
    after = restored.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), probe)
    # Live and restored engines must agree exactly (count, gate, mean); with the
    # raster tolerance both may take the 59.8 s sample, never only one of them.
    assert after.attributes[DAILY]["sample_count"] == live.attributes[DAILY]["sample_count"]
    assert after.attributes[DAILY]["coverage_ok"] is live.attributes[DAILY]["coverage_ok"]
    assert after.attributes[DAILY]["sample_coverage_ratio"] == live.attributes[DAILY]["sample_coverage_ratio"]
    assert after.states[DAILY] == live.states[DAILY]


@pytest.mark.parametrize("offset_us, probe_s", [(400400, 58.9998), (400000, 59.8), (999999, 59.0)])
def test_restore_agrees_with_live_at_any_sub_second_offset(offset_us, probe_s):
    """Rounding the persisted timestamps to any precision shifts the early-sample error, so none is allowed."""
    resource = load_resources()
    resource["template_blocks"] = []
    resource["balancing_automations"] = []
    engine = StrategyEngine(resource)
    start = NOW - dt.timedelta(hours=12) + dt.timedelta(microseconds=offset_us)
    last = start
    for minute in range(719):
        last = start + dt.timedelta(minutes=minute)
        engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), last)
    snapshot = json.loads(json.dumps(engine.snapshot()))
    probe = last + dt.timedelta(seconds=probe_s)
    live = engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), probe)
    restored = StrategyEngine(resource)
    restored.restore(snapshot)
    after = restored.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(), probe)
    assert after.attributes[DAILY]["sample_count"] == live.attributes[DAILY]["sample_count"]
    assert after.attributes[DAILY]["coverage_ok"] is live.attributes[DAILY]["coverage_ok"]
    assert after.states[DAILY] == live.states[DAILY]


@pytest.mark.parametrize("steps_per_hour", [7, 11])
def test_fractional_raster_steps_do_not_lose_samples(steps_per_hour):
    """Replay cadences of 3600/n s carry float noise; the raster must not skip samples."""
    resource = load_resources()
    resource["template_blocks"] = []
    resource["balancing_automations"] = []
    for definition in resource["statistics"]:
        if "sample_interval" in definition:
            definition["sample_interval"] = {"seconds": 3600 / steps_per_hour}
    engine = StrategyEngine(resource)
    start = NOW - dt.timedelta(hours=48)
    steps = 48 * steps_per_hour
    for index in range(steps + 1):
        result = engine.evaluate(measurements(**{"sensor.opti_house_consumption_w": 500}), solar_attrs(),
                                 start + dt.timedelta(seconds=index * 3600 / steps_per_hour))
    assert result.attributes[DAILY]["sample_coverage_ratio"] >= 0.99
    assert result.attributes[DAILY]["coverage_ok"] is True
