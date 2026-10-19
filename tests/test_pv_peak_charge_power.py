"""PV peaks release the forecast/SoC charge throttle while surplus would be exported."""
from __future__ import annotations

import datetime as dt

import pytest

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


def test_release_drops_when_surplus_disappears():
    engine = StrategyEngine()
    assert power(evaluate(engine, surplus())) == MAX_W
    gone = evaluate(engine, surplus(export=0, **{IMPORT: 100}), now=NOW + dt.timedelta(seconds=30))
    assert power(gone) == THROTTLED_W and not released(gone)
    # Re-entry needs the full threshold again.
    weak = evaluate(engine, surplus(export=100), now=NOW + dt.timedelta(seconds=60))
    assert power(weak) == THROTTLED_W


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
