"""Privacy-bounded diagnostics for Opti Akku support requests."""

from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from datetime import datetime
from math import isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .charge_curve import format_curve


def _status(value: Any) -> str | None:
    """Return a public status value without exporting its detailed payload."""
    if isinstance(value, Mapping):
        value = value.get("status")
    return value if isinstance(value, str) else None


def _source_error_summary(errors: Any) -> dict[str, Any]:
    """Aggregate source failures without exposing configured entity IDs."""
    if not isinstance(errors, Mapping):
        return {"count": 0, "roles": [], "codes": {}}

    roles: set[str] = set()
    codes: Counter[str] = Counter()
    for key, value in errors.items():
        role = key if isinstance(key, str) else "other"
        if role.startswith("plant:"):
            role = "plant"
        elif role not in {
            "cell_spread",
            "cell_voltage_min",
            "forecast_remaining",
            "forecast_today",
            "forecast_tomorrow",
            "house_consumption",
            "price_current",
            "price_series",
            "pv_generation",
            "pv_power",
        }:
            role = "other"
        roles.add(role)
        code = value if isinstance(value, str) and value else "unknown"
        codes[code] += 1
    return {"count": len(errors), "roles": sorted(roles), "codes": dict(sorted(codes.items()))}


def _device_summary(identity: Any) -> dict[str, Any]:
    """Keep compatibility facts while excluding serial numbers and identifiers."""
    if not isinstance(identity, Mapping):
        return {}
    allowed = ("device_class", "inverter_status", "max_power_w", "model", "model_id", "profile", "status_register")
    summary = {key: identity[key] for key in allowed if isinstance(identity.get(key), str | int | float | bool)}
    if isinstance(identity.get("register_probe"), list):
        summary["register_probe"] = _register_probe_summary(identity["register_probe"])
    return summary


def _register_probe_summary(rows: list[Any]) -> list[dict[str, Any]]:
    """Raw device-test registers (#170); the serial-number register is never included."""
    summary = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("address"), int) or row["address"] == 30057:
            continue
        item: dict[str, Any] = {"address": row["address"]}
        for key, kind in (("raw", int), ("error", str), ("evidence", str)):
            if isinstance(row.get(key), kind) and not isinstance(row.get(key), bool):
                item[key] = row[key]
        summary.append(item)
    return summary


def _command_evidence_summary(value: Any) -> dict[str, Any]:
    """Keep evidence semantics while excluding measurements and timestamps."""
    if not isinstance(value, Mapping):
        return {}
    allowed = (
        "block_observation",
        "execution_basis",
        "physical_effect",
        "safe_phase_completed",
        "setpoint_readback",
        "setpoint_readback_limitation",
        "status",
    )
    return {
        key: value[key]
        for key in allowed
        if isinstance(value.get(key), str | bool)
    }


def _strategy_comparison_summary(value: Any) -> dict[str, Any]:
    """Expose only bounded comparison status and reason codes."""
    if not isinstance(value, Mapping):
        return {"observation_only": True, "status": None, "blocks": {}}
    blocks = value.get("blocks")
    summary = {}
    if isinstance(blocks, Mapping):
        for key in ("remaining_day", "tomorrow", "sunny_day", "target_soc"):
            block = blocks.get(key)
            if not isinstance(block, Mapping):
                continue
            summary[key] = {
                name: block[name]
                for name in ("status", "reason")
                if isinstance(block.get(name), str)
            }
    return {
        "observation_only": value.get("observation_only") is True,
        "status": value.get("status") if isinstance(value.get("status"), str) else None,
        "blocks": summary,
    }


_AMBITIOUS_KEYS = (
    "status", "action", "target_soc", "charge_price_ct", "expected_grid_kwh", "expected_export_kwh",
    "expected_export_kwh_without", "expected_saving_eur", "first_at", "window_end", "quantile",
    "min_gain_ct", "controls_battery", "detail",
)


_OPERATING_COUNTERS = (
    "disconnects", "source_error_episodes", "write_failures", "sampling_gaps", "restarts",
    "clock_changes",
)
_INCIDENT_KINDS = frozenset({
    "clock_moved_backwards", "coverage_gap", "offline", "restart", "source_error", "write_failed",
})
_PEAK_TIMES = ("started_at", "ended_at")
_PEAK_SOCS = ("start_soc", "last_soc", "min_soc", "max_soc")
_PEAK_END_REASONS = frozenset({"strategy_disabled", "price_period_ended"})


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _soc(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 1) if isfinite(value) and 0 <= value <= 100 else None


def _peak_summary(peak: Any) -> dict[str, Any] | None:
    """Start, end and charge levels of a peak period; no energies or plan copy."""
    if not isinstance(peak, Mapping):
        return None
    summary: dict[str, Any] = {key: _timestamp(peak.get(key)) for key in _PEAK_TIMES}
    summary.update({key: _soc(peak.get(key)) for key in _PEAK_SOCS})
    reason = peak.get("end_reason")
    summary["end_reason"] = (
        reason if isinstance(reason, str) and reason in _PEAK_END_REASONS else None
    )
    summary["control_enabled_at_start"] = peak.get("control_enabled_at_start") is True
    return summary


def _operating_report_summary(report: Any) -> dict[str, Any] | None:
    """Counters, incidents and peak periods of the operating report (allowlist)."""
    if not isinstance(report, Mapping):
        return None
    summary: dict[str, Any] = {
        "status": report.get("status") if isinstance(report.get("status"), str) else None,
        "started_at": _timestamp(report.get("started_at")),
        "last_observation": _timestamp(report.get("last_observation")),
    }
    coverage = report.get("coverage_percent")
    summary["coverage_percent"] = (
        round(float(coverage), 1)
        if isinstance(coverage, (int, float)) and not isinstance(coverage, bool)
        and isfinite(coverage) and 0 <= coverage <= 100 else None
    )
    for key in _OPERATING_COUNTERS:
        value = report.get(key)
        summary[key] = value if type(value) is int and value >= 0 else None
    incidents = report.get("incidents")
    summary["incidents"] = [
        {"at": at, "kind": item["kind"]}
        for item in (incidents[-20:] if isinstance(incidents, list) else [])
        if isinstance(item, Mapping) and isinstance(item.get("kind"), str)
        and item["kind"] in _INCIDENT_KINDS
        and (at := _timestamp(item.get("at"))) is not None
    ]
    summary["current_peak"] = _peak_summary(report.get("current_peak"))
    summary["last_peak"] = _peak_summary(report.get("last_peak"))
    return summary


def _safe_operating_report(report: Any) -> dict[str, Any] | None:
    """A malformed stored report must not break the diagnostics download."""
    try:
        return _operating_report_summary(report)
    except Exception:  # noqa: BLE001 - diagnostics must still download
        return {"status": "error"}


def _decision_history(coordinator: Any) -> list[dict[str, Any]]:
    """Decision segments of the last 24 hours, oldest first."""
    log = getattr(coordinator, "decision_log", None)
    if log is None:
        return []
    try:
        return log.report(dt_util.utcnow())
    except Exception:  # noqa: BLE001 - diagnostics must still download
        return []


def _ambitious_precharge_summary(report: Any) -> dict[str, Any] | None:
    """Plan figures of the ambitious precharge (#222), without the slot list."""
    if not isinstance(report, Mapping):
        return None
    return {key: report[key] for key in _AMBITIOUS_KEYS if key in report}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return support data without hosts, serials or foreign entity IDs.

    History is limited to the integration's own decisions of the last 24 hours.
    """
    options = entry.options if isinstance(entry.options, Mapping) else {}
    coordinator = getattr(entry, "runtime_data", None)
    data = (
        getattr(coordinator, "data", None)
        if isinstance(getattr(coordinator, "data", None), Mapping)
        else {}
    )
    shadow_mode = bool(
        getattr(
            coordinator,
            "shadow_mode",
            entry.data.get("shadow_mode", entry.data.get("backend") == "huawei_solar"),
        )
    )
    strategy_enabled = bool(
        getattr(coordinator, "strategy_enabled", options.get("strategy_enabled", True))
    )
    sources = options.get("sources", {})
    windows = options.get("price_windows")

    result: dict[str, Any] = {
        "configuration": {
            "backend": entry.data.get("backend", "sma"),
            "shadow_mode": shadow_mode,
            "strategy_enabled": strategy_enabled,
            "write_enabled": bool(data.get("write_enabled", False)),
            "plant_mode": options.get("plant_mode", "legacy"),
            "price_provider": options.get("price_provider", "entities"),
            "configured_source_roles": sorted(sources) if isinstance(sources, Mapping) else [],
            "ev_preparation_enabled": options.get("ev_preparation", {}).get("enabled") is True
            if isinstance(options.get("ev_preparation"), Mapping)
            else False,
        },
        "connection": {
            "online": bool(data.get("online", False)),
            "status": _status(data.get("connection_status")),
            "command_result": data.get("command_result_this_update"),
            "command_confirmation": data.get("command_confirmation"),
            "command_evidence": _command_evidence_summary(data.get("command_evidence")),
            "pause_pending": bool(data.get("pause_pending", False)),
            "control_inactive": data.get("control_inactive") is True,
            "write_restore_blocked": data.get("write_restore_blocked"),
            "control_release": data.get("control_release"),
        },
        "device": _device_summary(data.get("identity")),
        "health": {
            "has_last_error": bool(data.get("last_error")),
            "standby": data.get("connection_status", {}).get("standby")
            if isinstance(data.get("connection_status"), Mapping)
            else None,
            "source_errors": _source_error_summary(data.get("source_errors")),
            "device_error_count": len(data.get("device_errors", {}))
            if isinstance(data.get("device_errors"), Mapping)
            else 0,
            "notification_error": bool(data.get("notification_error")),
            "internal_error": data["internal_error"].get("error_type")
            if isinstance(data.get("internal_error"), Mapping)
            else None,
        },
        "features": {
            key: _status(data.get(key))
            for key in (
                "demand_forecast",
                "ev_preparation",
                "operating_report",
                "reserve_plan",
                "source_observation",
                "standby_reserve",
                "ambitious_precharge",
            )
        },
        "ambitious_precharge": _ambitious_precharge_summary(data.get("ambitious_precharge")),
        "operating_report": _safe_operating_report(data.get("operating_report")),
        "decision_history": _decision_history(coordinator),
        "strategy_comparison": _strategy_comparison_summary(
            data.get("demand_forecast", {}).get("strategy_comparison")
            if isinstance(data.get("demand_forecast"), Mapping)
            else None
        ),
        "shadow": {
            "status": data.get("shadow_status") if shadow_mode else "not_active",
            "blocked_write_attempts": data.get("shadow_summary", {}).get(
                "blocked_write_attempts", 0
            )
            if shadow_mode and isinstance(data.get("shadow_summary"), Mapping)
            else 0,
        },
        "comparison": {
            "status": "not_applicable" if shadow_mode else data.get("shadow_status", "idle"),
            "reference_static": data.get("shadow_summary", {}).get("reference_static") is True
            if not shadow_mode and isinstance(data.get("shadow_summary"), Mapping)
            else False,
        },
    }
    if options.get("price_provider") == "fixed_windows":
        # Window count only; the tariff itself is not needed for support.
        result["configuration"]["price_window_count"] = (
            len(windows) if isinstance(windows, list) else 0
        )
    if "charge_curve" in options:
        # Battery limits only, no device or location data.
        result["configuration"]["charge_curve"] = format_curve(options.get("charge_curve"))
    return result
