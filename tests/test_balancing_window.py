"""Absolutes Balancing-Haltefenster, Prioritäten und unveränderte Ladegrenzen."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from custom_components.opti_akku.definitions import NUMBER_DEFINITIONS
from custom_components.opti_akku.migration import MIGRATABLE
from tests.test_engine import (
    DONE_AT, DONE_VALID, NOW, POWER, SOC, StrategyEngine, evaluate, load_resources, measurements,
    solar_attrs,
)
from tests.test_engine_parity import BASE

HOURS = "input_number.opti_balancing_haltezeit_h"
WINDOW = "binary_sensor.opti_balancing_haltefenster_aktiv"
PREVIEW = "sensor.opti_strategie_vorschau"


def assert_parity(result):
    assert result.states[PREVIEW] == result.mode
    assert result.attributes[PREVIEW]["grund"] == result.reason
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"


def complete(hours=4):
    engine = StrategyEngine()
    states = measurements(**{SOC: 100, HOURS: hours, "input_number.maxsoc": 95})
    evaluate(engine, states)
    for minute in range(1, 31):
        result = evaluate(engine, states, now=NOW + dt.timedelta(minutes=minute))
    assert result.states[DONE_VALID] == "on"
    assert result.states[DONE_AT] == (NOW + dt.timedelta(minutes=30)).isoformat()
    assert result.states["sensor.opti_balancing_watchdog"] == "aus"
    return engine, states, result


def test_setting_is_opt_in_on_both_layers_and_not_implicitly_migrated():
    definition = NUMBER_DEFINITIONS[HOURS]
    assert (definition["min"], definition["max"], definition["step"], definition["default"]) == (0, 6, 0.5, 0)
    assert definition["unit_of_measurement"] == "h"
    assert definition["icon"] == "mdi:battery-clock"
    resource = load_resources()
    assert resource["helper_defaults"][HOURS] == 0
    for key in ("min", "max", "step", "unit_of_measurement", "icon"):
        assert resource["helper_definitions"][HOURS][key] == definition[key]
    # Legacy import is a fixed allowlist, not a setting registry. Do not enable
    # a new control feature by importing a similarly named external helper.
    assert HOURS not in MIGRATABLE


def test_default_zero_keeps_ladedeckel_after_confirmed_completion():
    _, _, result = complete(hours=0)
    assert result.states[WINDOW] == "off"
    assert result.decision_id == "maximum_soc"
    assert result.mode == "Akku nur Entladen"
    assert_parity(result)


def test_confirmed_completion_holds_until_exact_absolute_deadline():
    engine, states, result = complete()
    start = dt.datetime.fromisoformat(result.states[DONE_AT])
    assert result.decision_id == "balancing_hold"
    assert result.mode == "Akku nur Laden"
    assert result.reason == "Balancing-Watchdog (Haltefenster)"
    assert result.attributes[WINDOW]["rest_minuten"] == 240
    assert dt.datetime.fromisoformat(result.attributes[WINDOW]["ende"]) == start + dt.timedelta(hours=4)
    assert dt.datetime.fromisoformat(result.attributes[WINDOW]["abschluss"]) == start
    assert_parity(result)
    for elapsed, expected in [(dt.timedelta(hours=4, seconds=-1), "balancing_hold"),
                              (dt.timedelta(hours=4), "maximum_soc"),
                              (dt.timedelta(hours=4, minutes=1), "maximum_soc")]:
        result = evaluate(engine, states, now=start + elapsed)
        assert result.decision_id == expected
        assert result.states[WINDOW] == ("on" if expected == "balancing_hold" else "off")
        assert_parity(result)


def test_restart_and_soc_jumps_never_extend_deadline():
    engine, states, first = complete()
    start = dt.datetime.fromisoformat(first.states[DONE_AT])
    restored = StrategyEngine()
    restored.restore(json.loads(json.dumps(engine.snapshot())))
    after = evaluate(restored, states, now=start + dt.timedelta(hours=2))
    assert after.decision_id == "balancing_hold"
    assert after.states[DONE_AT] == first.states[DONE_AT]
    assert after.attributes[WINDOW]["ende"] == first.attributes[WINDOW]["ende"]
    assert after.attributes[WINDOW]["rest_minuten"] == 120
    dip = evaluate(restored, {**states, SOC: 89.9}, now=start + dt.timedelta(hours=2, minutes=1))
    assert dip.states[WINDOW] == "off"
    assert dip.attributes[WINDOW]["abbruch_grund"] == "SoC unter 90 %"
    assert dip.decision_id != "balancing_hold"
    back = evaluate(restored, states, now=start + dt.timedelta(hours=3))
    assert back.decision_id == "balancing_hold"
    assert back.attributes[WINDOW]["rest_minuten"] == 60
    assert back.attributes[WINDOW]["ende"] == first.attributes[WINDOW]["ende"]
    assert back.attributes[WINDOW]["abbruch_grund"] == ""
    expired = evaluate(restored, states, now=start + dt.timedelta(hours=4, minutes=1))
    assert expired.states[DONE_AT] == first.states[DONE_AT]
    assert expired.decision_id == "maximum_soc"
    assert_parity(expired)


@pytest.mark.parametrize("changes", [
    {DONE_VALID: "off"},
    {DONE_AT: "invalid"},
    {DONE_AT: "unavailable"},
    {DONE_AT: ""},
    {DONE_AT: (NOW + dt.timedelta(seconds=1)).isoformat()},
    {SOC: "unknown"}, {SOC: "unavailable"}, {SOC: "nan"}, {SOC: "inf"}, {SOC: 101},
])
def test_missing_invalid_or_future_inputs_never_activate(changes):
    result = evaluate(states=measurements(**{
        SOC: 100, HOURS: 4, DONE_VALID: "on", DONE_AT: NOW.isoformat(), **changes,
    }))
    assert result.states[WINDOW] == "off"
    assert result.decision_id != "balancing_hold"
    assert result.attributes[WINDOW]["abbruch_grund"] == ""
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"


@pytest.mark.parametrize("soc,active", [(89.9, False), (90, True), (100, True)])
def test_soc_boundary(soc, active):
    result = evaluate(states=measurements(**{
        SOC: soc, HOURS: 4, DONE_VALID: "on", DONE_AT: NOW.isoformat(),
    }))
    assert result.states[WINDOW] == ("on" if active else "off")
    assert result.attributes[WINDOW]["abbruch_grund"] == ("" if active else "SoC unter 90 %")
    assert_parity(result)


@pytest.mark.parametrize("changes,expected", [
    ({SOC: 90, "input_number.minsoc": 90}, "minimum_soc"),
    ({SOC: 91, "input_number.minsoc": 90,
      "binary_sensor.opti_minsoc_schutz_aktiv": "on"}, "minimum_soc"),
    ({SOC: 91, "input_number.minsoc": 90,
      "binary_sensor.opti_minsoc_schutz_aktiv": "on",
      "sensor.opti_balancing_watchdog": "netz"}, "balancing_grid"),
    ({SOC: 90, "binary_sensor.opti_peak_reserve_aktiv": "on",
      "sensor.opti_peak_reserve_soc": 95}, "peak_precharge"),
    ({SOC: 90, "sensor.opti_forecast_score": 1,
      "sensor.opti_price_current_ct_kwh": -1}, "negative_price"),
    ({"binary_sensor.opti_peak_reserve_aktiv": "on",
      "sensor.opti_price_level": "VERY_EXPENSIVE"}, "peak_l1"),
    ({"binary_sensor.opti_peak_reserve_aktiv": "on",
      "sensor.opti_price_level": "EXPENSIVE", "sensor.opti_peak_reserve_soc": 30}, "peak_l2"),
    ({"sensor.opti_balancing_watchdog": "pv"}, "balancing_pv"),
    ({"sensor.opti_balancing_watchdog": "netz"}, "balancing_grid"),
])
def test_higher_priority_still_wins_with_active_window(changes, expected):
    resources = load_resources()
    for block in resources["template_blocks"]:
        for domain in ("sensor", "binary_sensor"):
            block[domain] = [item for item in block.get(domain, [])
                             if f"{domain}.{item['unique_id']}" in (WINDOW, PREVIEW)]
    resources["statistics"] = {}
    resources["balancing_automations"] = []
    result = StrategyEngine(resources).evaluate({
        **BASE, SOC: 100, HOURS: 4, DONE_VALID: "on", DONE_AT: NOW.isoformat(),
        "input_boolean.akku_opti_automatik": "on", **changes,
    }, {"sensor.opti_peak_reserve_soc": {"reserve_ve_soc": 30,
                                          "min_preis_vor_peak_ct": 30,
                                          "peak_preis_avg_ct": 100},
        "binary_sensor.opti_minsoc_schutz_aktiv": {"release_soc": 93}}, NOW)
    assert result.states[WINDOW] == "on"
    assert result.decision_id == expected
    assert_parity(result)


@pytest.mark.parametrize("soc,temperature,limit,manual,expected", [
    (97, 25, 3000, "off", 200),
    (97, 25, 3000, "on", 200),
    (92, 25, 3000, "off", 500),
    (97, 25, 100, "off", 100),
    (97, 50, 3000, "off", 0),
])
def test_window_keeps_balancing_taper_and_stricter_limits(soc, temperature, limit, manual, expected):
    result = evaluate(states=measurements(**{
        SOC: soc, HOURS: 4, DONE_VALID: "on", DONE_AT: NOW.isoformat(),
        "sensor.opti_battery_temp": temperature,
        "input_number.akkusteuerung_max_ladestaerke": limit,
        "input_boolean.opti_manuelle_ladegrenze": manual,
    }))
    assert result.decision_id == "balancing_hold"
    assert float(result.states[POWER]) == expected
    branch = result.attributes[POWER]["branch"]
    if temperature < 50 and limit > expected:
        assert "Balancing ab" in branch
    assert branch.endswith(f"Ergebnis {expected} W")
    assert_parity(result)


@pytest.mark.parametrize("date", [(2026, 3, 29), (2026, 10, 25)])
def test_deadline_is_four_elapsed_hours_across_clock_change(date):
    start = NOW.replace(year=date[0], month=date[1], day=date[2], hour=0)
    states = measurements(**{SOC: 100, HOURS: 4, DONE_VALID: "on", DONE_AT: start.isoformat()})
    engine = StrategyEngine()
    before = evaluate(engine, states, now=start)
    deadline = start.astimezone(dt.UTC) + dt.timedelta(hours=4)
    assert dt.datetime.fromisoformat(before.attributes[WINDOW]["ende"]) == deadline
    for seconds, active in [(-1, True), (0, False), (60, False)]:
        now = (deadline + dt.timedelta(seconds=seconds)).astimezone(start.tzinfo)
        result = evaluate(engine, states, now=now)
        assert result.states[WINDOW] == ("on" if active else "off")
        assert_parity(result)


def test_active_window_does_not_apply_taper_when_peak_wins():
    now = NOW.replace(hour=19)
    states = measurements(**{
        SOC: 97, HOURS: 4, DONE_VALID: "on", DONE_AT: now.isoformat(),
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
    assert result.states[WINDOW] == "on"
    assert result.decision_id == "peak_l1"
    assert float(result.states[POWER]) == 500  # Ordinary 0.05C; hold did not win.
    assert_parity(result)
