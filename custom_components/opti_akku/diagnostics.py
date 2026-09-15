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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return support data without hosts, serials, entity IDs or history."""
    coordinator = entry.runtime_data
    data = coordinator.data if isinstance(coordinator.data, Mapping) else {}
    options = entry.options if isinstance(entry.options, Mapping) else {}
    sources = options.get("sources", {})

    return {
        "configuration": {
            "backend": entry.data.get("backend", "sma"),
            "shadow_mode": bool(coordinator.shadow_mode),
            "strategy_enabled": bool(coordinator.strategy_enabled),
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
            "pause_pending": bool(data.get("pause_pending", False)),
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
        "shadow": {
            "status": data.get("shadow_status") if coordinator.shadow_mode else "not_active",
            "blocked_write_attempts": data.get("shadow_summary", {}).get(
                "blocked_write_attempts", 0
            )
            if coordinator.shadow_mode and isinstance(data.get("shadow_summary"), Mapping)
            else 0,
        },
    }
