from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.opti_akku.plant import (
    evaluate_plant,
    plant_entity_ids,
    plant_semantic_fingerprint,
    validate_plant_sources,
)


NOW = datetime(2026, 9, 13, 10, tzinfo=UTC)
NATIVE = {
    "sensor.opti_pv_power_w": 3000,
    "sensor.opti_grid_import_w": 100,
    "sensor.opti_grid_export_w": 2100,
}


def state(value, unit="W", age=0):
    return SimpleNamespace(
        state=str(value),
        attributes={"unit_of_measurement": unit},
        last_reported=NOW - timedelta(seconds=age),
    )


def balance_options(**overrides):
    return {
        "plant_mode": "balance",
        "plant_meter_confirmed": True,
        "forecast_min_load_w": 150,
        **overrides,
    }


def test_legacy_is_no_override_by_default():
    result = evaluate_plant(NATIVE, {}, {}, NOW)
    assert result.house_w is None
    assert result.base_load_w is None
    assert not result.errors


@pytest.mark.parametrize("battery_power", [-1800, 1800])
def test_two_inverters_do_not_double_count_battery(battery_power):
    measurements = {**NATIVE, "sensor.opti_battery_power_w": battery_power}
    result = evaluate_plant(
        measurements,
        balance_options(additional_ac_sources=["sensor.second_inverter"]),
        {"sensor.second_inverter": state(2, "kW")},
        NOW,
    )
    assert result.house_w == 3000
    assert result.attributes["components"]["additional_ac_w"] == 2000


def test_signed_additional_ac_source_is_permitted():
    result = evaluate_plant(
        NATIVE,
        balance_options(additional_ac_sources=["sensor.ac_coupling"]),
        {"sensor.ac_coupling": state(-0.2, "kW")},
        NOW,
    )
    assert result.house_w == 800


@pytest.mark.parametrize(
    ("sample", "error"),
    [
        (None, "missing_or_stale"),
        (state(1, age=901), "missing_or_stale"),
        (state(1, "A"), "unsupported_unit"),
        (state(True), "invalid_value"),
        (state("nan"), "invalid_value"),
    ],
)
def test_invalid_configured_additional_source_never_becomes_zero(sample, error):
    states = {} if sample is None else {"sensor.second": sample}
    result = evaluate_plant(
        NATIVE,
        balance_options(additional_ac_sources=["sensor.second"]),
        states,
        NOW,
    )
    assert result.house_w is None
    assert result.errors["sensor.second"] == error


def test_external_mode_uses_selected_raw_house_source_and_converts_kw():
    result = evaluate_plant(
        {},
        {
            "plant_mode": "external",
            "forecast_min_load_w": 0,
            "sources": {"house_consumption": "sensor.raw_house"},
        },
        {"sensor.raw_house": state(1.5, "kW")},
        NOW,
    )
    assert result.house_w == 1500
    assert result.forecast_input_w == 1500


def test_exclusion_is_nonnegative_and_missing_never_becomes_zero():
    result = evaluate_plant(
        NATIVE,
        balance_options(excluded_load_sources=["sensor.wallbox"]),
        {"sensor.wallbox": state(-1)},
        NOW,
    )
    assert result.base_load_w is None
    assert result.errors["sensor.wallbox"] == "negative_value"


def test_exclusion_zero_and_forecast_floor_is_not_applied_before_mean():
    result = evaluate_plant(
        NATIVE,
        balance_options(excluded_load_sources=["sensor.wallbox"]),
        {"sensor.wallbox": state(1000)},
        NOW,
    )
    assert result.house_w == 1000
    assert result.base_load_w == 0
    assert result.forecast_input_w == 0
    assert result.attributes["forecast_min_load_w"] == 150


def test_small_negative_noise_is_clamped_but_material_negative_balance_fails():
    noisy = {**NATIVE, "sensor.opti_grid_export_w": 3110}
    result = evaluate_plant(noisy, balance_options(), {}, NOW)
    assert result.house_w == 0
    broken = {**NATIVE, "sensor.opti_grid_export_w": 3200}
    result = evaluate_plant(broken, balance_options(), {}, NOW)
    assert result.house_w is None
    assert result.errors["plant_balance"] == "negative_balance"


def test_material_exclusion_above_house_is_quality_error():
    result = evaluate_plant(
        NATIVE,
        balance_options(excluded_load_sources=["sensor.wallbox"]),
        {"sensor.wallbox": state(1100)},
        NOW,
    )
    assert result.base_load_w is None
    assert result.errors["excluded_load_sources"] == "exclusions_exceed_house"


@pytest.mark.parametrize("floor", [None, True, float("nan"), -1, 5001])
def test_forecast_floor_must_be_explicit_finite_range(floor):
    options = balance_options()
    options["forecast_min_load_w"] = floor
    result = evaluate_plant(NATIVE, options, {}, NOW)
    assert result.house_w is None
    assert result.errors["forecast_min_load_w"] == "invalid_value"


def test_balance_requires_explicit_meter_confirmation():
    result = evaluate_plant(
        NATIVE,
        {"plant_mode": "balance", "forecast_min_load_w": 0},
        {},
        NOW,
    )
    assert result.errors["plant_meter_confirmed"] == "confirmation_required"


def test_entity_ids_and_fingerprint_are_stable():
    options = balance_options(
        additional_ac_sources=["sensor.b", "sensor.a"],
        excluded_load_sources=["sensor.c", "sensor.ev"],
    )
    assert plant_entity_ids(options) == (
        "sensor.b",
        "sensor.a",
        "sensor.c",
        "sensor.ev",
    )
    assert plant_semantic_fingerprint(options) == (
        "balance",
        ("sensor.a", "sensor.b"),
        ("sensor.c", "sensor.ev"),
        None,
        900,
        True,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"additional_ac_sources": ["sensor.a", "sensor.a"]},
        {"excluded_load_sources": ["sensor.a", "sensor.a"]},
        {
            "additional_ac_sources": ["sensor.shared"],
            "excluded_load_sources": ["sensor.shared"],
        },
    ],
)
def test_duplicate_or_overlapping_sources_are_invalid(overrides):
    result = evaluate_plant(NATIVE, balance_options(**overrides), {}, NOW)
    assert result.house_w is None
    assert result.errors


def test_external_full_meter_cannot_be_combined_with_other_balance_sources():
    base = {
        "plant_mode": "external",
        "forecast_min_load_w": 0,
        "sources": {"house_consumption": "sensor.house"},
    }
    additional = evaluate_plant(
        {},
        {**base, "additional_ac_sources": ["sensor.inverter"]},
        {"sensor.house": state(1000), "sensor.inverter": state(500)},
        NOW,
    )
    assert additional.errors["additional_ac_sources"] == "not_allowed_in_external_mode"
    overlap = evaluate_plant(
        {},
        {**base, "excluded_load_sources": ["sensor.house"]},
        {"sensor.house": state(1000)},
        NOW,
    )
    assert overlap.errors["plant_sources"] == "overlapping_sources"


@pytest.mark.parametrize(
    "options",
    [
        {"plant_mode": []},
        balance_options(sources=[]),
        balance_options(additional_ac_sources="sensor.one"),
        balance_options(excluded_load_sources={"sensor.one": True}),
    ],
)
def test_malformed_configuration_returns_errors_without_type_error(options):
    result = evaluate_plant(NATIVE, options, {}, NOW)
    assert result.house_w is None
    assert result.errors


def test_nonfinite_component_sums_are_rejected():
    huge_native = {
        "sensor.opti_pv_power_w": 1e308,
        "sensor.opti_grid_import_w": 1e308,
        "sensor.opti_grid_export_w": 0,
    }
    result = evaluate_plant(huge_native, balance_options(), {}, NOW)
    assert result.errors["plant_balance"] == "invalid_sum"

    result = evaluate_plant(
        NATIVE,
        balance_options(excluded_load_sources=["sensor.a", "sensor.b"]),
        {"sensor.a": state(1e308), "sensor.b": state(1e308)},
        NOW,
    )
    assert result.errors["excluded_load_sources"] == "invalid_sum"


def test_fingerprint_ignores_order_but_tracks_age_and_confirmation():
    first = balance_options(
        additional_ac_sources=["sensor.b", "sensor.a"], source_max_age=60
    )
    reordered = balance_options(
        additional_ac_sources=["sensor.a", "sensor.b"], source_max_age=60
    )
    assert plant_semantic_fingerprint(first) == plant_semantic_fingerprint(reordered)
    assert plant_semantic_fingerprint(first) != plant_semantic_fingerprint(
        {**first, "source_max_age": 61}
    )
    assert plant_semantic_fingerprint(first) != plant_semantic_fingerprint(
        {**first, "plant_meter_confirmed": False}
    )


def test_external_dependency_and_fingerprint_exclude_irrelevant_house_source():
    balance = balance_options(sources={"house_consumption": "sensor.external"})
    assert "sensor.external" not in plant_entity_ids(balance)
    assert "sensor.external" not in plant_semantic_fingerprint(balance)


def test_source_validation_does_not_require_native_measurements():
    options = balance_options(
        additional_ac_sources=["sensor.inverter"],
        excluded_load_sources=["sensor.wallbox"],
    )
    errors = validate_plant_sources(
        options,
        {
            "sensor.inverter": state(-500),
            "sensor.wallbox": state(1000),
        },
        NOW,
    )
    assert errors == {}


def test_source_validation_reports_stale_negative_and_config_errors():
    options = balance_options(
        additional_ac_sources=["sensor.inverter"],
        excluded_load_sources=["sensor.wallbox"],
    )
    errors = validate_plant_sources(
        options,
        {
            "sensor.inverter": state(500, age=901),
            "sensor.wallbox": state(-100),
        },
        NOW,
    )
    assert errors == {
        "sensor.inverter": "missing_or_stale",
        "sensor.wallbox": "negative_value",
    }
    invalid = validate_plant_sources(
        {**options, "additional_ac_sources": ["sensor.inverter", "sensor.inverter"]},
        {},
        NOW,
    )
    assert invalid["additional_ac_sources"] == "invalid_entity_list"


def test_balance_rejects_divergent_strategy_ac_override():
    options = {"plant_mode": "balance", "plant_meter_confirmed": True,
               "forecast_min_load_w": 0, "sources": {"pv_power": "sensor.other_ac"}}
    assert validate_plant_sources(options, {}, NOW)["pv_power"] == "not_allowed_in_balance_mode"
