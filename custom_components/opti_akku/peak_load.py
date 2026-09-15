"""Opt-in hourly load estimates for the existing price-peak horizon.

No PV/night reserve substitution and no mutation of observation or learned data.
"""

from datetime import timedelta
import json

from .demand import instant, number, source_value
from .demand_history import CONTEXT_KEYS


def peak_load_profile(model, now, data, settings, options, states, timezone, fingerprint):
    cfg = options.get("demand_forecast", {})
    result = {
        "status": "disabled",
        "hours": {},
        "methods": {},
        "margin_percent": 20,
        "thermal_model": "included_in_house_profile; future_cycles_not_scheduled",
    }
    if not (cfg.get("enabled") is True and cfg.get("use_for_peak_reserve") is True):
        return result
    result["status"] = "fallback"
    now = instant(now)
    sources = cfg.get("sources", {})
    expected_binding = json.dumps(
        [fingerprint, {k: v for k, v in sources.items() if k not in CONTEXT_KEYS}, str(timezone)],
        sort_keys=True,
    )
    house = number(data.get("states", {}).get("sensor.opti_house_consumption_w"))
    fallback = number(settings.get("input_number.opti_peak_verbrauch_kw"))
    if (
        not data.get("online")
        or house is None
        or not 0 <= house <= 50000
        or data.get("source_errors")
        or fallback is None
        or fallback <= 0
    ):
        return {**result, "reason": "invalid_current_inputs"}
    if model.fingerprint != expected_binding:
        return {**result, "reason": "profile_binding_changed"}
    if sources.get("heat_power"):
        return {**result, "reason": "separate_heat_model_not_supported_for_peaks"}
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
    if flags["dhw_due"]:
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
        if actual < target - 2:
            return {**result, "reason": "unquantified_water_temperature_deficit"}
    history_ok = model.history.binding == model.history_binding(fingerprint, options, timezone)

    def expected(at):
        online = model._expected(at, context, timezone, before=now)
        if online is not None:
            return sum(online[:2]), "measured_profile"
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
        if value is None or not 0 <= value <= 50000:
            value, method = fallback * 1000, "fixed_setting"
        else:
            # Forecast uncertainty is separate from switching hysteresis.
            value = value * 1.2 + uplift * max(0, 1 - max(0, (at - now).total_seconds()) / 14400)
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
