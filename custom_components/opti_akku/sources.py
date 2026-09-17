"""Normalize explicitly selected HA sources into the strategy's units.

Missing samples remain missing. In particular, price slots are never removed
or shifted when an upstream value is invalid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import SOURCE_DEFINITIONS
from .plant import evaluate_plant

if TYPE_CHECKING:
    from .tibber_prices import TibberPriceSnapshot

PRICE_UNITS = {"eur/kwh": "EUR/kWh", "€/kwh": "EUR/kWh",
               "ct/kwh": "ct/kWh", "cent/kwh": "ct/kWh", "cents/kwh": "ct/kWh"}


def normalize_price_unit(unit: Any) -> str | None:
    """Normalize only known units; missing or malformed units stay invalid."""
    return PRICE_UNITS.get(unit.strip().casefold()) if isinstance(unit, str) else None


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _factor(family: str, unit: str | None, price_unit: str) -> float:
    unit = unit.strip() if isinstance(unit, str) else ""
    if family == "power":
        return {"W": 1, "kW": 1000}[unit]
    if family == "energy":
        return {"kWh": 1, "Wh": 0.001, "MWh": 1000}[unit]
    if family == "price":
        # Reject unsupported scales instead of silently treating EUR/MWh as EUR/kWh.
        if normalize_price_unit(unit) != price_unit:
            raise ValueError("Price source unit does not match configured unit")
        return {"EUR/kWh": 100, "ct/kWh": 1}[price_unit]
    if family == "voltage_spread":
        return {"mV": 1, "V": 1000}[unit]
    raise ValueError(f"Unknown source family: {family}")


def _fresh(state: Any, now: datetime, max_age: float) -> bool:
    reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    if not isinstance(reported, datetime):
        return False
    age = (now - reported).total_seconds()
    return -60 <= age <= max_age


def _price_timestamp(value: Any) -> datetime:
    """Require an explicit instant; sensor reporting time never dates a price."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Price interval needs an explicit timezone")
    try:
        return value.astimezone(UTC)
    except OverflowError as err:
        raise ValueError("Price interval timestamp is outside the supported range") from err


def _price_slots(raw: Any, factor: float, day_start: datetime) -> list[float]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("Missing price slots")
    # The engine consumes a positional grid. Prove its calendar membership and
    # continuity before dropping timestamps, using UTC through DST transitions.
    expected_start = day_start.astimezone(UTC)
    day_end = (day_start + timedelta(days=1)).astimezone(UTC)
    duration = (day_end - expected_start).total_seconds() / len(raw)
    if duration not in (900, 3600):
        raise ValueError("Expected a complete hourly or quarter-hour local day")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Undated price slots cannot identify their local day")
        start = _price_timestamp(item.get("start"))
        end = _price_timestamp(item.get("end"))
        if start != expected_start or (end - start).total_seconds() != duration:
            raise ValueError("Price intervals must cover the local day without gaps or overlaps")
        value = finite(item.get("total", item.get("price", item.get("value"))))
        if value is None or finite(value * factor) is None:
            raise ValueError("Invalid price slot")
        result.append(value * factor)
        expected_start = end
    if expected_start != day_end:
        raise ValueError("Price intervals do not end at the next local midnight")
    return result


def normalize_price_series(raw_today: Any, raw_tomorrow: Any, factor: float, now: datetime) -> dict[str, list[float]]:
    """Use explicit interval dates even if an upstream bucket name has not rotated."""
    today = dt_util.start_of_local_day(dt_util.as_local(now))
    tomorrow = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)
    days: dict[datetime, list[float]] = {}
    for raw in (raw_today, raw_tomorrow):
        if raw is None or raw in ([], ()):
            continue
        if not isinstance(raw, (list, tuple)) or not isinstance(raw[0], dict):
            raise ValueError("Missing dated price intervals")
        first = dt_util.as_local(_price_timestamp(raw[0].get("start")))
        day = dt_util.start_of_local_day(first)
        if day not in (yesterday, today, tomorrow) or day in days:
            raise ValueError("Duplicate or out-of-horizon price day")
        # Validate every supplied bucket, including yesterday's. Corrupt future
        # data must not silently become a shorter, apparently valid plan.
        days[day] = _price_slots(raw, factor, day)
    if today not in days:
        raise ValueError("Price snapshot does not cover the current local day")
    return {"today": days[today], "tomorrow": days.get(tomorrow, [])}


def build_inputs(
    measurements: dict[str, Any],
    options: dict[str, Any],
    ha_states: Any,
    now: datetime,
    *,
    price_snapshot: TibberPriceSnapshot | None = None,
) -> tuple[dict[str, Any], dict[str, dict], dict[str, str]]:
    """Build independent virtual states without creating any helper entities."""
    states = dict(measurements)
    attributes: dict[str, dict] = {}
    errors: dict[str, str] = {}
    sources = options.get("sources", {})
    price_provider = options.get("price_provider", "entities")
    if options.get("single_inverter"):
        balance = finite(measurements.get("sensor.opti_house_balance_w"))
        if balance is not None:
            states["sensor.opti_house_consumption_w"] = max(0, balance)
    for key, (canonical, _label, family) in SOURCE_DEFINITIONS.items():
        if family == "price" and price_provider != "entities":
            states[canonical] = "unavailable"
            continue
        if key == "house_consumption" and options.get("plant_mode", "legacy") != "legacy":
            continue
        entity_id = sources.get(key)
        if not entity_id:
            states.setdefault(canonical, "unavailable")
            continue
        source = ha_states.get(entity_id)
        states[canonical] = "unavailable"
        max_age = options.get("source_max_age", 900)
        if family == "energy":
            max_age = options.get("forecast_max_age", 21600)
        elif family == "price":
            max_age = options.get("price_max_age", 7200)
        if source is None or not _fresh(source, now, max_age):
            errors[key] = "missing_or_stale"
            continue
        value = finite(source.state)
        if value is None:
            errors[key] = "invalid_value"
            continue
        try:
            factor = _factor(family, source.attributes.get("unit_of_measurement"), options.get("price_unit", "EUR/kWh"))
        except (KeyError, ValueError):
            errors[key] = "unsupported_unit"
            continue
        value *= factor
        if finite(value) is None:
            errors[key] = "invalid_value"
            continue
        if family != "price" and key != "pv_power" and value < 0:
            errors[key] = "negative_value"
            continue
        if key == "price_series":
            try:
                series = normalize_price_series(source.attributes.get("today"), source.attributes.get("tomorrow"), factor, now)
            except ValueError:
                errors[key] = "invalid_price_series"
                continue
            attributes[canonical] = series
        elif key.startswith("forecast_"):
            p10 = finite(source.attributes.get("estimate10"))
            attributes[canonical] = {"estimate10": p10 * factor if p10 is not None and p10 >= 0 else None}
        states[canonical] = value

    if price_provider != "entities":
        error = None
        if price_provider != "tibber":
            error = "invalid_provider"
        elif options.get("price_unit") != "EUR/kWh" or options.get("tibber_eur_confirmed") is not True:
            error = "tibber_eur_confirmation_required"
        elif not isinstance(options.get("tibber_home"), str) or not options["tibber_home"]:
            error = "tibber_home_required"
        elif not isinstance(options.get("tibber_entry_id"), str) or not options["tibber_entry_id"]:
            error = "tibber_entry_required"
        elif finite(options.get("price_max_age", 7200)) is None or float(options.get("price_max_age", 7200)) < 60:
            error = "invalid_price_max_age"
        elif price_snapshot is None:
            error = "missing_or_stale"
        elif price_snapshot.entry_id != options["tibber_entry_id"]:
            error = "tibber_entry_mismatch"
        else:
            try:
                current, series, period = price_snapshot.normalized(now, options.get("price_max_age", 7200), options["tibber_home"])
            except ValueError as err:
                error = getattr(err, "code", "invalid_price_series")
            else:
                states["sensor.opti_price_current_ct_kwh"] = current
                states["sensor.opti_price_series"] = current
                attributes["sensor.opti_price_series"] = series
                attributes["sensor.opti_price_current_ct_kwh"] = period
        if error:
            errors.update(price_current=error, price_series=error)

    # EV state is derived from the actual charging flag AND evcc mode. Smart
    # Cost additionally marks grid charging in pv mode when explicitly mapped.
    # These three inputs are persistent state or setting entities which
    # integrations may only report when their value changes. Their age
    # therefore says nothing about availability; unknown/unavailable values
    # remain invalid.
    # Unconfigured loadpoints do not participate in the latch; configured but
    # missing/invalid inputs must still hold its lock.
    for index in (1, 2):
        mode = ha_states.get(sources.get(f"ev{index}_mode", ""))
        charging = ha_states.get(sources.get(f"ev{index}_charging", ""))
        smart_cost_key = f"ev{index}_smart_cost"
        smart_cost_configured = bool(sources.get(smart_cost_key))
        smart_cost = ha_states.get(sources.get(smart_cost_key, ""))
        power = ha_states.get(sources.get(f"ev{index}_power", ""))
        key = f"binary_sensor.opti_ev_lp{index}_schnell"
        if not any(sources.get(f"ev{index}_{field}")
                   for field in ("mode", "charging", "smart_cost", "power")):
            states[key] = "off"
            attributes[key] = {"valide": True, "konfiguriert": False}
            continue
        base_valid = (mode is not None and charging is not None
                      and mode.state in ("off", "now", "minpv", "pv")
                      and charging.state in ("on", "off"))
        smart_cost_valid = (smart_cost is not None
                            and smart_cost.state in ("on", "off"))
        if not base_valid:
            states[key] = "unavailable"
            for field in (f"ev{index}_mode", f"ev{index}_charging"):
                if sources.get(field):
                    errors[field] = "missing_or_unavailable"
        elif charging.state == "on" and mode.state in ("now", "minpv"):
            states[key] = "on"
        elif (charging.state == "on" and mode.state == "pv"
              and smart_cost_configured and not smart_cost_valid):
            states[key] = "unavailable"
            errors[smart_cost_key] = "missing_or_unavailable"
        else:
            states[key] = ("on" if charging.state == "on" and mode.state == "pv"
                           and smart_cost_configured and smart_cost.state == "on" else "off")
        watts = finite(power.state) if power is not None else None
        if watts is not None and power.attributes.get("unit_of_measurement") == "kW":
            watts *= 1000
        attributes[key] = {"valide": states[key] != "unavailable",
                           "modus": mode.state if mode else None,
                           "charging": charging.state if charging else None,
                           "smart_cost_active": smart_cost.state if smart_cost else None,
                           "leistung_kw": watts / 1000 if watts is not None else None}
    if options.get("plant_mode", "legacy") != "legacy":
        plant = evaluate_plant(measurements, options, ha_states, now)
        errors.update({f"plant:{key}": value for key, value in plant.errors.items()})
        states["sensor.opti_house_raw_w"] = plant.house_w if plant.house_w is not None else "unavailable"
        states["sensor.opti_base_load_raw_w"] = plant.base_load_w if plant.base_load_w is not None else "unavailable"
        states["sensor.opti_house_consumption_w"] = plant.house_w if plant.house_w is not None else "unavailable"
        attributes["sensor.opti_house_raw_w"] = {**plant.attributes, "quality_errors": plant.errors}
    return states, attributes, errors
