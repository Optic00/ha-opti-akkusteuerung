"""Engine fixture for the peak reserve and precharge economics (Paket 2a).

14.01.2026, Europe/Berlin, 96 quarter-hour prices for today only, no sun
attributes, 12.8 kWh, MinSOC 5, MaxSOC 95, 0.8 kW, surcharge 10 ct, fixed
spread 10 ct, grid charging allowed, peak reserve active, both scores 0,
price level NORMAL, SoC 12, previous mode ``Akku Dynamisch``, 13:05.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

_SPEC = importlib.util.spec_from_file_location(
    "opti_precharge_fixture_engine",
    Path(__file__).parents[1] / "custom_components/opti_akku/engine.py",
)
ENGINE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ENGINE
_SPEC.loader.exec_module(ENGINE)
TZ = ZoneInfo("Europe/Berlin")
MODEL_ENTITY = "sensor.opti_battery_model"
_UNIQUE_IDS = {"opti_peak_reserve_soc", "opti_strategie_vorschau"}


def day_prices(base, valley, peaks, extra=()):
    """96 quarter-hour prices: base, 12-15 h valley, ``peaks`` as ((from_h, to_h), price)."""
    prices = [float(base)] * 96
    prices[12 * 4:15 * 4] = [float(valley)] * 12
    for (start, end), price in peaks:
        prices[start * 4:end * 4] = [float(price)] * ((end - start) * 4)
    for index, price in extra:
        prices[index] = float(price)
    return prices


def resources():
    resource = ENGINE.load_resources()
    resource["template_blocks"] = [
        block for block in resource["template_blocks"]
        if any(sensor.get("unique_id") in _UNIQUE_IDS for sensor in block.get("sensor", []))
    ]
    # Keep scores and other derived sensors as fixture inputs.
    for block in resource["template_blocks"]:
        block["sensor"] = [
            sensor for sensor in block.get("sensor", []) if sensor.get("unique_id") in _UNIQUE_IDS
        ]
    resource["statistics"] = []
    resource["balancing_automations"] = []
    return resource


def evaluate(prices, *, current, model=None, minute=5, hour=13, overrides=None, tomorrow=None,
             check=True, attributes=None):
    """Evaluate the reserve and preview; ``model`` is ``(state, attributes)`` or ``None``.

    ``attributes`` adds further input attributes, e.g. the PV forecast hours.
    """
    now = dt.datetime(2026, 1, 14, hour, minute, tzinfo=TZ)
    values = {
        "input_boolean.akku_opti_automatik": "on",
        "input_boolean.opti_prognose_netzladen": "on",
        "sensor.opti_soc": 12,
        "sensor.opti_battery_capacity_kwh": 12.8,
        "sensor.opti_price_current_ct_kwh": current,
        "sensor.opti_forecast_score": 0,
        "sensor.opti_forecast_score_tomorrow": 0,
        "sensor.opti_price_level": "NORMAL",
        "binary_sensor.opti_peak_reserve_aktiv": "on",
        "input_number.minsoc": 5,
        "input_number.maxsoc": 95,
        "input_number.opti_peak_verbrauch_kw": 0.8,
        "input_number.opti_peak_min_aufschlag_ct": 10,
        "input_number.opti_netzlade_spread_ct": 10,
        "input_select.akkusteuerung_modus": "Akku Dynamisch",
        "sun.sun": "below_horizon",
    }
    series = {"today": list(prices)}
    if tomorrow is not None:
        series["tomorrow"] = list(tomorrow)
    attributes_in = {"sensor.opti_price_series": series}
    if model is not None:
        values[MODEL_ENTITY], model_attributes = model
        if model_attributes is not None:
            attributes_in[MODEL_ENTITY] = dict(model_attributes)
    values.update(overrides or {})
    for entity, extra in (attributes or {}).items():
        attributes_in[entity] = dict(extra)
    result = ENGINE.StrategyEngine(resources()).evaluate(values, attributes_in, now)
    if check:
        assert result.states["sensor.opti_engine_diagnostics"] == "ok"
        assert result.states["sensor.opti_strategie_vorschau"] == result.mode
    return result
