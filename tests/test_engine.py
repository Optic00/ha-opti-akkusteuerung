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


def test_minsoc_precedes_price_and_forecast_rules():
    result = evaluate(states=measurements(**{
        SOC: 5, "sensor.opti_price_current_ct_kwh": -10,
        "sensor.opti_forecast_remaining_today_kwh": 0,
    }))
    assert result.mode == "Akku nur Laden"
    assert result.reason.startswith("MinSOC")


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


def test_selected_balancing_branch_applies_high_soc_charge_taper():
    result = evaluate(states=measurements(**{
        SOC: 97, DAYS: 20, "input_number.opti_balancing_intervall_tage": 14,
        "input_boolean.opti_prognose_netzladen": "off",
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
        (95, "Akku nur Entladen", "Ladedeckel"),
        (96, "Akku nur Entladen", "Ladedeckel"),
        (98, "Akku nur Entladen", "Ladedeckel"),
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
        (95, "on", "Akku nur Entladen"),
        (94, "on", "Akku nur Entladen"),
    ]):
        states, attributes = peak_maxsoc_inputs(soc)
        result = evaluate(engine, states, attributes, NOW + dt.timedelta(minutes=minute))
        assert result.states[DECKEL] == expected_deckel
        assert result.mode == expected_mode

    restored = StrategyEngine()
    restored.restore(json.loads(json.dumps(engine.snapshot(), allow_nan=False)))
    for minute, (soc, expected_deckel, expected_mode) in enumerate([
        (92, "on", "Akku nur Entladen"),
        (91.9, "off", "Akku Netzladen"),
    ], start=2):
        states, attributes = peak_maxsoc_inputs(soc)
        result = evaluate(restored, states, attributes, NOW + dt.timedelta(minutes=minute))
        assert result.states[DECKEL] == expected_deckel
        assert result.mode == expected_mode


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
