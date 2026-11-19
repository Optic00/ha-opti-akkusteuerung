"""Opt-in hourly load estimates for the existing price-peak horizon.

No PV/night reserve substitution and no mutation of observation or learned data.
"""

from datetime import UTC, datetime, timedelta

from .demand import instant, number, profile_house_value, source_value

# A planned hot-water start further ahead says nothing reliable about the runs
# before it; the learned hot-water share then stays in every hour (#248).
PLANNED_DHW_MAX_LEAD = timedelta(hours=24)
DEFAULT_DHW_RUN_MINUTES = 75
DHW_RUN_MINUTES_RANGE = (15, 240)


def _planned_start(state, timezone):
    """Timestamp sensor or date-and-time ``input_datetime``; unknown never means now."""
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    attributes = getattr(state, "attributes", None) or {}
    if "has_date" in attributes or "has_time" in attributes:
        if not (attributes.get("has_date") and attributes.get("has_time")):
            return None
        stamp = number(attributes.get("timestamp"))
        if stamp is not None:
            try:
                return datetime.fromtimestamp(stamp, UTC)
            except OverflowError, OSError, ValueError:
                return None
    try:
        value = datetime.fromisoformat(state.state)
    except TypeError, ValueError:
        return None
    if value.utcoffset() is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(UTC)


def planned_dhw_run(cfg, states, now, timezone):
    """Next scheduled hot-water run from a user automation, or why none is used."""
    entity = cfg.get("dhw_planned_start")
    if not entity:
        return {"status": "not_configured"}
    result = {"status": "unknown_source", "entity": entity}
    sources = cfg.get("sources", {})
    cycle = number(cfg.get("dhw_cycle_kwh", 0))
    minutes = number(cfg.get("dhw_run_minutes", DEFAULT_DHW_RUN_MINUTES))
    if not sources.get("heat_power") or not sources.get("dhw_active") or cycle is None or cycle <= 0:
        return {**result, "status": "requires_meter_and_cycle_energy"}
    low, high = DHW_RUN_MINUTES_RANGE
    if minutes is None or not low <= minutes <= high:
        return {**result, "status": "invalid_duration"}
    start = _planned_start(states.get(entity), timezone)
    if start is None:
        return result
    end = start + timedelta(minutes=minutes)
    result.update(start=start.isoformat(), end=end.isoformat(), kwh=round(cycle, 3))
    if end <= now:
        return {**result, "status": "stale"}
    if start - now > PLANNED_DHW_MAX_LEAD:
        return {**result, "status": "too_far"}
    return {
        **result,
        "status": "booked",
        "_start": start,
        "_end": end,
        "_power_w": cycle * 1000 / (minutes / 60),
    }


def peak_load_profile(model, now, data, settings, options, states, timezone, fingerprint):
    cfg = options.get("demand_forecast", {})
    result = {
        "status": "disabled",
        "hours": {},
        "methods": {},
        "margin_percent": 20,
        "thermal_model": "included_in_house_profile; future_cycles_not_scheduled",
        "planned_dhw": {"status": "not_configured"},
    }
    if not (cfg.get("enabled") is True and cfg.get("use_for_peak_reserve") is True):
        return result
    result["status"] = "fallback"
    now = instant(now)
    # Shown even when another input forces the fixed load, for diagnosis.
    plan = planned_dhw_run(cfg, states, now, timezone)
    result["planned_dhw"] = {k: v for k, v in plan.items() if not k.startswith("_")}
    booked = plan["status"] == "booked"
    sources = cfg.get("sources", {})
    expected_binding = model.profile_binding(fingerprint, options, timezone)
    house, house_error = profile_house_value(data, options, states, now)
    fallback = number(settings.get("input_number.opti_peak_verbrauch_kw"))
    if (
        house is None
        or house_error is not None
        or data.get("source_errors")
        or fallback is None
        or fallback <= 0
    ):
        return {**result, "reason": "invalid_current_inputs"}
    if model.fingerprint != expected_binding:
        return {**result, "reason": "profile_binding_changed"}
    separate_meter = bool(sources.get("heat_power"))
    if separate_meter:
        result["thermal_model"] = "separate_meter; hot_water_learned_unless_planned"
    flags = {
        k: source_value(states, sources.get(k), now, kind="flag")
        for k in ("summer_mode", "heating_active", "dhw_active", "dhw_due")
    }
    if any(sources.get(k) and v is None for k, v in flags.items()):
        return {**result, "reason": "unknown_thermal_context"}
    if flags["heating_active"] and flags["dhw_active"]:
        return {**result, "reason": "conflicting_thermal_context"}
    context = (
        "unknown"
        if flags["summer_mode"] is None
        else "summer"
        if flags["summer_mode"]
        else "winter"
    )
    # A pending cycle has no dependable time/energy calibration here. Keep the
    # established load assumption, never silently lower it using ordinary days.
    # A booked plan from the user's automation quantifies it (#248).
    if flags["dhw_due"] and not booked:
        return {**result, "reason": "unquantified_dhw_due"}
    for actual_key, target_key in (
        ("water_temperature", "water_target"),
        ("context_water_temperature", "context_water_target"),
    ):
        if not (sources.get(actual_key) or sources.get(target_key)):
            continue
        actual = source_value(states, sources.get(actual_key), now, kind="temperature")
        target = source_value(states, sources.get(target_key), now, kind="target")
        if actual is None or target is None:
            return {**result, "reason": "unknown_water_temperature"}
        if actual < target - 2 and not booked:
            return {**result, "reason": "unquantified_water_temperature_deficit"}
    history_ok = model.history.binding == model.history_binding(fingerprint, options, timezone)

    def expected(at):
        # Until the planned run ends, its learned share at the old time is
        # replaced by the booking; later runs are unknown and keep it.
        without_dhw = booked and at < plan["_end"]
        online = model.expected_house(
            at, context, timezone, before=now, without_dhw=without_dhw
        )
        if online is not None:
            return online, (
                "measured_profile_without_dhw" if without_dhw else "measured_profile"
            )
        prior = (
            model.history.expected(at, now, timezone, context, model.cells) if history_ok else None
        )
        if prior:
            return prior["house_w"], "historical_profile"
        return None, "fixed_setting"

    current, _ = expected(now)
    recent_fresh = (
        model.previous is not None and 0 <= (now - model.previous[0]).total_seconds() <= 90
    )
    # The public snapshot from the last observation carries its covered mean.
    uplift = 0.0
    if recent_fresh and current is not None:
        summary = data.get("demand_forecast", {})
        uplift = max(0.0, number(summary.get("extra_base_load_w")) or 0.0)
    thermal_active = flags["heating_active"] or flags["dhw_active"]
    if thermal_active:
        uplift = 0.0
    result.update(
        context=context,
        extra_base_load_w=round(uplift, 1),
        heating_active=flags["heating_active"],
        dhw_active=flags["dhw_active"],
    )
    start = now.replace(minute=0, second=0, microsecond=0)
    for i in range(37):
        at = start + timedelta(hours=i)
        value, method = expected(at)
        planned_w = 0.0
        if booked:
            overlap = (
                min(at + timedelta(hours=1), plan["_end"]) - max(at, plan["_start"])
            ).total_seconds()
            planned_w = plan["_power_w"] * max(0.0, overlap) / 3600
        if value is None or not 0 <= value <= 50000:
            value, method = fallback * 1000, "fixed_setting"
            # The fixed assumption covers ordinary hours, not a whole extra cycle.
            value += planned_w
        else:
            # Forecast uncertainty is separate from switching hysteresis. The
            # planned cycle shares the margin, so planners divide it out alike.
            value = (value + planned_w) * 1.2 + uplift * max(
                0, 1 - max(0, (at - now).total_seconds()) / 14400
            )
        if planned_w > 0:
            method += "+planned_dhw"
        if thermal_active and i <= 1:
            value = max(value, house, fallback * 1000)
            method += "+active_heat_floor"
        key = str(int(at.timestamp()))
        result["hours"][key] = round(min(50000, value), 1)
        result["methods"][key] = method
    result["status"] = (
        "profile" if any("profile" in v for v in result["methods"].values()) else "fallback"
    )
    return result
