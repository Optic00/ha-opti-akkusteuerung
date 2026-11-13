"""Expired slots must not bypass the precharge window (issue #175)."""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

from tests.battery_model_fixture import model_attributes, model_states


_SPEC = importlib.util.spec_from_file_location(
    "opti_precharge_window_engine",
    Path(__file__).parents[1] / "custom_components/opti_akku/engine.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
TZ = ZoneInfo("Europe/Berlin")


def evaluate(*, minute=45, slot_count=96, earlier=40.46, current=34.58, prices=None, level="NORMAL"):
    now = dt.datetime(2026, 10, 27, 8, minute, tzinfo=TZ)
    resource = _MODULE.load_resources()
    resource["template_blocks"] = [
        block for block in resource["template_blocks"]
        if any(sensor.get("unique_id") in {
            "opti_peak_reserve_soc", "opti_strategie_vorschau"
        } for sensor in block.get("sensor", []))
    ]
    # Keep the forecast scores as fixture inputs rather than deriving them
    # from the unrelated sensors sharing the preview's source block.
    for block in resource["template_blocks"]:
        block["sensor"] = [
            sensor for sensor in block.get("sensor", [])
            if sensor.get("unique_id") in {"opti_peak_reserve_soc", "opti_strategie_vorschau"}
        ]
    resource["statistics"] = []
    resource["balancing_automations"] = []
    prices_override = prices
    slots_per_hour = slot_count // 24
    prices = [35.0] * slot_count
    prices[8 * slots_per_hour] = earlier
    prices[8 * slots_per_hour + minute * slots_per_hour // 60] = current
    prices[12 * slots_per_hour] = 20.95
    prices[16 * slots_per_hour:] = [49.0] * (8 * slots_per_hour)
    if prices_override is not None:
        prices = list(prices_override)
    values = {
        "input_boolean.akku_opti_automatik": "on",
        "input_boolean.opti_prognose_netzladen": "on",
        "sensor.opti_soc": 12,
        "sensor.opti_battery_capacity_kwh": 10,
        "sensor.opti_price_current_ct_kwh": current,
        "sensor.opti_forecast_score": 0,
        "sensor.opti_forecast_score_tomorrow": 0,
        "sensor.opti_price_level": level,
        "binary_sensor.opti_peak_reserve_aktiv": "on",
        "input_number.minsoc": 5,
        "input_number.maxsoc": 95,
        "input_number.opti_peak_verbrauch_kw": 0.8,
        "input_number.opti_peak_min_aufschlag_ct": 10,
        "input_number.opti_netzlade_spread_ct": 10,
        "input_select.akkusteuerung_modus": "Akku Dynamisch",
        "sun.sun": "above_horizon",
        **model_states(),
    }
    result = _MODULE.StrategyEngine(resource).evaluate(
        values, {"sensor.opti_price_series": {"today": prices}, **model_attributes()}, now
    )
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    assert result.states["sensor.opti_strategie_vorschau"] == result.mode
    return result


@pytest.mark.parametrize("minute", [15, 30, 45])
def test_expired_morning_peak_keeps_future_cheaper_window(minute):
    result = evaluate(minute=minute)
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]
    assert attrs["min_preis_vor_peak_ct"] == 20.95
    assert result.decision_id != "peak_precharge"
    assert result.mode != "Akku Netzladen"
    # The full morning quarter-hour still contributes to the reserve (8.25 h).
    # Since issue #231 the 35-ct slots 12:15-16:00 count too: they beat the
    # 20.95-ct slot before them by the battery model's minimum (29.6 ct),
    # so precharging at 20.95 for them pays off (+3.75 h).
    assert attrs["peak_stunden_ve"] + attrs["peak_stunden_exp"] == 12.0
    assert attrs["benoetigt_kwh"] == 10.67


@pytest.mark.parametrize("minute", [0, 10, 14])
def test_unfinished_current_peak_still_counts_for_window(minute):
    result = evaluate(minute=minute, current=40.46)
    assert result.attributes["sensor.opti_peak_reserve_soc"]["min_preis_vor_peak_ct"] is None


def test_expired_cheap_slot_cannot_block_remaining_day_precharge():
    result = evaluate(earlier=1.0, current=20.95)
    assert result.attributes["sensor.opti_peak_reserve_soc"]["min_preis_vor_peak_ct"] == 20.95
    assert result.decision_id == "peak_precharge"
    assert result.mode == "Akku Netzladen"


def test_hourly_current_peak_is_not_expired_within_the_hour():
    result = evaluate(slot_count=24, current=40.46)
    assert result.attributes["sensor.opti_peak_reserve_soc"]["min_preis_vor_peak_ct"] is None


def test_current_cheap_window_can_precharge_before_future_peak():
    result = evaluate(current=20.95)
    assert result.decision_id == "peak_precharge"
    assert result.mode == "Akku Netzladen"


def _current_peak_prices():
    """Laufende Viertelstunde 55 ct ist selbst Peak, Stunden später kommen 41 ct."""
    prices = [41.0] * 96
    prices[32] = 55.0  # 08:00-08:15, der laufende Slot
    prices[64:] = [70.0] * 32
    return prices


def test_current_peak_slot_blocks_precharge_even_without_window_minimum():
    # Das Preisniveau ist hier bewusst NORMAL vorgegeben: Die Sperre kommt aus
    # der Peak-Klassifizierung des laufenden Slots, nicht nur aus dem Preisniveau.
    result = evaluate(minute=0, current=55.0, prices=_current_peak_prices())
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]
    assert attrs["min_preis_vor_peak_ct"] is None
    assert attrs["aktueller_slot_peak"] is True
    assert attrs["peak_preis_avg_ct"] - 55.0 >= 10
    assert result.decision_id != "peak_precharge"
    assert result.mode != "Akku Netzladen"


@pytest.mark.parametrize("level", ["EXPENSIVE", "VERY_EXPENSIVE"])
def test_expensive_price_level_blocks_precharge(level):
    result = evaluate(current=20.95, level=level)
    assert result.attributes["sensor.opti_peak_reserve_soc"]["aktueller_slot_peak"] is False
    assert result.decision_id != "peak_precharge"
    assert result.mode != "Akku Netzladen"


def test_cheap_slot_before_peak_is_not_marked_as_current_peak():
    result = evaluate(current=20.95)
    assert result.attributes["sensor.opti_peak_reserve_soc"]["aktueller_slot_peak"] is False
    assert result.decision_id == "peak_precharge"
