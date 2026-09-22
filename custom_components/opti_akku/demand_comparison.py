"""Passive profile-based comparisons for the active forecast strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from .demand import DemandForecast, instant, number, source_value
from .engine import TARGET_SOC_BOUNDS as _BOUNDS, TARGET_SOC_MARGIN as _MARGIN, TARGET_SOC_LEVELS


@dataclass(slots=True)
class _Window:
    start: datetime
    end: datetime
    energy_kwh: float
    status: str
    reason: str
    sources: dict[str, dict[str, float]]
    extra_base_load_w: float
    recent_coverage_seconds: float

    def report(self) -> dict[str, Any]:
        return {
            "profile_energy_kwh": round(self.energy_kwh, 3),
            "window_hours": round((self.end - self.start).total_seconds() / 3600, 3),
            "recent_coverage_seconds": round(self.recent_coverage_seconds),
            "extra_base_load_w": round(self.extra_base_load_w, 1),
            "profile_sources": {
                key: {"seconds": round(value["seconds"]), "kwh": round(value["kwh"], 3)}
                for key, value in self.sources.items()
            },
        }


def _value(states: dict[str, Any], key: str) -> float | None:
    value = number(states.get(key))
    return float(value) if value is not None else None


def _attribute(attributes: dict[str, Any], key: str, name: str) -> float | None:
    value = attributes.get(key, {})
    return number(value.get(name)) if isinstance(value, dict) else None


def _timestamp(attributes: dict[str, Any], key: str, name: str) -> datetime | None:
    value = attributes.get(key, {})
    if not isinstance(value, dict) or value.get(name) is None:
        return None
    try:
        result = instant(value[name])
        return result if isinstance(result, datetime) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _effective(median_kwh: float, p10_kwh: float | None, alpha: float) -> float:
    p10 = p10_kwh if p10_kwh is not None and p10_kwh > 0 else median_kwh
    return min(median_kwh, alpha * median_kwh + (1 - alpha) * p10)


def _day_bounds(day: date, timezone: Any) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone).astimezone(UTC)
    return start, end


def _extra_wh(extra_w: float, start: datetime, end: datetime, issued_at: datetime) -> float:
    """Integrate a four-hour linear ramp exactly over one segment."""
    begin = max(0.0, (start - issued_at).total_seconds())
    finish = min(14400.0, (end - issued_at).total_seconds())
    if finish <= begin:
        return 0.0
    area_seconds = (finish - begin) - (finish * finish - begin * begin) / 28800.0
    return extra_w * area_seconds / 3600


def _dhw_timing_unknown(
    report: dict[str, Any],
    options: dict[str, Any],
    ha_states: Any,
    issued_at: datetime,
) -> bool:
    """Keep an unplaced hot-water need visible when the main report cannot."""
    extra = number(report.get("dhw_extra_kwh"))
    if extra is not None:
        return bool(extra > 0)
    cfg = options.get("demand_forecast", {})
    sources = cfg.get("sources", {}) if isinstance(cfg, dict) else {}
    due_entity = sources.get("dhw_due")
    if due_entity:
        due = source_value(ha_states, due_entity, issued_at, kind="flag")
        if due is None or due is True:
            return True
    actual_entity = sources.get("water_temperature")
    target_entity = sources.get("water_target")
    if actual_entity or target_entity:
        actual = source_value(ha_states, actual_entity, issued_at, kind="temperature")
        target = source_value(ha_states, target_entity, issued_at, kind="target")
        if actual is None or target is None or actual < target - 2:
            return True
    return False


def _window(
    model: DemandForecast,
    start: datetime,
    end: datetime,
    issued_at: datetime,
    report: dict[str, Any],
    settings: dict[str, Any],
    options: dict[str, Any],
    ha_states: Any,
    timezone: Any,
    fingerprint: str,
    dhw_timing_unknown: bool,
) -> _Window | None:
    if end <= start:
        return None
    context = report.get("context")
    if context not in ("summer", "winter", "unknown"):
        return None
    cfg = options.get("demand_forecast", {})
    sources = cfg.get("sources", {}) if isinstance(cfg, dict) else {}
    heat_meter = bool(sources.get("heat_power"))
    heat = source_value(
        ha_states,
        sources.get("heat_power"),
        issued_at,
        kind="power",
        source_max_age=options.get("source_max_age", 900),
    ) if heat_meter else 0.0
    if heat is None or heat < 0:
        return None
    heating = report.get("heating_active") is True
    dhw = report.get("dhw_active") is True
    active_heat = heat_meter and (heating or dhw)
    fallback_kw = number(settings.get("input_number.opti_peak_verbrauch_kw"))
    extra = number(report.get("extra_base_load_w")) or 0.0
    extra = max(0.0, extra)
    recent = number(report.get("recent_coverage_seconds")) or 0.0
    outdoor = None
    if cfg.get("temperature_matching") is True:
        temperature_context = report.get("temperature_context")
        if isinstance(temperature_context, dict):
            outdoor = number(temperature_context.get("outdoor_temperature"))
    binding = model.history_binding(fingerprint, options, timezone)
    history_usable = not heat_meter and model.history.binding == binding
    totals = {
        "online": {"seconds": 0.0, "kwh": 0.0},
        "historical": {"seconds": 0.0, "kwh": 0.0},
        "fallback": {"seconds": 0.0, "kwh": 0.0},
    }
    cursor = start.astimezone(UTC)
    end = end.astimezone(UTC)
    issued_at = issued_at.astimezone(UTC)
    while cursor < end:
        local = cursor.astimezone(timezone)
        elapsed = local.minute * 60 + local.second + local.microsecond / 1_000_000
        next_hour = cursor + timedelta(seconds=3600 - elapsed if elapsed else 3600)
        segment_end = min(end, next_hour)
        expected = model._expected(cursor, context, timezone, before=issued_at)
        source = "online"
        if expected is None and history_usable:
            prior = model.history.expected(
                cursor, issued_at, timezone, context, model.cells, outdoor
            )
            if prior is not None:
                expected = (prior["house_w"], 0.0, 0.0)
                source = "historical"
        if expected is None:
            if fallback_kw is None or fallback_kw <= 0:
                return None
            expected = (fallback_kw * 1000, 0.0, 0.0)
            source = "fallback"
        seconds = (segment_end - cursor).total_seconds()
        base_w, thermal_w, _dhw_w = expected
        energy_wh = (base_w + thermal_w) * seconds / 3600
        # A current heat measurement is evidence only for the first future
        # hour. Full-day windows can include past hours and partial segments.
        if source != "fallback" and active_heat and cursor < issued_at + timedelta(hours=1):
            overlap_start = max(cursor, issued_at)
            overlap_end = min(segment_end, issued_at + timedelta(hours=1))
            overlap_seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
            energy_wh += max(0.0, heat - thermal_w) * overlap_seconds / 3600
        if source != "fallback":
            energy_wh += _extra_wh(extra, cursor, segment_end, issued_at)
        totals[source]["seconds"] += seconds
        totals[source]["kwh"] += energy_wh / 1000
        cursor = segment_end
    used = {key: value for key, value in totals.items() if value["seconds"] > 0}
    if set(used) == {"online"} and recent >= 1200:
        status, reason = "ready", "complete_online_profile"
    elif "fallback" in used:
        status, reason = "learning", "fixed_fallback_used"
    elif "historical" in used:
        status, reason = "learning", "historical_prior_used"
    else:
        status, reason = "learning", "recent_profile_warming_up"
    if dhw_timing_unknown:
        status, reason = "learning", "dhw_timing_unknown"
    return _Window(
        start, end, sum(value["kwh"] for value in used.values()), status, reason,
        totals, extra, recent,
    )


def _missing(reason: str) -> dict[str, Any]:
    return {"status": "data_missing", "reason": reason}


def _score_block(
    kind: str,
    active_score: float | None,
    median_kwh: float | None,
    p10_kwh: float | None,
    alpha: float,
    window: _Window | None,
    *,
    full_day_profile_assumption: bool = False,
) -> dict[str, Any]:
    if median_kwh is None or median_kwh < 0:
        return _missing("pv_forecast_missing")
    if window is None:
        return _missing("profile_window_unavailable")
    effective = _effective(median_kwh, p10_kwh, alpha)
    minimum = (window.end - window.start).total_seconds() / 3600 / 1000
    denominator = max(window.energy_kwh, minimum)
    score = round(10 * min(1.0, max(0.0, effective / denominator)))
    candidate = score if window.status == "ready" else None
    return {
        "status": window.status,
        "reason": window.reason,
        "comparison_kind": kind,
        "active_score": active_score,
        "candidate_score": candidate,
        "score_delta": candidate - active_score if candidate is not None and active_score is not None else None,
        "median_pv_kwh": median_kwh,
        "p10_pv_kwh": p10_kwh if p10_kwh is not None and p10_kwh > 0 else median_kwh,
        "effective_pv_kwh": effective,
        "profile_denominator_kwh": denominator,
        "full_day_profile_assumption": full_day_profile_assumption,
        **window.report(),
    }


def _overall(blocks: dict[str, dict[str, Any]]) -> str:
    statuses = {value.get("status") for value in blocks.values()}
    for status in ("error", "data_missing", "learning", "ready"):
        if status in statuses:
            return status
    return "not_applicable"


def remaining_day_profile(
    model: DemandForecast,
    issued_at: datetime,
    data: dict[str, Any],
    settings: dict[str, Any],
    options: dict[str, Any],
    ha_states: Any,
    timezone: Any,
    fingerprint: str,
) -> tuple[dict[str, Any], _Window | None]:
    """Return the same qualified online-profile window used by the active score."""
    cfg = options.get("demand_forecast", {})
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return {"status": "disabled", "reason": "demand_forecast_disabled"}, None
    report = data.get("demand_forecast", {})
    if not isinstance(report, dict) or report.get("status") not in (
        "ready", "learning", "no_pv_timing"
    ):
        return _missing("demand_observation_unavailable"), None
    issued_at = instant(issued_at)
    states = data.get("states", {})
    attributes = data.get("attributes", {})
    today = issued_at.astimezone(timezone).date()
    next_setting = _timestamp(attributes, "sun.sun", "next_setting")
    if (
        states.get("sun.sun") != "above_horizon"
        or next_setting is None
        or next_setting.astimezone(timezone).date() != today
    ):
        return {"status": "not_applicable", "reason": "outside_daylight"}, None
    window = _window(
        model,
        issued_at,
        next_setting,
        issued_at,
        report,
        settings,
        options,
        ha_states,
        timezone,
        fingerprint,
        _dhw_timing_unknown(report, options, ha_states, issued_at),
    )
    if window is None:
        return _missing("profile_window_unavailable"), None
    return {"status": window.status, "reason": window.reason, **window.report()}, window


def build_strategy_comparison(
    model: DemandForecast,
    issued_at: datetime,
    data: dict[str, Any],
    settings: dict[str, Any],
    options: dict[str, Any],
    ha_states: Any,
    timezone: Any,
    fingerprint: str,
    previous_level: float | None,
    *,
    remaining_day: tuple[dict[str, Any], _Window | None] | None = None,
) -> dict[str, Any]:
    """Compare profile candidates without mutating controller or demand state."""
    disabled = {
        key: {"status": "disabled", "reason": "demand_forecast_disabled"}
        for key in ("remaining_day", "tomorrow", "sunny_day", "target_soc")
    }
    out: dict[str, Any] = {
        "status": "disabled", "observation_only": True, "blocks": disabled
    }
    cfg = options.get("demand_forecast", {})
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return out
    if data.get("strategy_enabled") is False:
        inactive = {
            key: {"status": "not_applicable", "reason": "strategy_disabled"}
            for key in ("remaining_day", "tomorrow", "sunny_day", "target_soc")
        }
        return {**out, "status": "not_applicable", "blocks": inactive}
    report = data.get("demand_forecast", {})
    if not isinstance(report, dict) or report.get("status") not in (
        "ready", "learning", "no_pv_timing"
    ):
        reason = "demand_observation_unavailable"
        missing_blocks = {
            key: _missing(reason)
            for key in ("remaining_day", "tomorrow", "sunny_day", "target_soc")
        }
        return {**out, "status": "data_missing", "blocks": missing_blocks}
    issued_at = instant(issued_at)
    dhw_timing_unknown = _dhw_timing_unknown(report, options, ha_states, issued_at)
    states = data.get("states", {})
    attributes = data.get("attributes", {})
    alpha_raw = _value(states, "input_number.opti_forecast_optimismus")
    alpha = min(100.0, max(0.0, alpha_raw or 0.0)) / 100
    today = issued_at.astimezone(timezone).date()
    tomorrow = today + timedelta(days=1)
    next_setting = _timestamp(attributes, "sun.sun", "next_setting")
    next_rising = _timestamp(attributes, "sun.sun", "next_rising")

    blocks: dict[str, dict[str, Any]] = {}
    remaining_report, window = remaining_day if remaining_day is not None else remaining_day_profile(
        model, issued_at, data, settings, options, ha_states, timezone, fingerprint
    )
    if window is None:
        blocks["remaining_day"] = (
            _missing("remaining_day_inputs")
            if remaining_report.get("reason") == "profile_window_unavailable"
            else remaining_report
        )
    else:
        remaining = _value(states, "sensor.opti_forecast_remaining_today_kwh")
        effective = _value(states, "sensor.opti_forecast_effective_remaining_kwh")
        capacity = _value(states, "sensor.opti_battery_capacity_kwh")
        soc = _value(states, "sensor.opti_soc")
        active = _value(states, "sensor.opti_forecast_score")
        legacy_w = _value(states, "sensor.opti_house_consumption_60min_w")
        if legacy_w is None:
            legacy_w = _value(states, "sensor.opti_house_consumption_w")
        if legacy_w is None:
            legacy_w = 400.0
        if (remaining is None or effective is None or capacity is None
                or capacity < 0 or soc is None or not 0 <= soc <= 100):
            blocks["remaining_day"] = _missing("remaining_day_inputs")
        else:
            needed = capacity * (1 - soc / 100)
            surplus = max(effective - window.energy_kwh, 0.0)
            score = 10 if needed <= 0 else round(min(surplus / needed, 1) * 10)
            candidate: int | None = score if window.status == "ready" else None
            blocks["remaining_day"] = {
                "status": window.status,
                "reason": window.reason,
                "active_remaining_pv_kwh": remaining,
                "effective_remaining_pv_kwh": effective,
                "needed_full_kwh": needed,
                "legacy_extrapolated_load_kwh": legacy_w * (window.end - window.start).total_seconds() / 3600000,
                "profile_surplus_kwh": surplus,
                "active_score": active,
                "candidate_score": candidate,
                "score_delta": candidate - active if candidate is not None and active is not None else None,
                **remaining_report,
            }

    tomorrow_start, tomorrow_end = _day_bounds(tomorrow, timezone)
    tomorrow_window = _window(
        model, tomorrow_start, tomorrow_end, issued_at, report, settings,
        options, ha_states, timezone, fingerprint, dhw_timing_unknown,
    )
    blocks["tomorrow"] = _score_block(
        "full_local_calendar_day",
        _value(states, "sensor.opti_forecast_score_tomorrow"),
        _value(states, "sensor.opti_forecast_tomorrow_kwh"),
        _attribute(attributes, "sensor.opti_forecast_tomorrow_kwh", "estimate10"),
        alpha, tomorrow_window,
    )

    if next_rising is None:
        blocks["sunny_day"] = _missing("next_rising_missing")
    else:
        sunny_date = today if next_rising.astimezone(timezone).date() == today else tomorrow
        if sunny_date == tomorrow:
            sunny_window = tomorrow_window
        else:
            sunny_start, sunny_end = _day_bounds(sunny_date, timezone)
            sunny_window = _window(
                model, sunny_start, sunny_end, issued_at, report, settings,
                options, ha_states, timezone, fingerprint, dhw_timing_unknown,
            )
        prefix = "today" if sunny_date == today else "tomorrow"
        blocks["sunny_day"] = _score_block(
            "full_local_calendar_day",
            _value(states, "sensor.opti_forecast_score_sonnentag"),
            _value(states, f"sensor.opti_forecast_{prefix}_kwh"),
            _attribute(attributes, f"sensor.opti_forecast_{prefix}_kwh", "estimate10"),
            alpha, sunny_window, full_day_profile_assumption=sunny_date == today,
        )

    effective = _value(states, "sensor.opti_forecast_effective_remaining_kwh")
    capacity = _value(states, "sensor.opti_battery_capacity_kwh")
    low = number(settings.get("input_number.minsoc"))
    high = number(settings.get("input_number.maxsoc"))
    active_target = _value(states, "sensor.opti_target_soc")
    active_ratio = _attribute(attributes, "sensor.opti_target_soc", "ratio")
    override = settings.get("input_boolean.hausakku_aus_netz_laden") is True
    if override and low is not None and high is not None and 0 <= low <= high <= 100:
        previous = (int(previous_level) if previous_level is not None
                    and previous_level.is_integer() and 0 <= previous_level <= 5 else 0)
        target = round(high)
        blocks["target_soc"] = {
            "status": "ready",
            "reason": "grid_charge_override",
            "comparison_kind": "single_step_same_previous_level",
            "grid_charge_override": True,
            "previous_level": previous,
            "plain_level": 0,
            "candidate_level": 0,
            "active_target_soc": active_target,
            "candidate_target_soc": target,
            "target_delta": target - active_target if active_target is not None else None,
            "active_ratio": active_ratio,
            "candidate_ratio": None,
        }
    elif next_setting is None:
        blocks["target_soc"] = _missing("next_setting_missing")
    else:
        horizon_hours = min(12.0, max(0.5, (next_setting - issued_at).total_seconds() / 3600))
        target_end = issued_at + timedelta(hours=horizon_hours)
        target_window = _window(
            model, issued_at, target_end, issued_at, report, settings,
            options, ha_states, timezone, fingerprint, dhw_timing_unknown,
        )
        if (target_window is None or effective is None or capacity is None or capacity <= 0
                or low is None or high is None or not 0 <= low <= high <= 100):
            blocks["target_soc"] = _missing("target_inputs")
        else:
            ratio = max(effective - target_window.energy_kwh, 0.0) / capacity
            plain = sum(bound <= ratio for bound in _BOUNDS)
            previous = int(previous_level) if (previous_level is not None
                                                and previous_level.is_integer()
                                                and 0 <= previous_level <= 5) else plain
            level = previous
            for _ in _BOUNDS:
                if level < 5 and ratio >= _BOUNDS[level] + _MARGIN:
                    level += 1
            for _ in _BOUNDS:
                if level > 0 and ratio < _BOUNDS[level - 1] - _MARGIN:
                    level -= 1
            targets = (high, *TARGET_SOC_LEVELS)
            target = round(max(low, min(high, targets[level])))
            candidate_target: int | None = (
                target if target_window.status == "ready" else None
            )
            blocks["target_soc"] = {
                "status": target_window.status,
                "reason": target_window.reason,
                "comparison_kind": "single_step_same_previous_level",
                "grid_charge_override": override,
                "previous_level": previous,
                "plain_level": plain,
                "candidate_level": level if candidate_target is not None else None,
                "active_target_soc": active_target,
                "candidate_target_soc": candidate_target,
                "target_delta": (candidate_target - active_target
                                 if candidate_target is not None and active_target is not None
                                 else None),
                "active_ratio": active_ratio,
                "candidate_ratio": ratio,
                **target_window.report(),
            }
    out.update(status=_overall(blocks), blocks=blocks)
    return out
