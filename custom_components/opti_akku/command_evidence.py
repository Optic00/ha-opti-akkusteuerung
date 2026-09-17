"""Describe command evidence without inferring unobserved device effects."""

from __future__ import annotations

from datetime import datetime
from typing import Any


EXECUTION_BASES = frozenset({"modbus_write_sequence", "ha_service_and_entity_checks"})
READBACK_LEVELS = frozenset({"not_supported", "partial"})
STATUS_BY_RESULT = {
    "confirmed": "completed",
    "safe_phase_confirmed": "pending",
    "pending": "pending",
    "failed": "failed",
    "superseded": "superseded",
    "not_attempted": "not_attempted",
}


def _capability(device: Any, name: str, allowed: frozenset[str], fallback: str) -> str:
    value = getattr(device, name, None)
    return value if isinstance(value, str) and value in allowed else fallback


def build_command_evidence(
    device: Any,
    *,
    command_result: str,
    write_enabled: bool,
    shadow: bool,
    write_ready: bool,
    persistent_violation: bool,
    battery_power_w: float | None,
    battery_power_observed_at: datetime | None = None,
    execution_completed_this_update: bool = False,
) -> dict[str, Any]:
    """Return independent execution, readback and physical-observation claims."""
    basis = _capability(device, "command_execution_basis", EXECUTION_BASES, "unknown")
    readback_capability = _capability(
        device, "setpoint_readback_capability", READBACK_LEVELS, "not_supported"
    )
    limitation = getattr(device, "setpoint_readback_limitation", None)
    limitation = limitation if isinstance(limitation, str) else "unspecified"

    if shadow or not write_enabled:
        status = "observation"
    elif not write_ready:
        status = "waiting_ready"
    else:
        status = STATUS_BY_RESULT.get(command_result, "not_attempted")

    if status == "completed":
        setpoint_readback = readback_capability
    elif status == "pending" and command_result == "safe_phase_confirmed":
        setpoint_readback = "partial"
    elif status == "pending" and readback_capability != "not_supported":
        setpoint_readback = "pending"
    else:
        setpoint_readback = "not_assessed"

    last_write = getattr(device, "last_write", None)
    completed_at = last_write.isoformat() if isinstance(last_write, datetime) else None
    observed_at = (
        battery_power_observed_at.isoformat()
        if battery_power_w is not None
        and isinstance(battery_power_observed_at, datetime)
        else None
    )
    observed_after_execution = None
    if (
        battery_power_w is not None
        and isinstance(last_write, datetime)
        and isinstance(battery_power_observed_at, datetime)
    ):
        try:
            observed_after_execution = (
                False
                if execution_completed_this_update
                else battery_power_observed_at >= last_write
            )
        except TypeError:
            # Naive and timezone-aware timestamps are deliberately not made
            # comparable by guessing a timezone.
            observed_after_execution = None
    return {
        "status": status,
        "execution_basis": basis,
        "setpoint_readback": setpoint_readback,
        "setpoint_readback_limitation": limitation,
        "physical_effect": "not_verified",
        "block_observation": (
            "violation_observed" if persistent_violation else "not_assessed"
        ),
        "observed_battery_power_w": battery_power_w,
        "battery_power_observed_at": observed_at,
        "observed_after_execution": observed_after_execution,
        "execution_completed_at": completed_at,
        "safe_phase_completed": command_result == "safe_phase_confirmed",
    }
