"""Tests for privacy-bounded config-entry diagnostics."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.diagnostics import async_get_config_entry_diagnostics


def config_entry(hass):
    """Create a private-looking entry so the test can detect accidental leaks."""
    entry = MockConfigEntry(
        domain="opti_akku",
        title="Private inverter title",
        data={
            "host": "192.0.2.44",
            "port": 502,
            "unit_id": 3,
            "serial_number": "3012345678",
            "huawei_entry_id": "private-entry-id",
        },
        options={
            "sources": {
                "house_consumption": "sensor.private_house_meter",
                "forecast_today": "sensor.pv",
            },
            "ev_preparation": {"enabled": True, "soc_entity": "sensor.private_car_soc"},
            "price_provider": "tibber",
            "tibber_entry_id": "private-tibber-entry",
        },
    )
    return entry


async def test_diagnostics_reports_health_without_private_bindings(hass):
    entry = config_entry(hass)
    private_entity = "sensor.private_house_meter"
    entry.runtime_data = SimpleNamespace(
        shadow_mode=True,
        strategy_enabled=True,
        data={
            "online": True,
            "write_enabled": False,
            "connection_status": {"status": "ready", "write_ready": True},
            "command_result_this_update": "not_attempted",
            "command_confirmation": "idle_or_confirmed",
            "control_release": "not_supported",
            "identity": {
                "model": "STP10.0-3SE-40",
                "model_id": 9340,
                "serial_number": "3012345678",
                "max_power_w": 10000,
            },
            "source_errors": {
                f"plant:{private_entity}": "missing_or_stale",
                "forecast_today": "invalid_value",
                private_entity: "invalid_value",
            },
            "device_errors": {"register_30845": "timeout"},
            "last_error": f"Failed source {private_entity}",
            "notification_error": "notify.mobile_app_private_phone",
            "reserve_plan": {"status": "planned", "required_soc": 47.2},
            "operating_report": {"status": "collecting", "totals": {"house": 99}},
            "demand_forecast": {"status": "ready", "history": [1, 2, 3]},
            "ev_preparation": {"status": "away", "vehicle": "private-car"},
            "source_observation": {"status": "warming_up", "entities": [private_entity]},
            "shadow_status": "recording",
            "shadow_summary": {
                "blocked_write_attempts": 2,
                "reference_entity": "select.private_legacy_mode",
            },
        },
    )
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["configuration"] == {
        "backend": "sma",
        "shadow_mode": True,
        "strategy_enabled": True,
        "write_enabled": False,
        "plant_mode": "legacy",
        "price_provider": "tibber",
        "configured_source_roles": ["forecast_today", "house_consumption"],
        "ev_preparation_enabled": True,
    }
    assert result["device"] == {
        "model": "STP10.0-3SE-40",
        "model_id": 9340,
        "max_power_w": 10000,
    }
    assert result["health"]["source_errors"] == {
        "count": 3,
        "roles": ["forecast_today", "other", "plant"],
        "codes": {"invalid_value": 2, "missing_or_stale": 1},
    }
    assert result["features"] == {
        "demand_forecast": "ready",
        "ev_preparation": "away",
        "operating_report": "collecting",
        "reserve_plan": "planned",
        "source_observation": "warming_up",
    }
    assert result["shadow"] == {"status": "recording", "blocked_write_attempts": 2}
    exported = json.dumps(result, allow_nan=False)
    for secret in (
        "192.0.2.44",
        "3012345678",
        "private-entry-id",
        "private-tibber-entry",
        private_entity,
        "sensor.private_car_soc",
        "select.private_legacy_mode",
        "notify.mobile_app_private_phone",
        "private-car",
    ):
        assert secret not in exported


async def test_diagnostics_handles_first_refresh_without_data(hass):
    entry = config_entry(hass)
    entry.runtime_data = SimpleNamespace(shadow_mode=False, strategy_enabled=False, data=None)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["connection"] == {
        "online": False,
        "status": None,
        "command_result": None,
        "command_confirmation": None,
        "pause_pending": False,
        "control_release": None,
    }
    assert result["health"]["source_errors"] == {"count": 0, "roles": [], "codes": {}}
    assert result["shadow"] == {"status": "not_active", "blocked_write_attempts": 0}
