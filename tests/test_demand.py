"""Synthetic demand histories; no household data in fixtures."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import json

import pytest

from custom_components.opti_akku.demand import (
    DemandForecast,
    instant,
    number,
    pv_intervals,
    source_value,
)

NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)
SETTINGS = {
    "input_number.opti_peak_verbrauch_kw": 0.8,
    "input_number.minsoc": 5,
    "input_number.maxsoc": 95,
}


def state(value, unit="W", now=NOW, **attrs):
    return SimpleNamespace(
        state=str(value),
        last_reported=now,
        last_updated=now,
        attributes={"unit_of_measurement": unit, **attrs},
    )


def fixture(house=500, heat=0, pv_at=2):
    rows = [
        {
            "period_start": NOW + timedelta(minutes=30 * i),
            "pv_estimate10": 0 if i < pv_at * 2 else 5,
        }
        for i in range(48)
    ]
    states = {
        "sensor.heat": state(heat),
        "sensor.pv": state(50, "kWh", detailedForecast=rows),
        "binary_sensor.summer": state("on", None),
        "binary_sensor.heating": state("off", None),
        "binary_sensor.dhw": state("off", None),
    }
    options = {
        "demand_forecast": {
            "enabled": True,
            "sources": {
                "heat_power": "sensor.heat",
                "summer_mode": "binary_sensor.summer",
                "heating_active": "binary_sensor.heating",
                "dhw_active": "binary_sensor.dhw",
                "pv_today": "sensor.pv",
            },
        }
    }
    data = {
        "online": True,
        "source_errors": {},
        "reserve_plan": {"planned_reserve_soc": 45},
        "states": {
            "sensor.opti_house_consumption_w": house,
            "sensor.opti_battery_capacity_kwh": 10,
            "sensor.opti_soc": 50,
        },
    }
    return data, options, states


def update(model, data, options, states, now=NOW, tz=UTC):
    return model.update(now, data, SETTINGS, options, states, tz, "fixture")


def test_source_values_reject_ambiguous_or_stale_inputs():
    assert number(True) is None
    assert instant(NOW.isoformat()) == NOW
    with pytest.raises(ValueError, match="timezone"):
        instant("2026-09-14T06:00:00")

    malformed = state(500)
    malformed.last_reported = object()
    assert source_value({"sensor.load": malformed}, "sensor.load", NOW, kind="power") is None
    assert (
        source_value(
            {"sensor.temp": state(20, "°C", NOW - timedelta(hours=7))},
            "sensor.temp",
            NOW,
            kind="temperature",
        )
        is None
    )
    assert source_value({"sensor.load": state(1, "A")}, "sensor.load", NOW, kind="power") is None


def test_power_source_age_can_opt_in_to_ha_availability():
    old_w = state(500, "W", NOW - timedelta(hours=1))
    old_kw = state(0.5, "kW", NOW - timedelta(hours=1))

    assert source_value({"sensor.load": old_w}, "sensor.load", NOW, kind="power") is None
    assert source_value(
        {"sensor.load": old_w}, "sensor.load", NOW, kind="power", source_max_age=0
    ) == 500
    assert source_value(
        {"sensor.load": old_kw}, "sensor.load", NOW, kind="power", source_max_age=0
    ) == 500

    unavailable = state("unavailable", "W", NOW - timedelta(hours=1))
    assert source_value(
        {"sensor.load": unavailable},
        "sensor.load",
        NOW,
        kind="power",
        source_max_age=0,
    ) is None
    assert source_value({}, "sensor.load", NOW, kind="power", source_max_age=0) is None
    assert source_value(
        {"sensor.load": state("unknown")},
        "sensor.load",
        NOW,
        kind="power",
        source_max_age=0,
    ) is None
    assert source_value(
        {"sensor.load": state(float("nan"))},
        "sensor.load",
        NOW,
        kind="power",
        source_max_age=0,
    ) is None
    assert source_value(
        {"sensor.load": state(1, "A")},
        "sensor.load",
        NOW,
        kind="power",
        source_max_age=0,
    ) is None
    assert source_value(
        {"sensor.load": state(500, "W", NOW + timedelta(seconds=61))},
        "sensor.load",
        NOW,
        kind="power",
        source_max_age=0,
    ) is None


@pytest.mark.parametrize("timestamp", [None, NOW.isoformat(), NOW.replace(tzinfo=None)])
def test_power_source_requires_ha_datetime_even_with_availability_opt_in(timestamp):
    # HA State objects supply aware datetimes; serialized or malformed timestamps
    # must not turn an unverified power reading into valid demand data.
    assert source_value(
        {"sensor.load": state(500, "W", timestamp)},
        "sensor.load",
        NOW,
        kind="power",
        source_max_age=0,
    ) is None


@pytest.mark.parametrize("timestamp", [None, NOW.replace(tzinfo=None), NOW - timedelta(hours=7)])
def test_temperature_guard_is_not_relaxed_by_power_availability_opt_in(timestamp):
    assert source_value(
        {"sensor.temp": state(20, "°C", timestamp)},
        "sensor.temp",
        NOW,
        kind="temperature",
        source_max_age=0,
    ) is None


def test_demand_forecast_propagates_source_max_age_to_heat_power():
    data, options, states = fixture(house=500, heat=100)
    states["sensor.heat"] = state(100, "W", NOW - timedelta(hours=1))

    default = update(DemandForecast(), data, options, states)
    assert default["status"] == "data_missing"
    assert default["detail"] == "house_or_heat_power"

    options["source_max_age"] = 0
    opted_in = DemandForecast()
    result = update(opted_in, data, options, states)
    assert result["status"] == "learning"
    assert opted_in.previous[1] == 400


def test_pv_intervals_fail_closed_on_invalid_or_conflicting_forecasts():
    invalid = state(1, "kWh", detailedForecast=[{"period_start": NOW}])
    assert pv_intervals({"sensor.pv": invalid}, {"pv_today": "sensor.pv"}, NOW) == []

    today = state(
        1,
        "kWh",
        detailedForecast=[
            {"period_start": NOW - timedelta(hours=1), "pv_estimate10": 1},
            {"period_start": NOW, "pv_estimate10": 1},
        ],
    )
    tomorrow = state(
        1,
        "kWh",
        detailedForecast=[{"period_start": NOW, "pv_estimate10": 2}],
    )
    sources = {"pv_today": "sensor.today", "pv_tomorrow": "sensor.tomorrow"}
    states = {"sensor.today": today, "sensor.tomorrow": tomorrow}
    assert pv_intervals(states, sources, NOW) == []

    today.attributes["detailedForecast"] = [
        {"period_start": NOW, "pv_estimate10": 1},
        {"period_start": NOW + timedelta(minutes=15), "pv_estimate10": 1},
    ]
    assert pv_intervals({"sensor.today": today}, {"pv_today": "sensor.today"}, NOW) == []


@pytest.mark.parametrize(
    "cells",
    [
        {"2026-09-01|24|summer": [0, 0, 0, 3600]},
        {"2026-09-01|1|summer": [0, 0, 3600]},
        {"2026-09-01|1|summer": [0, 0, 0, 0]},
        {"2026-09-01|1|summer": [50001 * 3600, 0, 0, 3600]},
        {"not-a-date|1|summer": [0, 0, 0, 3600]},
    ],
)
def test_restore_rejects_malformed_training_cells(cells):
    model = DemandForecast()
    model.restore({"version": 1, "fingerprint": "fixture", "cells": cells})
    assert model.cells == {}


def trained(base=500, learned_heat=0, **kwargs):
    data, options, states = fixture(**kwargs)
    model = DemandForecast()
    update(model, data, options, states)
    for weeks in (1, 2, 3):
        date = (NOW - timedelta(days=7 * weeks)).date()
        for hour in range(24):
            model.cells[f"{date}|{hour}|summer"] = [base * 3600, learned_heat * 3600, 0, 3600]
    model.previous = None
    for minute in range(31):
        at = NOW - timedelta(minutes=30 - minute)
        for s in states.values():
            s.last_reported = s.last_updated = at
        out = update(model, data, options, states, at)
    return model, data, options, states, out


def test_reserve_uses_deficit_until_pv_not_daily_total_or_confirmation_hour():
    _, _, _, _, out = trained()
    assert out["status"] == "ready"
    assert out["pv_cover_from"] == (NOW + timedelta(hours=2)).isoformat()
    assert out["expected_load_kwh"] == 1
    assert out["expected_deficit_kwh"] == 1
    assert out["required_battery_kwh"] == pytest.approx(1.556, abs=0.001)
    assert out["suggested_reserve_soc"] == 20.6
    assert out["current_strategy_reserve_soc"] == 45
    assert out["observation_only"] and not out["controls_battery"]
    assert out["pv_cover_factor"] == 1.2
    assert out["pv_cover_min_minutes"] == 60
    assert out["pv_cover_min_net_kwh"] == 0.5
    assert out["pv_cover_net_kwh"] >= 0.5
    assert out["refill_covered"] is True
    assert out["refill_missing_kwh"] == 0
    assert out["refill_coverage_percent"] == 100


def test_low_light_ratio_without_useful_energy_is_not_pv_cover():
    model, data, options, states, _ = trained(base=10, house=10, pv_at=0)
    for row in states["sensor.pv"].attributes["detailedForecast"]:
        row["pv_estimate10"] = 0.02

    out = update(model, data, options, states)

    assert out["status"] == "no_pv_timing"
    assert out["detail"] == "no_contiguous_sustained_pv_cover"
    assert out["pv_cover_min_net_kwh"] == 0.5


@pytest.mark.parametrize(
    ("pv_kw", "expected_status"),
    [(0.999, "no_pv_timing"), (1.0, "ready")],
)
def test_pv_cover_minimum_net_energy_boundary(pv_kw, expected_status):
    model, data, options, states, _ = trained(base=500, house=500, pv_at=0)
    for index, row in enumerate(states["sensor.pv"].attributes["detailedForecast"]):
        row["pv_estimate10"] = pv_kw if index < 2 else 0

    out = update(model, data, options, states)

    assert out["status"] == expected_status


def test_insufficient_refill_is_reported_without_changing_reserve():
    model, data, options, states, baseline = trained(base=500, house=500, pv_at=2)
    for row in states["sensor.pv"].attributes["detailedForecast"]:
        if row["period_start"] >= NOW + timedelta(hours=2):
            row["pv_estimate10"] = 1
        if row["period_start"] >= NOW + timedelta(hours=3):
            row["pv_estimate10"] = 0

    out = update(model, data, options, states)

    assert out["status"] == "ready"
    assert out["refill_surplus_kwh"] == 0.5
    assert out["refill_battery_kwh"] == 0.45
    assert out["refill_covered"] is False
    assert out["refill_missing_kwh"] == pytest.approx(1.106)
    assert out["refill_coverage_percent"] == pytest.approx(28.93)
    assert out["suggested_reserve_soc"] == baseline["suggested_reserve_soc"]
    assert out["observation_only"] is True
    assert out["controls_battery"] is False


def test_refill_fallback_does_not_downgrade_bridge_forecast():
    model, data, options, states, _ = trained(base=500, house=500, pv_at=2)
    model.cells = {
        key: row for key, row in model.cells.items() if int(key.split("|")[1]) != 12
    }

    out = update(model, data, options, states)

    assert out["status"] == "ready"
    assert out["profile_ready"] is True
    assert out["refill_profile_ready"] is False


def test_longer_cover_window_keeps_original_boundary_and_bridge_need():
    model, data, options, states, _ = trained(base=500, house=500, pv_at=2)
    rows = states["sensor.pv"].attributes["detailedForecast"]
    for index, row in enumerate(rows):
        row["pv_estimate10"] = 0.8 if 4 <= index < 8 else 0

    out = update(model, data, options, states)

    assert out["pv_cover_from"] == (NOW + timedelta(hours=2)).isoformat()
    assert out["pv_cover_net_kwh"] == 0.6
    assert out["expected_deficit_kwh"] == 1
    assert out["suggested_reserve_soc"] == 20.6


def test_refill_sums_only_surplus_and_reports_truncated_horizon():
    model, data, options, states, _ = trained(base=500, house=500, pv_at=2)
    rows = states["sensor.pv"].attributes["detailedForecast"]
    for index, row in enumerate(rows):
        row["pv_estimate10"] = {4: 1, 5: 1, 6: 0.55, 7: 0.4}.get(index, 0)
    del rows[9]

    out = update(model, data, options, states)

    assert out["pv_cover_from"] == (NOW + timedelta(hours=2)).isoformat()
    assert out["refill_surplus_kwh"] == 0.525
    assert out["refill_horizon_end"] == (NOW + timedelta(hours=4, minutes=30)).isoformat()
    assert out["refill_horizon_hours"] == 4.5


def test_refill_deducts_intervening_deficit_before_later_surplus():
    model, data, options, states, _ = trained(base=500, house=500, pv_at=2)
    rows = states["sensor.pv"].attributes["detailedForecast"]
    for index, row in enumerate(rows):
        row["pv_estimate10"] = 1 if index in {4, 5, 7, 8} else 0

    out = update(model, data, options, states)

    assert out["pv_cover_from"] == (NOW + timedelta(hours=2)).isoformat()
    assert out["refill_surplus_kwh"] == 0.75


def test_refill_target_is_limited_to_usable_capacity():
    model, data, options, states, _ = trained()
    data["states"]["sensor.opti_battery_capacity_kwh"] = 1

    out = update(model, data, options, states)

    assert out["capacity_shortfall_kwh"] > 0
    assert out["refill_target_kwh"] == 0.9
    assert out["refill_storable_kwh"] == 0.9
    assert out["refill_covered"] is True
    assert out["refill_missing_kwh"] == 0
    assert out["refill_coverage_percent"] == 100


def test_extra_server_increases_base_but_decays():
    _, _, _, _, out = trained(house=900)
    assert out["extra_base_load_w"] == 400
    assert out["forecast_slots"][0]["load_w"] == 900
    assert out["forecast_slots"][2]["load_w"] < 900
    assert out["suggested_reserve_soc"] > 20.6


def test_heat_meter_avoids_double_count_and_dhw_is_not_server():
    model, data, options, states, _ = trained(base=500, learned_heat=200, house=1500)
    states["sensor.heat"] = state(1000)
    states["binary_sensor.dhw"] = state("on", None)
    # Reset recent window: demonstrate separate residual, not a whole-house correction.
    model.recent._samples.clear()
    model.previous = None
    for minute in range(31):
        at = NOW - timedelta(minutes=30 - minute)
        states["sensor.heat"].last_reported = at
        out = update(model, data, options, states, at)
    assert out["extra_base_load_w"] == 0
    assert out["forecast_slots"][0]["load_w"] == 1500
    assert out["forecast_slots"][2]["load_w"] == 700


def test_missing_heat_measurement_cannot_be_assumed_zero():
    model, data, options, states, _ = trained()
    states["sensor.heat"] = state("unavailable")
    out = update(model, data, options, states)
    assert out["status"] == "data_missing"
    assert "suggested_reserve_soc" not in out


def test_dhw_floor_does_not_add_a_second_full_cycle():
    model, data, options, states, _ = trained(base=500, learned_heat=500, house=1000, heat=500)
    options["demand_forecast"]["sources"]["dhw_due"] = "binary_sensor.due"
    options["demand_forecast"]["dhw_cycle_kwh"] = 1
    states["binary_sensor.due"] = state("on", None)
    out = update(model, data, options, states)
    # Fingerprint changed: reset intentionally. Seed separate DHW-labelled history.
    for weeks in (1, 2, 3):
        for hour in range(24):
            model.cells[f"{(NOW - timedelta(days=weeks * 7)).date()}|{hour}|summer"] = [
                500 * 3600,
                500 * 3600,
                250 * 3600,
                3600,
            ]
    out = update(model, data, options, states)
    assert out["dhw_load_kwh"] == 1
    assert out["dhw_extra_kwh"] == 0.5


def test_space_heat_cannot_replace_due_hot_water():
    model, data, options, states, _ = trained(base=500, learned_heat=1000, house=1500, heat=1000)
    options["demand_forecast"]["sources"]["dhw_due"] = "binary_sensor.due"
    options["demand_forecast"]["dhw_cycle_kwh"] = 1
    states["binary_sensor.due"] = state("on", None)
    out = update(model, data, options, states)
    assert out["dhw_extra_kwh"] == 1


def test_no_dated_p10_means_no_precise_reserve():
    model, data, options, states, _ = trained()
    states["sensor.pv"].attributes = {"unit_of_measurement": "kWh"}
    out = update(model, data, options, states)
    assert out["status"] == "no_pv_timing"
    assert "suggested_reserve_soc" not in out


def test_missing_slot_cannot_bridge_to_later_sun():
    model, data, options, states, _ = trained()
    del states["sensor.pv"].attributes["detailedForecast"][1]
    assert update(model, data, options, states)["status"] == "no_pv_timing"


def test_static_target_is_not_stale_and_unknown_due_energy_is_explicit():
    model, data, options, states, _ = trained()
    options["demand_forecast"]["sources"].update(
        water_target="number.target", water_temperature="sensor.water"
    )
    states["number.target"] = state(50, "°C", NOW - timedelta(days=30))
    states["sensor.water"] = state(45, "°C")
    out = update(model, data, options, states)
    assert out["detail"] == "dhw_due_requires_meter_and_cycle_energy"
    states["sensor.water"] = state(53, "°C")
    assert "suggested_reserve_soc" in update(model, data, options, states)


def test_restart_no_downtime_learning_and_snapshot_numeric_canonicalization():
    model, data, options, states, _ = trained()
    saved = json.loads(json.dumps(model.snapshot(), allow_nan=False))
    saved["cells"] = {k: [str(v) for v in row] for k, row in saved["cells"].items()}
    other = DemandForecast()
    other.restore(saved)
    before = deepcopy(other.cells)
    update(other, data, options, states, NOW + timedelta(minutes=10))
    assert other.cells == before
    assert other.previous[0] == NOW + timedelta(minutes=10)
    assert all(isinstance(x, float) for row in other.cells.values() for x in row)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "unavailable", None])
def test_bad_house_never_learns_or_recommends(value):
    model, data, options, states, _ = trained()
    data["states"]["sensor.opti_house_consumption_w"] = value
    assert update(model, data, options, states)["status"] == "data_missing"
    assert model.previous[1] is None


def test_no_heat_meter_keeps_whole_house_not_plus_thermal():
    model, data, options, states, _ = trained()
    options["demand_forecast"]["sources"].pop("heat_power")
    out = update(model, data, options, states)
    assert not out["heat_meter_configured"]
    assert out["heat_load_kwh"] == 0


def test_summer_history_cannot_be_used_as_winter_profile():
    model, data, options, states, _ = trained()
    states["binary_sensor.summer"] = state("off", None)
    out = update(model, data, options, states)
    assert out["status"] == "learning" and not out["profile_ready"]


def test_source_rebinding_resets_only_observer_history():
    model, data, options, states, _ = trained()
    original = deepcopy(data)
    options["demand_forecast"]["sources"]["heat_power"] = "sensor.other"
    states["sensor.other"] = state(0)
    update(model, data, options, states)
    assert not model.cells
    assert data == original


def test_dst_fold_integrates_actual_elapsed_time():
    model = DemandForecast()
    tz = ZoneInfo("Europe/Berlin")
    a = datetime(2026, 10, 25, 0, 59, 30, tzinfo=UTC)
    model._learn(a, 500, 0, 0, "summer", tz)
    model._learn(a + timedelta(seconds=60), 500, 0, 0, "summer", tz)
    assert sum(row[3] for row in model.cells.values()) == 60
    assert list(model.cells) == ["2026-10-25|2|summer"]


def test_clock_backwards_clears_training():
    model, data, options, states, _ = trained()
    update(model, data, options, states, NOW - timedelta(seconds=1))
    assert not model.cells


def test_snapshot_is_detached_and_bounded():
    model, data, options, states, _ = trained()
    saved = model.snapshot()
    key = next(iter(saved["cells"]))
    saved["cells"][key][0] = 0
    assert model.cells[key][0] > 0
    other = DemandForecast()
    other.restore(
        {"version": 1, "fingerprint": "x", "cells": {str(i): [0, 0, 0, 3600] for i in range(4033)}}
    )
    assert not other.cells


def test_capacity_shortfall_is_visible_not_hidden_by_clamp():
    model, data, options, states, _ = trained(base=10000, house=10000, pv_at=2)
    # PV needs enough power to cover the house eventually.
    for row in states["sensor.pv"].attributes["detailedForecast"][4:]:
        row["pv_estimate10"] = 20
    out = update(model, data, options, states)
    assert out["suggested_reserve_soc"] == 95
    assert out["capacity_shortfall_kwh"] > 0


def test_cold_start_does_not_project_hot_water_for_entire_night():
    data, options, states = fixture(house=1500, heat=1000, pv_at=8)
    states["binary_sensor.dhw"] = state("on", None)
    out = update(DemandForecast(), data, options, states)
    assert out["status"] == "learning"
    assert out["heat_load_kwh"] == 1
    assert out["forecast_slots"][0]["load_w"] == 1500
    assert out["forecast_slots"][2]["load_w"] == 800


def test_missing_heat_context_has_precise_diagnostic_and_is_not_learned():
    model, data, options, states, _ = trained()
    states["binary_sensor.heating"] = state("unavailable", None)

    out = update(model, data, options, states)

    assert out["status"] == "data_missing"
    assert out["detail"] == "heat_context_unknown"
    assert model.previous[1] is None


def test_integrated_heat_activity_never_becomes_extra_base_load():
    model, data, options, states, _ = trained(house=900)
    options["demand_forecast"]["sources"].pop("heat_power")
    states["binary_sensor.heating"] = state("on", None)

    out = update(model, data, options, states)

    assert out["extra_base_load_w"] == 0


def test_invalid_fallback_and_battery_limits_fail_closed():
    model, data, options, states, _ = trained()
    settings = {**SETTINGS, "input_number.opti_peak_verbrauch_kw": 0}
    out = model.update(NOW, data, settings, options, states, UTC, "fixture")
    assert out["status"] == "data_missing"
    assert out["detail"] == "fallback_load"

    data["states"]["sensor.opti_battery_capacity_kwh"] = 0
    out = update(model, data, options, states)
    assert out["status"] == "data_missing"
    assert out["detail"] == "battery_limits"


def test_changed_history_binding_discards_incompatible_prior():
    model, data, options, states, _ = trained()
    model.history.binding = "different installation"
    model.history.rows = {NOW.isoformat(): {"house_w": 500}}

    update(model, data, options, states)

    assert model.history.binding is None
    assert model.history.rows == {}


@pytest.mark.parametrize("gap,coverage", [(0.007, 99.55), (0.000001, 99.99)])
def test_small_refill_gap_remains_visible_without_changing_control(gap, coverage):
    model, data, options, states, baseline = trained(base=500, house=500, pv_at=2)
    target = (1 * 1.2 + 0.2) / 0.9
    for row in states["sensor.pv"].attributes["detailedForecast"]:
        if row["period_start"] >= NOW + timedelta(hours=2):
            row["pv_estimate10"] = 0.5 + (target - gap) / 0.9
        if row["period_start"] >= NOW + timedelta(hours=3):
            row["pv_estimate10"] = 0
    out = update(model, data, options, states)
    assert out["refill_covered"] is False
    assert out["refill_missing_kwh"] == round(gap, 3)
    assert out["refill_coverage_percent"] == coverage
    assert out["suggested_reserve_soc"] == baseline["suggested_reserve_soc"]
    assert out["observation_only"] is True and out["controls_battery"] is False
