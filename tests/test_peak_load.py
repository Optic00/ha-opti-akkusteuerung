"""Profile inputs for active reserves; synthetic histories only."""

from copy import deepcopy
from datetime import UTC, timedelta, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.demand import DemandForecast
from custom_components.opti_akku.peak_load import peak_load_profile
from tests.test_demand import fixture, state, NOW, SETTINGS, update


def setup_profile():
    data, options, states = fixture()
    del options["demand_forecast"]["sources"]["heat_power"]
    options["demand_forecast"]["use_for_peak_reserve"] = True
    model = DemandForecast()
    update(model, data, options, states)
    rows = {}
    for d in range(1, 29):
        for hour in range(24):
            at = (NOW - timedelta(days=d)).replace(hour=hour)
            rows[at.isoformat()] = {"house_w": 500 if hour < 12 else 1000, "summer_mode": "on"}
    model.history.replace(rows, model.history_binding("fixture", options, UTC), NOW)
    return model, data, options, states


def project(model, data, options, states, now=NOW, timezone=UTC):
    return peak_load_profile(model, now, data, SETTINGS, options, states, timezone, "fixture")


def test_hourly_profile_with_margin_and_no_mutation():
    m, d, o, s = setup_profile()
    before = deepcopy(m.snapshot())
    result = project(m, d, o, s)
    assert result["status"] == "profile"
    assert result["hours"][str(int(NOW.timestamp()))] == 600
    assert result["hours"][str(int(NOW.replace(hour=15).timestamp()))] == 1200
    assert len(result["hours"]) == 37
    assert m.snapshot() == before


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "binding",
        "missing",
        "history_binding",
        "heat_meter",
        "water_due",
        "unknown_flag",
    ],
)
def test_untrusted_profile_falls_back(change):
    m, d, o, s = setup_profile()
    if change == "disabled":
        o["demand_forecast"]["use_for_peak_reserve"] = False
    if change == "binding":
        m.fingerprint = "other"
    if change == "missing":
        d["source_errors"] = {"house_consumption": "missing_or_stale"}
    if change == "history_binding":
        m.history.binding = "other"
    if change == "heat_meter":
        o["demand_forecast"]["sources"]["heat_power"] = "sensor.heat"
    if change == "water_due":
        o["demand_forecast"]["sources"]["dhw_due"] = "binary_sensor.due"
        s["binary_sensor.due"] = state("on", None)
        update(m, d, o, s)
    if change == "unknown_flag":
        s["binary_sensor.heating"] = state("unavailable", None)
    result = project(m, d, o, s)
    assert result["status"] in ("disabled", "fallback")
    assert not result["hours"] or set(result["hours"].values()) == {800}


def test_active_heating_not_counted_twice_but_current_demand_preserved():
    m, d, o, s = setup_profile()
    s["binary_sensor.heating"] = state("on", None)
    d["states"]["sensor.opti_house_consumption_w"] = 2100
    result = project(m, d, o, s)
    assert result["hours"][str(int(NOW.timestamp()))] == 2100
    assert result["hours"][str(int((NOW + timedelta(hours=3)).timestamp()))] == 600


def test_historical_expiry_and_missing_hour_use_fixed_load():
    m, d, o, s = setup_profile()
    m.history.rows = {
        k: v for k, v in m.history.rows.items() if datetime.fromisoformat(k).hour != NOW.hour
    }
    result = project(m, d, o, s)
    assert result["hours"][str(int(NOW.timestamp()))] == 800
    assert result["hours"][str(int((NOW + timedelta(hours=1)).timestamp()))] == 600


def test_today_is_never_a_training_day_for_future_slots():
    m, d, o, s = setup_profile()
    m.history.rows.clear()
    for offset in [0, 1]:
        day = (NOW - timedelta(days=offset)).date().isoformat()
        m.cells[f"{day}|6|summer"] = [100 * 3600, 0, 0, 3600]
    result = project(m, d, o, s)
    assert result["hours"][str(int((NOW + timedelta(days=1)).timestamp()))] == 800


def test_unquantified_water_deficit_keeps_fixed_assumption():
    m, d, o, s = setup_profile()
    o["demand_forecast"]["sources"].update(
        context_water_temperature="sensor.water", context_water_target="sensor.target"
    )
    s.update({"sensor.water": state(35, "°C"), "sensor.target": state(50, "°C")})
    m.history.binding = m.history_binding("fixture", o, UTC)
    result = project(m, d, o, s)
    assert result["reason"] == "unquantified_water_temperature_deficit"
    assert not result["hours"]


def test_dst_projection_has_distinct_utc_hours():
    m, d, o, s = setup_profile()
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 25, 0, tzinfo=UTC)
    update(m, d, o, s, now=now, tz=tz)
    result = project(m, d, o, s, now=now, timezone=tz)
    assert len(result["hours"]) == 37
    assert str(int(now.timestamp())) in result["hours"]
    assert str(int((now + timedelta(hours=1)).timestamp())) in result["hours"]


def test_restored_expired_online_cells_do_not_lower_active_reserve():
    m, d, o, s = setup_profile()
    m.history.rows.clear()
    for days in (49, 56, 63):
        day = (NOW - timedelta(days=days)).date().isoformat()
        m.cells[f"{day}|6|summer"] = [100 * 3600, 0, 0, 3600]
    assert project(m, d, o, s)["hours"][str(int(NOW.timestamp()))] == 800


@pytest.mark.parametrize(
    "actual, reason",
    [(35, "unquantified_water_temperature_deficit"), ("unavailable", "unknown_water_temperature")],
)
def test_primary_water_pair_also_guards_active_profile(actual, reason):
    m, d, o, s = setup_profile()
    o["demand_forecast"]["sources"].update(
        water_temperature="sensor.water", water_target="sensor.target"
    )
    s.update({"sensor.water": state(actual, "°C"), "sensor.target": state(50, "°C")})
    update(m, d, o, s)
    assert project(m, d, o, s)["reason"] == reason


def test_real_peak_template_integrates_hourly_loads_not_single_average():
    from custom_components.opti_akku.engine import StrategyEngine, load_resources

    resource = load_resources()
    resource["template_blocks"] = [
        b
        for b in resource["template_blocks"]
        if any(x.get("unique_id") == "opti_peak_reserve_soc" for x in b.get("sensor", []))
    ]
    resource["statistics"] = []
    resource["balancing_automations"] = []
    values = {
        "sensor.opti_battery_capacity_kwh": 10,
        "input_number.minsoc": 5,
        "input_number.maxsoc": 95,
        "input_number.opti_peak_verbrauch_kw": 0.8,
        "input_number.opti_peak_min_aufschlag_ct": 0,
        "sensor.opti_forecast_score": 5,
        "binary_sensor.opti_peak_horizont_lang": "off",
        "binary_sensor.opti_pv_reichtag": "on",
        "sun.sun": "below_horizon",
    }
    prices = [0] * 24
    prices[6], prices[7] = 50, 40
    attributes = {
        "sensor.opti_price_series": {"today": prices},
        "sun.sun": {"next_rising": (NOW + timedelta(hours=1)).isoformat()},
        "sensor.opti_peak_load_profile": {
            "hours": {
                str(int(NOW.timestamp())): 600,
                str(int((NOW + timedelta(hours=1)).timestamp())): 1200,
            },
            "methods": {
                str(int(NOW.timestamp())): "historical_profile",
                str(int((NOW + timedelta(hours=1)).timestamp())): "historical_profile",
            },
        },
    }
    result = StrategyEngine(resource).evaluate(values, attributes, NOW)
    a = result.attributes["sensor.opti_peak_reserve_soc"]
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    assert a["profile_hours"] == 2
    assert a["benoetigt_kwh"] == 2
    assert a["assumed_load_w"] == 900
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == 25
    attributes.pop("sensor.opti_peak_load_profile")
    fallback = StrategyEngine(resource).evaluate(values, attributes, NOW)
    assert fallback.attributes["sensor.opti_peak_reserve_soc"]["profile_hours"] == 0
    assert fallback.attributes["sensor.opti_peak_reserve_soc"]["benoetigt_kwh"] == 1.78
