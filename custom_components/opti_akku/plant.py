"""Pure plant-boundary balance and load-source evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Mapping

from .source_quality import measurement_max_age, reported_recently


PLANT_MODES = {"legacy", "balance", "external"}
MEASUREMENT_NOISE_TOLERANCE_W = 25.0


@dataclass(frozen=True)
class PlantConfig:
    """Validated plant options used by the pure evaluator."""

    mode: str
    additional_ac_sources: tuple[str, ...]
    excluded_load_sources: tuple[str, ...]
    event_based_excluded_sources: tuple[str, ...]
    external_house_source: str | None
    source_max_age: float
    forecast_min_load_w: float | None
    meter_confirmed: bool


@dataclass(frozen=True)
class PlantResult:
    """Raw plant load and its quality information."""

    house_w: float | None
    base_load_w: float | None
    forecast_input_w: float | None
    errors: dict[str, str]
    attributes: dict[str, Any]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _entity_ids(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result: list[str] = []
    for entity_id in value:
        if not isinstance(entity_id, str) or not entity_id.strip():
            return None
        entity_id = entity_id.strip()
        if entity_id in result:
            return None
        result.append(entity_id)
    return tuple(result)


def _config(options: Mapping[str, Any]) -> tuple[PlantConfig | None, dict[str, str]]:
    errors: dict[str, str] = {}
    mode = options.get("plant_mode", "legacy")
    if not isinstance(mode, str) or mode not in PLANT_MODES:
        errors["plant_mode"] = "invalid_mode"
        return None, errors

    additional = _entity_ids(options.get("additional_ac_sources", []))
    excluded = _entity_ids(options.get("excluded_load_sources", []))
    if additional is None:
        errors["additional_ac_sources"] = "invalid_entity_list"
        additional = ()
    if excluded is None:
        errors["excluded_load_sources"] = "invalid_entity_list"
        excluded = ()
    event_based = _entity_ids(options.get("event_based_excluded_sources", []))
    if event_based is None:
        errors["event_based_excluded_sources"] = "invalid_entity_list"
        event_based = ()
    elif not set(event_based) <= set(excluded):
        errors["event_based_excluded_sources"] = "not_an_excluded_source"

    max_age = _finite(options.get("source_max_age", 900))
    if max_age is None or max_age < 0:
        errors["source_max_age"] = "invalid_value"
        max_age = 900.0

    floor = None
    if mode != "legacy":
        floor = _finite(options.get("forecast_min_load_w"))
        if floor is None or not 0 <= floor <= 5000:
            errors["forecast_min_load_w"] = "invalid_value"

    sources = options.get("sources", {})
    if not isinstance(sources, Mapping):
        errors["sources"] = "invalid_mapping"
        sources = {}
    external = sources.get("house_consumption")
    if external is not None and (not isinstance(external, str) or not external.strip()):
        errors["house_consumption"] = "invalid_entity_id"
        external = None
    elif isinstance(external, str):
        external = external.strip()
    if mode == "external" and external is None:
        errors["house_consumption"] = "source_required"
    if mode == "external" and additional:
        errors["additional_ac_sources"] = "not_allowed_in_external_mode"
    if set(additional) & set(excluded):
        errors["plant_sources"] = "overlapping_sources"
    if mode == "external" and external in excluded:
        errors["plant_sources"] = "overlapping_sources"
    if mode == "balance" and sources.get("pv_power"):
        errors["pv_power"] = "not_allowed_in_balance_mode"
    if mode == "balance" and options.get("plant_meter_confirmed") is not True:
        errors["plant_meter_confirmed"] = "confirmation_required"

    return PlantConfig(
        mode=mode,
        additional_ac_sources=additional,
        excluded_load_sources=excluded,
        event_based_excluded_sources=event_based,
        external_house_source=external,
        source_max_age=max_age,
        forecast_min_load_w=floor,
        meter_confirmed=options.get("plant_meter_confirmed") is True,
    ), errors


def _ha_power(
    entity_id: str,
    ha_states: Any,
    now: datetime,
    max_age: float,
    *,
    signed: bool,
    allow_idle_zero: bool = False,
) -> tuple[float | None, str | None]:
    max_age = measurement_max_age(max_age, event_based=True)
    state = ha_states.get(entity_id)
    if state is None:
        return None, "missing_or_stale"
    value = _finite(getattr(state, "state", None))
    if not reported_recently(state, now, max_age) and not (
        allow_idle_zero and value == 0 and reported_recently(state, now, math.inf)
    ):
        return None, "missing_or_stale"
    if value is None:
        return None, "invalid_value"
    attributes = getattr(state, "attributes", {})
    unit = attributes.get("unit_of_measurement") if isinstance(attributes, Mapping) else None
    if unit == "kW":
        value *= 1000
    elif unit != "W":
        return None, "unsupported_unit"
    if not math.isfinite(value):
        return None, "invalid_value"
    if not signed and value < 0:
        return None, "negative_value"
    return value, None


def _invalid_result(config: PlantConfig | None, errors: dict[str, str]) -> PlantResult:
    return PlantResult(
        house_w=None,
        base_load_w=None,
        forecast_input_w=None,
        errors=errors,
        attributes={
            "plant_mode": config.mode if config else None,
            "forecast_min_load_w": config.forecast_min_load_w if config else None,
            "noise_tolerance_w": MEASUREMENT_NOISE_TOLERANCE_W,
        },
    )


def evaluate_plant(
    measurements: Mapping[str, Any],
    options: Mapping[str, Any],
    ha_states: Any,
    now: datetime,
) -> PlantResult:
    """Evaluate a raw plant balance without smoothing or hidden fallbacks."""
    config, errors = _config(options)
    if config is None:
        return _invalid_result(None, errors)
    if config.mode == "legacy":
        return _invalid_result(config, errors)
    if errors:
        return _invalid_result(config, errors)

    components: dict[str, float] = {}
    if config.mode == "external":
        assert config.external_house_source is not None
        house, error = _ha_power(
            config.external_house_source, ha_states, now, config.source_max_age, signed=False
        )
        if error:
            errors["house_consumption"] = error
            return _invalid_result(config, errors)
        assert house is not None
        components["external_house_w"] = house
    else:
        native_keys = {
            "pv_power": "sensor.opti_pv_power_w",
            "grid_import": "sensor.opti_grid_import_w",
            "grid_export": "sensor.opti_grid_export_w",
        }
        native: dict[str, float] = {}
        for key, measurement_key in native_keys.items():
            value = _finite(measurements.get(measurement_key))
            if value is None:
                errors[key] = "invalid_value"
            elif key != "pv_power" and value < 0:
                errors[key] = "negative_value"
            else:
                native[key] = value
        additional_total = 0.0
        for entity_id in config.additional_ac_sources:
            value, error = _ha_power(entity_id, ha_states, now, config.source_max_age, signed=True)
            if error:
                errors[entity_id] = error
            else:
                assert value is not None
                additional_total += value
                if not math.isfinite(additional_total):
                    errors["additional_ac_sources"] = "invalid_sum"
        if errors:
            return _invalid_result(config, errors)
        components = {
            "native_ac_w": native["pv_power"],
            "additional_ac_w": additional_total,
            "grid_import_w": native["grid_import"],
            "grid_export_w": native["grid_export"],
        }
        house = sum((native["pv_power"], additional_total, native["grid_import"], -native["grid_export"]))
        if not math.isfinite(house):
            errors["plant_balance"] = "invalid_sum"
            return _invalid_result(config, errors)
        if house < -MEASUREMENT_NOISE_TOLERANCE_W:
            errors["plant_balance"] = "negative_balance"
            return _invalid_result(config, errors)
        house = max(0.0, house)

    excluded_total = 0.0
    stale_zero_sources: list[str] = []
    for entity_id in config.excluded_load_sources:
        value, error = _ha_power(
            entity_id, ha_states, now, config.source_max_age, signed=False,
            allow_idle_zero=entity_id in config.event_based_excluded_sources,
        )
        if error:
            errors[entity_id] = error
        else:
            assert value is not None
            if not reported_recently(ha_states.get(entity_id), now, measurement_max_age(config.source_max_age, event_based=True)):
                stale_zero_sources.append(entity_id)
            excluded_total += value
            if not math.isfinite(excluded_total):
                errors["excluded_load_sources"] = "invalid_sum"
    if errors:
        return _invalid_result(config, errors)

    base_load = house - excluded_total
    # Both operands are currently validated as finite and non-negative. Keep this
    # guard for future balance inputs that may relax either invariant.
    if not math.isfinite(base_load):
        errors["base_load"] = "invalid_sum"
        return _invalid_result(config, errors)
    if base_load < -MEASUREMENT_NOISE_TOLERANCE_W:
        errors["excluded_load_sources"] = "exclusions_exceed_house"
        return _invalid_result(config, errors)
    base_load = max(0.0, base_load)
    attributes: dict[str, Any] = {
        "plant_mode": config.mode,
        "components": components,
        "excluded_load_w": excluded_total,
        "stale_zero_sources": stale_zero_sources,
        "forecast_min_load_w": config.forecast_min_load_w,
        "noise_tolerance_w": MEASUREMENT_NOISE_TOLERANCE_W,
    }
    return PlantResult(house, base_load, base_load, {}, attributes)


def plant_entity_ids(options: Mapping[str, Any]) -> tuple[str, ...]:
    """Return configured HA entity dependencies in stable order."""
    config, errors = _config(options)
    if config is None or errors or config.mode == "legacy":
        return ()
    result: list[str] = []
    if config.mode == "external" and config.external_house_source:
        result.append(config.external_house_source)
    for entity_id in (*config.additional_ac_sources, *config.excluded_load_sources):
        if entity_id not in result:
            result.append(entity_id)
    return tuple(result)


def validate_plant_sources(
    options: Mapping[str, Any], ha_states: Any, now: datetime
) -> dict[str, str]:
    """Validate configured HA plant sources without requiring native readings."""
    config, errors = _config(options)
    if config is None or config.mode == "legacy":
        return errors
    if config.mode == "external" and config.external_house_source:
        _, error = _ha_power(
            config.external_house_source,
            ha_states,
            now,
            config.source_max_age,
            signed=False,
        )
        if error:
            errors["house_consumption"] = error
    for entity_id in config.additional_ac_sources:
        _, error = _ha_power(entity_id, ha_states, now, config.source_max_age, signed=True)
        if error:
            errors[entity_id] = error
    for entity_id in config.excluded_load_sources:
        _, error = _ha_power(
            entity_id, ha_states, now, config.source_max_age, signed=False,
            allow_idle_zero=entity_id in config.event_based_excluded_sources,
        )
        if error:
            errors[entity_id] = error
    return errors


def plant_semantic_fingerprint(options: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return stable semantics that require a new load-statistics series."""
    config, errors = _config(options)
    if config is None or errors:
        return ("invalid", tuple(sorted(errors.items())))
    fingerprint = (
        config.mode,
        tuple(sorted(config.additional_ac_sources)),
        tuple(sorted(config.excluded_load_sources)),
        config.external_house_source if config.mode == "external" else None,
        config.source_max_age,
        config.meter_confirmed,
    )
    # Preserve existing learned profiles when the new option is unused.
    if config.mode != "legacy" and config.event_based_excluded_sources:
        return (*fingerprint, tuple(sorted(config.event_based_excluded_sources)))
    return fingerprint
