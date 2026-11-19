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


def test_ev_cleaned_profile_can_drive_peak_reserve_without_gross_house_floor():
    model, data, options, states = setup_profile()
    prior_rows = deepcopy(model.history.rows)
    options["sources"] = {"ev1_power": "sensor.ev"}
    options["sources"]["ev1_charging"] = "binary_sensor.ev"
    states["binary_sensor.ev"] = state("on", None)
    options["demand_forecast"]["house_includes_ev"] = True
    states["sensor.ev"] = state(1, "kW")
    states["binary_sensor.heating"] = state("on", None)
    data["states"]["sensor.opti_house_consumption_w"] = 2100
    update(model, data, options, states)
    model.history.replace(prior_rows, model.history_binding("fixture", options, UTC), NOW)

    result = project(model, data, options, states)

    assert result["status"] == "profile"
    assert result["hours"][str(int(NOW.timestamp()))] == 1100


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


# --- #248: separate heat pump meter and planned hot-water runs ---------------


def setup_meter_profile(dhw_hour=11, heat_w=0, dhw_w=1200, base_w=500):
    """28 synthetic days: base load, optional space heating, one hot-water hour."""
    data, options, states = fixture()
    cfg = options["demand_forecast"]
    cfg["use_for_peak_reserve"] = True
    cfg["dhw_cycle_kwh"] = 1.2
    model = DemandForecast()
    update(model, data, options, states)
    model.cells.clear()
    for d in range(1, 29):
        day = (NOW - timedelta(days=d)).date().isoformat()
        for hour in range(24):
            dhw = dhw_w if hour == dhw_hour else 0
            model.cells[f"{day}|{hour}|summer"] = [
                base_w * 3600, (heat_w + dhw) * 3600, dhw * 3600, 3600
            ]
    return model, data, options, states


def at_hour(hour, days=0):
    return str(int((NOW.replace(hour=hour) + timedelta(days=days)).timestamp()))


def plan(options, states, start, minutes=75, entity="sensor.dhw_start", **attrs):
    options["demand_forecast"]["dhw_planned_start"] = entity
    options["demand_forecast"]["dhw_run_minutes"] = minutes
    value = start.isoformat() if isinstance(start, datetime) else start
    states[entity] = state(value, None, **attrs)


def test_separate_meter_uses_profile_with_space_heating_instead_of_fixed_load():
    m, d, o, s = setup_meter_profile(heat_w=300)
    result = project(m, d, o, s)
    assert result["status"] == "profile"
    assert result["thermal_model"].startswith("separate_meter")
    assert result["planned_dhw"] == {"status": "not_configured"}
    # Base 500 W plus space heating 300 W, 20 % margin.
    assert result["hours"][at_hour(9)] == 960
    # Without a planned run the learned hot-water share stays where it was learned.
    assert result["hours"][at_hour(11)] == (500 + 300 + 1200) * 1.2
    assert result["methods"][at_hour(11)] == "measured_profile"


def test_space_heating_median_is_taken_per_cell_not_as_difference_of_medians():
    m, d, o, s = setup_meter_profile(dhw_w=0)
    # Hot water on only some days: subtracting separate medians would mix days.
    for d_ in (7, 14):
        day = (NOW - timedelta(days=d_)).date().isoformat()
        m.cells[f"{day}|11|summer"] = [500 * 3600, 1500 * 3600, 1500 * 3600, 3600]
    for d_ in (21, 28):
        day = (NOW - timedelta(days=d_)).date().isoformat()
        m.cells[f"{day}|11|summer"] = [500 * 3600, 400 * 3600, 0, 3600]
    later = datetime(2026, 9, 14, 11, tzinfo=UTC)
    assert m.expected_house(later, "summer", UTC, before=NOW) == 1450
    assert m.expected_house(later, "summer", UTC, before=NOW, without_dhw=True) == 700


def test_planned_run_is_booked_where_it_runs_and_learned_slot_is_freed():
    m, d, o, s = setup_meter_profile()
    plan(o, s, NOW.replace(hour=15))
    result = project(m, d, o, s)
    assert result["planned_dhw"]["status"] == "booked"
    assert result["planned_dhw"]["kwh"] == 1.2
    # The old learned 11:00 slot no longer carries the run before the planned end.
    assert result["hours"][at_hour(11)] == 600
    assert result["methods"][at_hour(11)] == "measured_profile_without_dhw"
    # 1.2 kWh over 75 min: 60 min in hour 15, 15 min in hour 16.
    assert result["hours"][at_hour(15)] == pytest.approx((500 + 960) * 1.2)
    assert result["hours"][at_hour(16)] == pytest.approx((500 + 240) * 1.2)
    assert result["methods"][at_hour(15)] == "measured_profile_without_dhw+planned_dhw"
    # After the planned run the next one is unknown: the learned share returns.
    assert result["hours"][at_hour(11, days=1)] == (500 + 1200) * 1.2
    assert result["methods"][at_hour(11, days=1)] == "measured_profile"


@pytest.mark.parametrize(
    "start, attrs, status",
    [
        ("unavailable", {}, "unknown_source"),
        ("not a time", {}, "unknown_source"),
        (NOW - timedelta(hours=3), {}, "stale"),
        (NOW + timedelta(hours=25), {}, "too_far"),
        ("15:00:00", {"has_date": False, "has_time": True}, "unknown_source"),
        ("x", {"has_date": True, "has_time": True, "timestamp": 1e20}, "unknown_source"),
    ],
)
def test_unusable_plan_keeps_learned_hot_water_share(start, attrs, status):
    m, d, o, s = setup_meter_profile()
    plan(o, s, start, **attrs)
    result = project(m, d, o, s)
    assert result["planned_dhw"]["status"] == status
    assert result["status"] == "profile"
    assert result["hours"][at_hour(11)] == (500 + 1200) * 1.2
    assert not any("planned" in method for method in result["methods"].values())


def test_plan_requires_meter_hot_water_flag_and_cycle_energy():
    m, d, o, s = setup_meter_profile()
    plan(o, s, NOW.replace(hour=15))
    o["demand_forecast"]["dhw_cycle_kwh"] = 0
    result = project(m, d, o, s)
    assert result["planned_dhw"]["status"] == "requires_meter_and_cycle_energy"
    assert result["hours"][at_hour(11)] == (500 + 1200) * 1.2
    o["demand_forecast"]["dhw_cycle_kwh"] = 1.2
    o["demand_forecast"]["dhw_run_minutes"] = 5
    assert project(m, d, o, s)["planned_dhw"]["status"] == "invalid_duration"


def test_input_datetime_and_naive_local_times_are_accepted():
    tz = ZoneInfo("Europe/Berlin")
    m, d, o, s = setup_meter_profile()
    m.fingerprint = m.profile_binding("fixture", o, tz)
    start = NOW.replace(hour=15)
    plan(o, s, "2026-09-14 17:00:00", entity="input_datetime.dhw",
         has_date=True, has_time=True, timestamp=start.timestamp())
    assert project(m, d, o, s, timezone=tz)["planned_dhw"]["start"] == start.isoformat()
    plan(o, s, "2026-09-14T17:00:00")
    assert project(m, d, o, s, timezone=tz)["planned_dhw"]["start"] == start.isoformat()


def test_booked_plan_quantifies_due_hot_water_but_unplanned_due_keeps_fallback():
    m, d, o, s = setup_meter_profile()
    o["demand_forecast"]["sources"]["dhw_due"] = "binary_sensor.due"
    s["binary_sensor.due"] = state("on", None)
    m.fingerprint = m.profile_binding("fixture", o, UTC)
    assert project(m, d, o, s)["reason"] == "unquantified_dhw_due"
    plan(o, s, NOW.replace(hour=8))
    result = project(m, d, o, s)
    assert result["status"] == "profile"
    assert "+planned_dhw" in result["methods"][at_hour(8)]


def test_running_planned_cycle_is_not_counted_twice():
    m, d, o, s = setup_meter_profile()
    plan(o, s, NOW - timedelta(minutes=15))
    s["binary_sensor.dhw"] = state("on", None)
    d["states"]["sensor.opti_house_consumption_w"] = 2500
    result = project(m, d, o, s)
    assert result["planned_dhw"]["status"] == "booked"
    # Floor of the measured house load, not house load plus the booked cycle.
    assert result["hours"][at_hour(6)] == 2500
    assert result["methods"][at_hour(6)].endswith("+active_heat_floor")


def test_plan_keys_never_reset_learned_profile_or_history():
    _, _, options, _ = setup_meter_profile()
    before = (
        DemandForecast.profile_binding("fixture", options, UTC),
        DemandForecast.history_binding("fixture", options, UTC),
    )
    options["demand_forecast"].update(dhw_planned_start="sensor.dhw_start", dhw_run_minutes=60)
    assert before == (
        DemandForecast.profile_binding("fixture", options, UTC),
        DemandForecast.history_binding("fixture", options, UTC),
    )


def test_learning_hours_add_planned_cycle_to_fixed_load():
    m, d, o, s = setup_meter_profile()
    m.cells.clear()
    plan(o, s, NOW.replace(hour=15), minutes=60)
    result = project(m, d, o, s)
    assert result["hours"][at_hour(15)] == 800 + 1200
    assert result["methods"][at_hour(15)] == "fixed_setting+planned_dhw"
    assert result["hours"][at_hour(14)] == 800


def test_plan_status_is_shown_when_fixed_load_is_forced():
    m, d, o, s = setup_meter_profile()
    plan(o, s, NOW.replace(hour=15))
    d["source_errors"] = {"house_consumption": "missing_or_stale"}
    result = project(m, d, o, s)
    assert result["reason"] == "invalid_current_inputs"
    assert result["planned_dhw"]["status"] == "booked"
    assert "_end" not in result["planned_dhw"]
