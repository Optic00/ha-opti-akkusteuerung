"""PV peaks release the forecast/SoC charge throttle while surplus would be exported."""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from custom_components.opti_akku.coordinator import pv_peak_release_applies
from tests.test_engine import (
    MODE,
    NOW,
    POWER,
    SOC,
    TEMP,
    StrategyEngine,
    charge_power_branch_end,
    evaluate,
    measurements,
)

EXPORT = "sensor.opti_grid_export_w"
IMPORT = "sensor.opti_grid_import_w"
BATTERY = "sensor.opti_battery_power_w"
PV_CHARGING = "input_boolean.opti_pv_ueberschuss_ladung"
MAX_W = 3000  # helper default akkusteuerung_max_ladestaerke
THROTTLED_W = 2000  # score >= 5, SoC 50 %: 20 % of 10 kWh


def surplus(export=600, **overrides):
    """Default fixture: good forecast, SoC 50 %, preview 'Akku Dynamisch'."""
    return measurements(**{EXPORT: export, **overrides})


def power(result):
    return float(result.states[POWER])


def released(result):
    return result.attributes[POWER]["drossel_frei"] is True


def test_throttle_releases_on_real_net_surplus_below_85_percent():
    result = evaluate(states=surplus())
    assert result.mode == "Akku Dynamisch"
    assert power(result) == MAX_W
    assert released(result)
    assert "Drossel gelöst" in result.attributes[POWER]["branch"]
    assert charge_power_branch_end(result) == MAX_W


@pytest.mark.parametrize("states", [
    {EXPORT: 200},                      # threshold is exclusive
    {EXPORT: 700, IMPORT: 600},         # net surplus 100 W
    {EXPORT: 600, BATTERY: -500},       # discharging: surplus before battery 100 W
    {EXPORT: 600, SOC: 85},
    {EXPORT: 0},
], ids=["at_threshold", "import_offsets", "discharge_offsets", "soc_85", "no_export"])
def test_throttle_stays_without_sufficient_net_surplus(states):
    result = evaluate(states=surplus(**states))
    assert power(result) < MAX_W
    assert not released(result)
    assert "Drossel gelöst" not in result.attributes[POWER]["branch"]


def test_release_is_held_until_87_percent_or_no_surplus():
    engine = StrategyEngine()
    # No remaining forecast: target SoC = maxsoc, so the preview stays 'Akku Dynamisch'
    # above 70 % instead of switching to 'Akku nur Entladen'.
    high = {"sensor.opti_forecast_remaining_today_kwh": 0}
    assert released(evaluate(engine, surplus(**high)))
    # Held: surplus below the entry threshold but still positive, SoC under 87 %.
    held = evaluate(engine, surplus(export=50, **{SOC: 86.5}, **high), now=NOW + dt.timedelta(seconds=30))
    assert held.states["sensor.opti_strategie_vorschau"] == "Akku Dynamisch"
    assert power(held) == MAX_W and released(held)
    assert "(gehalten)" in held.attributes[POWER]["branch"]
    # Released at 87 %: back to the SoC step (score 0, SoC < 90 %: 20 % of 10 kWh).
    back = evaluate(engine, surplus(export=600, **{SOC: 87}, **high), now=NOW + dt.timedelta(seconds=60))
    assert power(back) == 2000 and not released(back)


def test_release_needs_a_pv_charging_preview():
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    # Above the target SoC the preview is 'Akku nur Entladen': no PV charging, no release.
    result = evaluate(engine, surplus(**{SOC: 80}), now=NOW + dt.timedelta(seconds=30))
    assert result.states["sensor.opti_strategie_vorschau"] == "Akku nur Entladen"
    assert power(result) == 1500 and not released(result)


def at(seconds):
    return NOW + dt.timedelta(seconds=seconds)


def test_release_drops_only_after_60_seconds_without_surplus():
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    gone = surplus(export=0, **{IMPORT: 100})
    # 30-second cadence: a dip is held, the timer starts at the first dip.
    first = evaluate(engine, gone, now=at(30))
    assert power(first) == MAX_W and released(first)
    assert first.attributes[POWER]["kein_ueberschuss_seit"] == at(30).isoformat()
    assert "ohne Überschuss seit 0 s" in first.attributes[POWER]["branch"]
    second = evaluate(engine, gone, now=at(60))
    assert power(second) == MAX_W and released(second)
    assert second.attributes[POWER]["kein_ueberschuss_seit"] == at(30).isoformat()
    # 60 s continuously without surplus: back to the throttle.
    third = evaluate(engine, gone, now=at(90))
    assert power(third) == THROTTLED_W and not released(third)
    assert third.attributes[POWER]["kein_ueberschuss_seit"] == ""
    # Re-entry needs the full threshold again.
    weak = evaluate(engine, surplus(export=100), now=at(120))
    assert power(weak) == THROTTLED_W


def test_short_dip_keeps_the_release_and_resets_the_timer():
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    dip = evaluate(engine, surplus(export=0), now=at(30))
    assert power(dip) == MAX_W and released(dip)
    back = evaluate(engine, surplus(export=50), now=at(60))
    assert power(back) == MAX_W and released(back)
    assert back.attributes[POWER]["kein_ueberschuss_seit"] == ""
    assert "(gehalten)" in back.attributes[POWER]["branch"]
    again = evaluate(engine, surplus(export=0), now=at(90))
    assert again.attributes[POWER]["kein_ueberschuss_seit"] == at(90).isoformat()
    assert power(again) == MAX_W


@pytest.mark.parametrize("states, expected", [
    ({TEMP: 45}, 1000), ({SOC: 87}, 800), ({PV_CHARGING: "off"}, THROTTLED_W),
    ({MODE: "Akku Netzladen"}, THROTTLED_W), ({"input_boolean.opti_manuelle_ladegrenze": "on", "input_number.akkusteuerung_max_ladestaerke": 2500}, 2500),
], ids=["temperature", "soc_87", "pv_charging_off", "grid_charge", "manual_limit"])
def test_blocking_conditions_end_the_release_immediately(states, expected):
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    result = evaluate(engine, surplus(export=0, **states), now=at(30))
    assert power(result) == expected and not released(result)


def test_latch_needs_an_immediately_preceding_valid_evaluation():
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    # Temperature missing: the sensor is unavailable, the latch template is skipped.
    outage = surplus(export=0)
    outage.pop(TEMP)
    for seconds in (30, 60, 90):
        result = evaluate(engine, outage, now=at(seconds))
        assert result.states[POWER] == "unavailable"
    # Back after 120 s with SoC 86 % and 50 W: no re-entry, the old release is stale.
    back = evaluate(engine, surplus(export=50, **{SOC: 86}), now=at(120))
    assert power(back) == 800 and not released(back)


def test_backward_clock_discards_the_latch():
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    result = evaluate(engine, surplus(export=50, **{SOC: 86}), now=at(-30))
    assert power(result) == 800 and not released(result)


def test_restored_latch_expires_after_90_seconds():
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    restored = StrategyEngine()
    restored.restore(engine.snapshot())
    stale = evaluate(restored, surplus(export=50, **{SOC: 86}), now=at(120))
    assert power(stale) == 800 and not released(stale)


@pytest.mark.parametrize("missing", [EXPORT, IMPORT, BATTERY])
def test_missing_measurements_keep_the_throttle(missing):
    states = surplus()
    states.pop(missing, None)
    result = evaluate(states=states)
    assert power(result) == THROTTLED_W and not released(result)


@pytest.mark.parametrize("missing", [EXPORT, IMPORT, BATTERY])
def test_missing_measurements_end_a_held_release(missing):
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    states = surplus(export=50, **{SOC: 86})
    states.pop(missing)
    result = evaluate(engine, states, now=NOW + dt.timedelta(seconds=30))
    assert power(result) < MAX_W and not released(result)


@pytest.mark.parametrize("temperature, expected", [(45, 1000), (0, 500), (-4, 500), (50, 0)])
def test_temperature_limits_are_not_lifted(temperature, expected):
    result = evaluate(states=surplus(**{TEMP: temperature}))
    assert power(result) == expected
    if expected:
        assert not released(result)


def test_release_respects_the_configured_maximum():
    result = evaluate(states=surplus(**{"input_number.akkusteuerung_max_ladestaerke": 2500}))
    assert power(result) == 2500 and released(result)


def test_pv_surplus_charging_switch_off_keeps_the_throttle():
    result = evaluate(states=surplus(**{PV_CHARGING: "off"}))
    assert power(result) == THROTTLED_W and not released(result)


def test_grid_charging_is_not_boosted_by_export():
    # Negative price with a poor forecast selects 'Akku Netzladen' (preview); the
    # bad-day step gives 3000 W, so a higher maximum shows that nothing is lifted.
    limit = {"input_number.akkusteuerung_max_ladestaerke": 5000}
    states = surplus(**limit, **{
        "sensor.opti_price_current_ct_kwh": -10,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
    })
    result = evaluate(states=states)
    assert result.mode == "Akku Netzladen"
    assert power(result) == 3000 and not released(result)
    # An active (manual or previous) grid charge keeps the throttle even with a PV preview.
    active = evaluate(states=surplus(**limit, **{MODE: "Akku Netzladen"}))
    assert active.states["sensor.opti_strategie_vorschau"] == "Akku Dynamisch"
    assert power(active) == THROTTLED_W and not released(active)
    # The same inputs without an active grid charge do release.
    assert power(evaluate(states=surplus(**limit))) == 5000


def test_downstream_pv_charge_modes_use_the_mode_independent_latch():
    # SoC 80 % above the target: preview 'nur Entladen', so the engine keeps the
    # throttle although the PV peak condition (pv_spitze) is met.
    result = evaluate(states=surplus(export=1600, **{SOC: 80}))
    assert result.states["sensor.opti_strategie_vorschau"] == "Akku nur Entladen"
    assert power(result) == 1500 and not released(result)
    assert result.attributes[POWER]["pv_spitze"] is True
    override = dataclasses.replace(result, mode="Akku nur Laden", decision_id="ev_preparation")
    assert pv_peak_release_applies(override, "Akku nur Laden", None, True)
    assert pv_peak_release_applies(dataclasses.replace(override, decision_id="arbitrage_hold"), "Akku Dynamisch", None, True)
    # Never for grid charging, manual modes, disabled strategy or other decisions.
    assert not pv_peak_release_applies(override, "Akku Netzladen", None, True)
    assert not pv_peak_release_applies(override, "Akku nur Laden", "Akku nur Laden", True)
    assert not pv_peak_release_applies(override, "Akku nur Laden", None, False)
    assert not pv_peak_release_applies(dataclasses.replace(override, decision_id="above_target"), "Akku nur Laden", None, True)
    # Without the PV peak condition (e.g. SoC 87 %) nothing is lifted downstream.
    blocked = evaluate(states=surplus(export=1600, **{SOC: 87}))
    assert blocked.attributes[POWER]["pv_spitze"] is False
    assert not pv_peak_release_applies(dataclasses.replace(blocked, decision_id="ev_preparation"), "Akku nur Laden", None, True)
