"""Privacy-bounded diagnostics for Opti Akku support requests."""

from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


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
    allowed = ("device_class", "inverter_status", "max_power_w", "model", "model_id")
    return {key: identity[key] for key in allowed if isinstance(identity.get(key), str | int | float | bool)}


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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return support data without hosts, serials, entity IDs or history."""
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

    return {
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
            "source_errors": _source_error_summary(data.get("source_errors")),
            "device_error_count": len(data.get("device_errors", {}))
            if isinstance(data.get("device_errors"), Mapping)
            else 0,
            "notification_error": bool(data.get("notification_error")),
        },
        "features": {
            key: _status(data.get(key))
            for key in (
                "demand_forecast",
                "ev_preparation",
                "operating_report",
                "reserve_plan",
                "source_observation",
            )
        },
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
