"""Peak ladder L1/L2 lets daytime PV surplus charge instead of exporting it."""
from __future__ import annotations

import pytest

from tests.test_engine_parity import BASE, NOW, StrategyEngine, load_resources

PREVIEW = "sensor.opti_strategie_vorschau"


@pytest.fixture
def engine():
    resource = load_resources()
    preview = next(
        definition for block in resource["template_blocks"]
        for definition in block.get("sensor", [])
        if definition["unique_id"] == "opti_strategie_vorschau"
    )
    resource["template_blocks"] = [{"sensor": [preview]}]
    resource["statistics"] = []
    resource["balancing_automations"] = []
    return StrategyEngine(resource)


def evaluate(engine, level, attrs=None, **overrides):
    # 19.10.2026, 16:30-18:13: SoC stuck at 53 % in 'nur Entladen' while exporting.
    states = {**BASE, "input_boolean.akku_opti_automatik": "on",
              "sun.sun": "above_horizon", "sensor.opti_soc": "53",
              "input_number.minsoc": "5", "sensor.opti_price_level": level,
              "binary_sensor.opti_peak_reserve_aktiv": "on",
              "sensor.opti_peak_reserve_soc": "60", **overrides}
    attrs = {"sensor.opti_peak_reserve_soc": {"reserve_ve_soc": 27.6}, **(attrs or {})}
    result = engine.evaluate(states, attrs, NOW)
    assert result.states[PREVIEW] == result.mode
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    return result


@pytest.mark.parametrize("level, decision, reason", [
    ("VERY_EXPENSIVE", "peak_l1", "Peak-Leiter L1 (VE entladen, PV-Laden frei)"),
    ("EXPENSIVE", "peak_l2", "Peak-Leiter L2 (EXP entladen, ueber VE-Reserve 27.6%, PV-Laden frei)"),
])
def test_daytime_peak_discharge_keeps_pv_charging_open(engine, level, decision, reason):
    result = evaluate(engine, level)
    assert result.decision_id == decision
    assert result.mode == "Akku Dynamisch"
    assert result.reason == reason


@pytest.mark.parametrize("level", ["VERY_EXPENSIVE", "EXPENSIVE"])
@pytest.mark.parametrize("overrides", [
    {"sun.sun": "below_horizon"},
    {"input_boolean.opti_pv_ueberschuss_ladung": "off"},
    {"sensor.opti_soc": "95"},
    {"sensor.opti_soc": "93", "binary_sensor.opti_ladedeckel_aktiv": "on"},  # held cap band
    {"input_number.maxsoc": "unavailable"},
], ids=["night", "pv_charging_off", "at_cap", "cap_band", "maxsoc_missing"])
def test_peak_discharge_stays_charge_locked_otherwise(engine, level, overrides):
    attrs = {"binary_sensor.opti_ladedeckel_aktiv": {"maxsoc": 95}}
    result = evaluate(engine, level, attrs, **overrides)
    assert result.decision_id in ("peak_l1", "peak_l2")
    assert result.mode == "Akku nur Entladen"
    assert "PV-Laden frei" not in result.reason


def test_cap_band_releases_below_hysteresis(engine):
    attrs = {"binary_sensor.opti_ladedeckel_aktiv": {"maxsoc": 95}}
    result = evaluate(engine, "VERY_EXPENSIVE", attrs, **{
        "sensor.opti_soc": "91.9", "binary_sensor.opti_ladedeckel_aktiv": "on"})
    assert result.mode == "Akku Dynamisch"


@pytest.mark.parametrize("overrides, decision, mode", [
    ({"binary_sensor.opti_extreme_price_hold": "on"}, "extreme_peak_hold", "Akku nur Laden"),
    ({"input_boolean.opti_ev_akku_pause": "on", "binary_sensor.opti_ev_schnellladung": "on"},
     "ev_lock", "Akku nur Laden"),
    ({"sensor.opti_soc": "5"}, "minimum_soc", "Akku nur Laden"),
])
def test_protection_still_wins_over_daytime_peak(engine, overrides, decision, mode):
    result = evaluate(engine, "VERY_EXPENSIVE", **overrides)
    assert result.decision_id == decision
    assert result.mode == mode
