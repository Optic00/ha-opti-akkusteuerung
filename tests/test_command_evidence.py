"""Command evidence never upgrades execution into unproven device effect."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.opti_akku.command_evidence import build_command_evidence


COMPLETED = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 15, 20, 0, 5, tzinfo=UTC)


def adapter(basis="modbus_write_sequence", readback="not_supported", limitation="none"):
    return SimpleNamespace(
        command_execution_basis=basis,
        setpoint_readback_capability=readback,
        setpoint_readback_limitation=limitation,
        last_write=COMPLETED,
    )


def evidence(device=None, **overrides):
    values = {
        "command_result": "confirmed",
        "write_enabled": True,
        "shadow": False,
        "write_ready": True,
        "persistent_violation": False,
        "battery_power_w": 0.0,
        "battery_power_observed_at": OBSERVED,
    }
    values.update(overrides)
    return build_command_evidence(device or adapter(), **values)


def test_sma_completion_is_not_setpoint_or_physical_confirmation():
    result = evidence()
    assert result == {
        "status": "completed",
        "execution_basis": "modbus_write_sequence",
        "setpoint_readback": "not_supported",
        "setpoint_readback_limitation": "none",
        "physical_effect": "not_verified",
        "block_observation": "not_assessed",
        "observed_battery_power_w": 0.0,
        "battery_power_observed_at": OBSERVED.isoformat(),
        "observed_after_execution": True,
        "execution_completed_at": COMPLETED.isoformat(),
        "safe_phase_completed": False,
    }


def test_huawei_completion_is_only_partial_readback():
    device = adapter(
        "ha_service_and_entity_checks",
        "partial",
        "tou_schedule_not_independently_read_back",
    )
    result = evidence(device)
    assert result["status"] == "completed"
    assert result["setpoint_readback"] == "partial"
    assert result["physical_effect"] == "not_verified"


@pytest.mark.parametrize(
    ("command_result", "status", "readback"),
    [
        ("pending", "pending", "pending"),
        ("safe_phase_confirmed", "pending", "partial"),
        ("failed", "failed", "not_assessed"),
        ("superseded", "superseded", "not_assessed"),
        ("not_attempted", "not_attempted", "not_assessed"),
    ],
)
def test_current_result_cannot_reuse_old_completion(command_result, status, readback):
    device = adapter("ha_service_and_entity_checks", "partial")
    result = evidence(device, command_result=command_result)
    assert result["status"] == status
    assert result["setpoint_readback"] == readback
    assert result["safe_phase_completed"] is (command_result == "safe_phase_confirmed")


@pytest.mark.parametrize(
    "override",
    [{"shadow": True}, {"write_enabled": False}],
)
def test_observation_never_claims_own_command_evidence(override):
    result = evidence(**override)
    assert result["status"] == "observation"
    assert result["setpoint_readback"] == "not_assessed"
    assert result["physical_effect"] == "not_verified"


def test_waiting_and_violation_are_independent_axes():
    result = evidence(write_ready=False, persistent_violation=True, battery_power_w=-500)
    assert result["status"] == "waiting_ready"
    assert result["block_observation"] == "violation_observed"
    assert result["physical_effect"] == "not_verified"


def test_pre_command_measurement_is_not_presented_as_effect_evidence():
    observed = datetime(2026, 9, 15, 19, 59, 59, tzinfo=UTC)
    result = evidence(battery_power_observed_at=observed)
    assert result["battery_power_observed_at"] == observed.isoformat()
    assert result["observed_after_execution"] is False
    assert result["physical_effect"] == "not_verified"


def test_same_update_execution_order_wins_over_wall_clock_rollback():
    result = evidence(execution_completed_this_update=True)
    assert result["observed_after_execution"] is False


def test_missing_or_incomparable_observation_time_stays_unknown():
    assert evidence(battery_power_observed_at=None)["observed_after_execution"] is None
    naive = datetime(2026, 9, 15, 20, 0, 5)
    assert evidence(battery_power_observed_at=naive)["observed_after_execution"] is None


def test_timestamp_is_suppressed_without_a_measurement():
    result = evidence(battery_power_w=None)
    assert result["battery_power_observed_at"] is None
    assert result["observed_after_execution"] is None
