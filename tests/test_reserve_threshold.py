"""Regression coverage for reserve entry and release hysteresis."""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "opti_reserve_threshold_engine",
    Path(__file__).parents[1] / "custom_components/opti_akku/engine.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
NOW = dt.datetime(2026, 1, 15, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))

BASE = {
    "sensor.opti_soc": "32",
    "sensor.opti_battery_capacity_kwh": "12.8",
    "sensor.opti_forecast_score": "5",
    "sensor.opti_forecast_score_tomorrow": "5",
    "sensor.opti_price_level": "NORMAL",
    "sensor.opti_target_soc": "60",
    "sensor.opti_price_current_ct_kwh": "30",
    "binary_sensor.opti_ueberschuss_70_aktiv": "off",
    "binary_sensor.opti_ueberschuss_ac_aktiv": "off",
    "binary_sensor.opti_ueberschuss_veto_aktiv": "off",
    "sensor.opti_peak_reserve_soc": "29.3",
    "binary_sensor.opti_peak_reserve_aktiv": "on",
    "binary_sensor.opti_winter_charging_allowed": "on",
    "input_number.minsoc": "10",
    "input_number.maxsoc": "95",
    "input_number.opti_einspeiseverguetung_ct": "8",
    "input_number.opti_netzlade_spread_ct": "10",
    "input_number.opti_halte_spread_ct": "3",
    "input_boolean.opti_prognose_netzladen": "off",
    "input_boolean.opti_pv_ueberschuss_ladung": "off",
    "input_select.akkusteuerung_modus": "Akku Dynamisch",
    "sun.sun": "below_horizon",
    "sensor.opti_balancing_watchdog": "aus",
    "input_boolean.opti_ev_akku_pause": "off",
    "binary_sensor.opti_ev_schnellladung": "off",
    "input_boolean.akku_opti_automatik": "on",
}
ATTRS = {
    "sensor.opti_peak_reserve_soc": {
        "reserve_ve_soc": 29.3,
        "min_preis_vor_peak_ct": 20.0,
        "peak_preis_avg_ct": 50.0,
        "peak_preis_ve_avg_ct": 50.0,
    }
}


@pytest.fixture
def engine():
    resource = _MODULE.load_resources()
    preview = next(
        definition
        for block in resource["template_blocks"]
        for definition in block.get("sensor", [])
        if definition["unique_id"] == "opti_strategie_vorschau"
    )
    resource["template_blocks"] = [{"sensor": [preview]}]
    resource["statistics"] = []
    resource["balancing_automations"] = []
    return _MODULE.StrategyEngine(resource)


def evaluate(engine, *, preview_matches=True, **overrides):
    states = {**BASE, **overrides}
    result = engine.evaluate(states, ATTRS, NOW)
    if preview_matches:
        assert result.states["sensor.opti_strategie_vorschau"] == result.mode
    return result


@pytest.mark.parametrize(
    "price, expected", [("NORMAL", "Akku Dynamisch"), ("EXPENSIVE", "Akku nur Entladen")]
)
def test_soc_32_above_29_3_reserve_is_released(engine, price, expected):
    assert evaluate(engine, **{"sensor.opti_price_level": price}).mode == expected


@pytest.mark.parametrize("price", ["NORMAL", "EXPENSIVE"])
def test_hold_starts_at_reserve_and_releases_above_two_percent(engine, price):
    price_state = {"sensor.opti_price_level": price}
    assert evaluate(engine, **price_state, **{"sensor.opti_soc": "29.3"}).mode == "Akku nur Laden"
    assert (
        evaluate(
            engine,
            **price_state,
            **{"sensor.opti_soc": "31.3", "input_select.akkusteuerung_modus": "Akku nur Laden"},
        ).mode
        == "Akku nur Laden"
    )
    expected = "Akku nur Entladen" if price == "EXPENSIVE" else "Akku Dynamisch"
    assert (
        evaluate(
            engine,
            **price_state,
            **{"sensor.opti_soc": "31.31", "input_select.akkusteuerung_modus": "Akku nur Laden"},
        ).mode
        == expected
    )


def test_very_expensive_and_ev_priorities_are_unchanged(engine):
    assert (
        evaluate(
            engine,
            **{"sensor.opti_soc": "20", "sensor.opti_price_level": "VERY_EXPENSIVE"},
        ).mode
        == "Akku nur Entladen"
    )
    assert (
        evaluate(
            engine,
            **{
                "sensor.opti_soc": "32",
                "sensor.opti_price_level": "VERY_EXPENSIVE",
                "input_boolean.opti_ev_akku_pause": "on",
                "binary_sensor.opti_ev_schnellladung": "on",
            },
        ).mode
        == "Akku nur Laden"
    )


def test_manual_mode_still_bypasses_strategy(engine):
    result = evaluate(engine, preview_matches=False, **{"input_boolean.akku_opti_automatik": "off"})
    assert result.mode == "Akku Pause"
    assert result.reason == "Opti-Automatik ausgeschaltet"
