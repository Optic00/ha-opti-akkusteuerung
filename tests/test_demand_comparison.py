"""Passive profile comparisons against the active forecast formulas."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.demand import DemandForecast
from custom_components.opti_akku.demand_comparison import (
    build_strategy_comparison,
    remaining_day_profile,
)

TZ = ZoneInfo("Europe/Berlin")


def learned_model(issued_at, watts=1000):
    model = DemandForecast()
    local = issued_at.astimezone(TZ)
    for days in range(1, 22):
        day = (local - timedelta(days=days)).date()
        for hour in range(24):
            model.cells[f"{day.isoformat()}|{hour}|summer"] = [watts * 3600, 0, 0, 3600]
    return model


def payload(issued_at, *, report_status="ready", next_setting=None, next_rising=None):
    next_setting = next_setting or issued_at + timedelta(hours=6)
    next_rising = next_rising or issued_at + timedelta(hours=18)
    return {
        "states": {
            "sun.sun": "above_horizon",
            "sensor.opti_forecast_remaining_today_kwh": 10,
            "sensor.opti_forecast_effective_remaining_kwh": 10,
            "sensor.opti_forecast_today_kwh": 24,
            "sensor.opti_forecast_tomorrow_kwh": 24,
            "sensor.opti_battery_capacity_kwh": 10,
            "sensor.opti_soc": 50,
            "sensor.opti_house_consumption_w": 1000,
            "sensor.opti_house_consumption_60min_w": 1000,
            "sensor.opti_forecast_score": 8,
            "sensor.opti_forecast_score_tomorrow": 10,
            "sensor.opti_forecast_score_sonnentag": 10,
            "sensor.opti_target_soc": 90,
            "input_number.opti_forecast_optimismus": 0,
        },
        "attributes": {
            "sun.sun": {
                "next_setting": next_setting.isoformat(),
                "next_rising": next_rising.isoformat(),
            },
            "sensor.opti_forecast_today_kwh": {"estimate10": 24},
            "sensor.opti_forecast_tomorrow_kwh": {"estimate10": 24},
            "sensor.opti_target_soc": {"level": 1, "ratio": 0.6},
        },
        "demand_forecast": {
            "status": report_status,
            "context": "summer",
            "heating_active": False,
            "dhw_active": False,
            "heat_meter_configured": False,
            "recent_coverage_seconds": 1800,
            "extra_base_load_w": 0,
            "temperature_context": {},
        },
    }


SETTINGS = {
    "input_number.opti_peak_verbrauch_kw": 2,
    "input_number.minsoc": 10,
    "input_number.maxsoc": 95,
    "input_boolean.hausakku_aus_netz_laden": False,
}
OPTIONS = {"demand_forecast": {"enabled": True, "sources": {}}}


def compare(
    model, issued_at, data, *, previous=1, settings=None, options=None, ha_states=None
):
    return build_strategy_comparison(
        model, issued_at, data, settings or SETTINGS, options or OPTIONS, ha_states or {}, TZ,
        "fingerprint", previous,
    )


def active_profile(model, issued_at, data, *, settings=None, options=None, ha_states=None):
    return remaining_day_profile(
        model, issued_at, data, settings or SETTINGS, options or OPTIONS, ha_states or {}, TZ,
        "fingerprint",
    )[0]


def test_constant_profile_matches_all_three_formulas():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    result = compare(learned_model(issued), issued, payload(issued))
    assert result["status"] == "ready"
    assert result["blocks"]["remaining_day"]["profile_energy_kwh"] == 6
    assert result["blocks"]["remaining_day"]["candidate_score"] == 8
    assert result["blocks"]["tomorrow"]["profile_energy_kwh"] == 24
    assert result["blocks"]["tomorrow"]["candidate_score"] == 10
    assert result["blocks"]["sunny_day"]["candidate_score"] == 10
    assert result["blocks"]["target_soc"]["candidate_ratio"] == pytest.approx(0.4)
    assert result["blocks"]["target_soc"]["candidate_target_soc"] == 90


def test_partial_hours_are_integrated_exactly():
    issued = datetime(2026, 9, 16, 10, 30, tzinfo=UTC)
    setting = issued + timedelta(hours=1, minutes=45)
    result = compare(learned_model(issued), issued, payload(issued, next_setting=setting))
    assert result["blocks"]["remaining_day"]["profile_energy_kwh"] == 1.75


def test_active_remaining_day_profile_uses_only_a_complete_online_window():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    ready = active_profile(learned_model(issued), issued, payload(issued))
    assert ready["status"] == "ready"
    assert ready["reason"] == "complete_online_profile"
    assert ready["profile_energy_kwh"] == 6

    warming = payload(issued)
    warming["demand_forecast"]["recent_coverage_seconds"] = 0
    result = active_profile(learned_model(issued), issued, warming)
    assert result["status"] == "learning"
    assert result["reason"] == "recent_profile_warming_up"


def test_active_remaining_day_profile_reports_disabled_forecast():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)

    result = active_profile(
        learned_model(issued),
        issued,
        payload(issued),
        options={"demand_forecast": {"enabled": False}},
    )

    assert result == {
        "status": "disabled",
        "reason": "demand_forecast_disabled",
    }


def test_active_remaining_day_profile_rejects_unplaced_hot_water_need():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["demand_forecast"]["dhw_extra_kwh"] = 1
    result = active_profile(learned_model(issued), issued, data)
    assert result["status"] == "learning"
    assert result["reason"] == "dhw_timing_unknown"


@pytest.mark.parametrize(
    ("issued", "expected_hours"),
    [
        (datetime(2026, 3, 28, 12, tzinfo=UTC), 23),
        (datetime(2026, 10, 24, 12, tzinfo=UTC), 25),
    ],
)
def test_tomorrow_uses_real_dst_day_duration(issued, expected_hours):
    data = payload(issued)
    result = compare(learned_model(issued), issued, data)
    tomorrow = result["blocks"]["tomorrow"]
    assert tomorrow["window_hours"] == expected_hours
    assert tomorrow["profile_energy_kwh"] == expected_hours


def test_true_zero_profile_uses_visible_one_watt_day_floor():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["states"]["sensor.opti_forecast_tomorrow_kwh"] = 0.012
    data["attributes"]["sensor.opti_forecast_tomorrow_kwh"]["estimate10"] = 0.012
    tomorrow = compare(learned_model(issued, watts=0), issued, data)["blocks"]["tomorrow"]
    assert tomorrow["profile_energy_kwh"] == 0
    assert tomorrow["profile_denominator_kwh"] == pytest.approx(0.024)
    assert tomorrow["candidate_score"] == 5


def test_tomorrow_does_not_use_incomplete_current_day_cells():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    model = learned_model(issued)
    today = issued.astimezone(TZ).date().isoformat()
    for hour in range(24):
        model.cells[f"{today}|{hour}|summer"] = [50000 * 3600, 0, 0, 3600]
    result = compare(model, issued, payload(issued))
    assert result["blocks"]["tomorrow"]["profile_energy_kwh"] == 24


def test_mixed_online_and_fallback_is_learning_and_hides_candidates():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    model = learned_model(issued)
    for key in list(model.cells):
        if key.split("|")[1] == "5":
            del model.cells[key]
    result = compare(model, issued, payload(issued))
    tomorrow = result["blocks"]["tomorrow"]
    assert tomorrow["status"] == "learning"
    assert tomorrow["reason"] == "fixed_fallback_used"
    assert tomorrow["profile_sources"]["fallback"]["seconds"] == 3600
    assert tomorrow["candidate_score"] is None


def test_bound_recorder_prior_is_reported_as_historical_learning():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    model = learned_model(issued)
    for key in list(model.cells):
        if key.split("|")[1] == "5":
            del model.cells[key]
    model.history.binding = model.history_binding("fingerprint", OPTIONS, TZ)
    for days in (28, 35, 42):
        local = issued.astimezone(TZ) - timedelta(days=days)
        at = local.replace(hour=5, minute=0, second=0, microsecond=0).astimezone(UTC)
        model.history.rows[at.isoformat()] = {
            "house_w": 1500, "summer_mode": "on", "heating_active": "off",
            "dhw_active": "off", "outdoor_temperature": None,
            "context_water_temperature": None, "context_water_target": None,
        }
    tomorrow = compare(model, issued, payload(issued))["blocks"]["tomorrow"]
    assert tomorrow["status"] == "learning"
    assert tomorrow["reason"] == "historical_prior_used"
    assert tomorrow["profile_sources"]["historical"] == {"seconds": 3600, "kwh": 1.5}


def test_extra_load_decays_and_active_separate_heat_is_only_first_hour_floor():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["demand_forecast"].update(
        heating_active=True, heat_meter_configured=True, extra_base_load_w=1000
    )
    options = {"demand_forecast": {
        "enabled": True, "sources": {"heat_power": "sensor.heat"}
    }}
    heat_state = SimpleNamespace(
        state="3000", last_reported=issued, last_updated=issued,
        attributes={"unit_of_measurement": "W"},
    )
    remaining = compare(
        learned_model(issued), issued, data, options=options,
        ha_states={"sensor.heat": heat_state},
    )["blocks"]["remaining_day"]
    # Six base kWh + three heat kWh in hour one + two kWh triangular uplift.
    assert remaining["profile_energy_kwh"] == 11


def test_comparison_heat_power_honors_global_source_max_age():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["demand_forecast"].update(
        heating_active=True, heat_meter_configured=True
    )
    options = {"demand_forecast": {
        "enabled": True, "sources": {"heat_power": "sensor.heat"}
    }}
    heat_state = SimpleNamespace(
        state="3000",
        last_reported=issued - timedelta(hours=1),
        last_updated=issued - timedelta(hours=1),
        attributes={"unit_of_measurement": "W"},
    )
    model = learned_model(issued)

    bounded = active_profile(
        model, issued, data, options=options, ha_states={"sensor.heat": heat_state}
    )
    assert bounded == {
        "status": "data_missing", "reason": "profile_window_unavailable"
    }

    options["source_max_age"] = 0
    available = active_profile(
        model, issued, data, options=options, ha_states={"sensor.heat": heat_state}
    )
    assert available["status"] == "ready"
    assert available["profile_energy_kwh"] == 9


def test_fixed_fallback_is_not_increased_by_heat_or_extra_load():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["demand_forecast"].update(
        heating_active=True, heat_meter_configured=True, extra_base_load_w=1000
    )
    options = {"demand_forecast": {
        "enabled": True, "sources": {"heat_power": "sensor.heat"}
    }}
    heat_state = SimpleNamespace(
        state="3000", last_reported=issued, last_updated=issued,
        attributes={"unit_of_measurement": "W"},
    )
    remaining = compare(
        DemandForecast(), issued, data, options=options,
        ha_states={"sensor.heat": heat_state},
    )["blocks"]["remaining_day"]
    assert remaining["status"] == "learning"
    assert remaining["reason"] == "fixed_fallback_used"
    assert remaining["profile_energy_kwh"] == 12


def test_current_heat_floor_never_rewrites_past_hours_of_sunny_day():
    issued = datetime(2026, 9, 16, 3, 30, tzinfo=UTC)
    rising = datetime(2026, 9, 16, 4, tzinfo=UTC)
    data = payload(issued, next_rising=rising)
    data["states"]["sun.sun"] = "below_horizon"
    data["demand_forecast"].update(heating_active=True, heat_meter_configured=True)
    options = {"demand_forecast": {
        "enabled": True, "sources": {"heat_power": "sensor.heat"}
    }}
    heat_state = SimpleNamespace(
        state="3000", last_reported=issued, last_updated=issued,
        attributes={"unit_of_measurement": "W"},
    )
    sunny = compare(
        learned_model(issued), issued, data, options=options,
        ha_states={"sensor.heat": heat_state},
    )["blocks"]["sunny_day"]
    # 24 kWh base plus the separately metered 3 kW heat for one future hour.
    assert sunny["profile_energy_kwh"] == 27


def test_unplaced_hot_water_need_keeps_candidates_in_learning():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["demand_forecast"].update(dhw_due=True, dhw_extra_kwh=1.5)
    result = compare(learned_model(issued), issued, data)
    assert result["blocks"]["tomorrow"]["status"] == "learning"
    assert result["blocks"]["tomorrow"]["reason"] == "dhw_timing_unknown"
    assert result["blocks"]["tomorrow"]["candidate_score"] is None


def test_no_pv_timing_still_detects_unplaced_hot_water_need():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued, report_status="no_pv_timing")
    data["demand_forecast"].pop("dhw_extra_kwh", None)
    due = SimpleNamespace(state="on")
    options = {"demand_forecast": {
        "enabled": True, "sources": {"dhw_due": "binary_sensor.dhw_due"}
    }}
    tomorrow = compare(
        learned_model(issued), issued, data, options=options,
        ha_states={"binary_sensor.dhw_due": due},
    )["blocks"]["tomorrow"]
    assert tomorrow["status"] == "learning"
    assert tomorrow["reason"] == "dhw_timing_unknown"
    assert tomorrow["candidate_score"] is None


def test_temperature_context_only_reaches_history_when_matching_is_enabled():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["demand_forecast"]["temperature_context"] = {"outdoor_temperature": 8.5}
    for enabled, expected in ((False, None), (True, 8.5)):
        model = DemandForecast()
        options = {"demand_forecast": {
            "enabled": True, "temperature_matching": enabled, "sources": {}
        }}
        model.history.binding = model.history_binding("fingerprint", options, TZ)
        model.history.expected = Mock(return_value={"house_w": 1000})
        compare(model, issued, data, options=options)
        assert model.history.expected.call_args.args[-1] == expected


def test_sunny_today_marks_full_day_profile_assumption():
    issued = datetime(2026, 9, 16, 3, tzinfo=UTC)
    rising = datetime(2026, 9, 16, 4, tzinfo=UTC)
    data = payload(issued, next_rising=rising)
    data["states"]["sun.sun"] = "below_horizon"
    result = compare(learned_model(issued), issued, data)
    assert result["blocks"]["remaining_day"]["status"] == "not_applicable"
    assert result["blocks"]["sunny_day"]["full_day_profile_assumption"] is True


def test_missing_data_and_no_pv_timing_are_distinct():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    missing = payload(issued, report_status="data_missing")
    assert compare(learned_model(issued), issued, missing)["status"] == "data_missing"
    no_timing = payload(issued, report_status="no_pv_timing")
    result = compare(learned_model(issued), issued, no_timing)
    assert result["status"] == "ready"
    assert result["blocks"]["tomorrow"]["candidate_score"] == 10


def test_malformed_sun_and_unavailable_fallback_fail_visible_per_block():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["attributes"]["sun.sun"]["next_rising"] = "not-a-time"
    data["states"]["sensor.opti_forecast_tomorrow_kwh"] = -1
    settings = {**SETTINGS, "input_number.opti_peak_verbrauch_kw": 0}
    result = compare(DemandForecast(), issued, data, settings=settings)
    assert result["blocks"]["remaining_day"] == {
        "status": "data_missing", "reason": "remaining_day_inputs"
    }
    assert result["blocks"]["tomorrow"] == {
        "status": "data_missing", "reason": "pv_forecast_missing"
    }
    assert result["blocks"]["sunny_day"] == {
        "status": "data_missing", "reason": "next_rising_missing"
    }
    assert result["blocks"]["target_soc"] == {
        "status": "data_missing", "reason": "target_inputs"
    }


def test_fresh_profile_still_hides_candidates_while_recent_window_warms_up():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["demand_forecast"]["recent_coverage_seconds"] = 0
    data["states"].pop("sensor.opti_house_consumption_60min_w")
    data["states"].pop("sensor.opti_house_consumption_w")
    result = compare(learned_model(issued), issued, data)
    remaining = result["blocks"]["remaining_day"]
    assert remaining["reason"] == "recent_profile_warming_up"
    assert remaining["candidate_score"] is None
    assert remaining["legacy_extrapolated_load_kwh"] == 2.4


@pytest.mark.parametrize(
    ("previous", "ratio", "expected"),
    [(0, 0.474, 0), (0, 0.475, 1), (1, 0.275, 1), (1, 0.274, 0)],
)
def test_target_hysteresis_uses_template_thresholds(previous, ratio, expected):
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    horizon_energy = 6
    data["states"]["sensor.opti_forecast_effective_remaining_kwh"] = horizon_energy + ratio * 10
    result = compare(learned_model(issued), issued, data, previous=previous)
    target = result["blocks"]["target_soc"]
    assert target["candidate_level"] == expected


def test_comparison_does_not_mutate_demand_snapshot():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    model = learned_model(issued)
    model.previous = (issued - timedelta(seconds=30), 900.0, 0.0, 0.0, "summer")
    model.recent.observe(900, issued - timedelta(seconds=30), "fingerprint")
    before = deepcopy(model.snapshot())
    previous = deepcopy(model.previous)
    recent = deepcopy(model.recent)
    compare(model, issued, payload(issued))
    assert model.snapshot() == before
    assert model.previous == previous
    assert model.recent._fingerprint == recent._fingerprint
    assert model.recent._samples == recent._samples


def test_local_hour_boundaries_work_in_half_hour_offset_timezone():
    timezone = ZoneInfo("Asia/Kolkata")
    issued = datetime(2026, 9, 16, 10, 45, tzinfo=UTC)  # 16:15 local
    model = DemandForecast()
    local = issued.astimezone(timezone)
    for days in range(1, 22):
        day = (local - timedelta(days=days)).date()
        model.cells[f"{day.isoformat()}|16|summer"] = [1000 * 3600, 0, 0, 3600]
        model.cells[f"{day.isoformat()}|17|summer"] = [3000 * 3600, 0, 0, 3600]
    data = payload(issued, next_setting=issued + timedelta(hours=1))
    result = build_strategy_comparison(
        model, issued, data, SETTINGS, OPTIONS, {}, timezone, "fingerprint", 1
    )
    # 45 minutes at 1 kW plus 15 minutes at 3 kW.
    assert result["blocks"]["remaining_day"]["profile_energy_kwh"] == 1.5


def test_grid_charge_forces_maxsoc_and_level_zero():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    settings = {**SETTINGS, "input_boolean.hausakku_aus_netz_laden": True}
    target = compare(learned_model(issued), issued, payload(issued), settings=settings)["blocks"]["target_soc"]
    assert target["grid_charge_override"] is True
    assert target["candidate_level"] == 0
    assert target["candidate_target_soc"] == 95


def test_grid_charge_override_needs_neither_profile_nor_sunset():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["attributes"]["sun.sun"].pop("next_setting")
    settings = {**SETTINGS, "input_boolean.hausakku_aus_netz_laden": True}
    target = compare(DemandForecast(), issued, data, settings=settings)["blocks"]["target_soc"]
    assert target["status"] == "ready"
    assert target["reason"] == "grid_charge_override"
    assert target["candidate_level"] == 0
    assert target["candidate_target_soc"] == 95


def test_target_uses_plain_level_without_valid_previous_and_requires_sunset():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    target = compare(learned_model(issued), issued, data, previous=None)["blocks"]["target_soc"]
    assert target["previous_level"] == target["plain_level"]
    data["attributes"]["sun.sun"].pop("next_setting")
    missing = compare(learned_model(issued), issued, data)["blocks"]["target_soc"]
    assert missing == {"status": "data_missing", "reason": "next_setting_missing"}


def test_disabled_comparison_needs_no_profile_data():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    result = compare(DemandForecast(), issued, {}, options={"demand_forecast": {"enabled": False}})
    assert result["status"] == "disabled"
    assert result["observation_only"] is True
    assert {block["status"] for block in result["blocks"].values()} == {"disabled"}


def test_disabled_strategy_has_no_active_baseline_to_compare():
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    data = payload(issued)
    data["strategy_enabled"] = False
    result = compare(learned_model(issued), issued, data)
    assert result["status"] == "not_applicable"
    assert {block["reason"] for block in result["blocks"].values()} == {
        "strategy_disabled"
    }


def test_cached_remaining_window_preserves_comparison_and_is_not_recomputed():
    from unittest.mock import patch
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    model = learned_model(issued)
    data = payload(issued)
    expected = compare(model, issued, data)
    prepared = remaining_day_profile(model, issued, data, SETTINGS, OPTIONS, {}, TZ, "fingerprint")
    with patch("custom_components.opti_akku.demand_comparison.remaining_day_profile",
               side_effect=AssertionError("remaining window computed twice")):
        actual = build_strategy_comparison(model, issued, data, SETTINGS, OPTIONS, {}, TZ,
                                           "fingerprint", 1, remaining_day=prepared)
    assert actual == expected


def test_target_comparison_matches_active_template_at_every_hysteresis_boundary():
    from custom_components.opti_akku.engine import StrategyEngine, load_resources, TARGET_SOC_BOUNDS, TARGET_SOC_MARGIN
    resources = load_resources()
    target = next(entity for block in resources["template_blocks"]
                  for entity in block.get("sensor", []) if entity["unique_id"] == "opti_target_soc")
    engine = StrategyEngine({"schema_version": 1, "helper_defaults": resources["helper_defaults"],
                             "template_blocks": [{"sensor": [target]}]})
    issued = datetime(2026, 9, 16, 10, tzinfo=UTC)
    model = learned_model(issued)
    for previous in range(6):
        for boundary in TARGET_SOC_BOUNDS:
            for offset in (-TARGET_SOC_MARGIN - .000001, -TARGET_SOC_MARGIN,
                           TARGET_SOC_MARGIN - .000001, TARGET_SOC_MARGIN, TARGET_SOC_MARGIN + .000001):
                data = payload(issued)
                data["states"]["sensor.opti_forecast_effective_remaining_kwh"] = 6 + (boundary + offset) * 10
                engine.restore({"version": 1, "attributes": {"sensor.opti_target_soc": {"level": previous}}})
                result = engine.evaluate({**data["states"], **SETTINGS}, data["attributes"], issued.astimezone(TZ))
                candidate = compare(model, issued, data, previous=previous)["blocks"]["target_soc"]
                assert candidate["candidate_level"] == result.attributes["sensor.opti_target_soc"]["level"]
                assert candidate["candidate_target_soc"] == float(result.states["sensor.opti_target_soc"])
