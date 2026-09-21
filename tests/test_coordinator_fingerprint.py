"""HA persistence across SMA transport changes, using synthetic history only."""

from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.coordinator import (
    _canonical_load_fingerprint,
    _load_device_identity,
    _rebind_load_prefix,
)
from tests.test_coordinator import device


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("change, retained", [
    ("host", True), ("port", True), ("unit_id", True), ("backend", True),
    ("serial_number", False), ("profile", False), ("source", False),
    ("missing_serial", False),
])
async def test_sma_reconfigure_preserves_only_same_device_and_sources(hass, change, retained, legacy):
    data = {"backend": "sma_modbus", "host": "192.0.2.10", "port": 502,
            "unit_id": 3, "profile": "sma_stp_se", "serial_number": "123456789"}
    if change == "missing_serial":
        data.pop("serial_number")
    options = {"single_writer_confirmed": True, "single_inverter": True,
               "sources": {}, "demand_forecast": {"enabled": True, "sources": {}}}
    entry = MockConfigEntry(domain="opti_akku", title="Synthetic persistence",
                            data=data, options=options)
    entry.add_to_hass(hass)
    inverter = device()
    with patch("custom_components.opti_akku.async_get_unit"), patch(
        "custom_components.opti_akku.SmaDevice", return_value=inverter
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        before = entry.runtime_data
        entities_before = {s.entity_id for s in hass.states.async_all()}
        now = dt_util.utcnow()
        previous_hour = (now - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        cells = {f"{previous_hour.date()}|{previous_hour.hour}|unknown": [1800000, 0, 0, 3600]}
        rows = {previous_hour.isoformat(): {"house_w": 500}}
        before._demand_forecast.cells = deepcopy(cells)
        before._demand_forecast.history.replace(rows, before._demand_forecast.history_binding(
            before._load_source_fingerprint, options, dt_util.DEFAULT_TIME_ZONE), now)
        before.settings["input_number.maxsoc"] = 91
        before.write_enabled = True
        saved = deepcopy(before._stored_data())
        if legacy:
            old = json.loads(saved["load_source_fingerprint"])
            old["device"] = data
            old = json.dumps(old, sort_keys=True)
            saved["load_source_fingerprint"] = old
            for container, key in ((saved["demand_forecast"], "fingerprint"),
                                   (saved["demand_forecast"]["history"], "binding")):
                parts = json.loads(container[key])
                container[key] = json.dumps([old, *parts[1:]], sort_keys=True)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await before._store.async_save(saved)

        updated_data, updated_options = dict(data), deepcopy(options)
        if change == "source":
            updated_options["sources"]["house_consumption"] = "sensor.other_house"
        else:
            key = "host" if change == "missing_serial" else change
            updated_data[key] = {"host": "192.0.2.11", "port": 1502, "unit_id": 4,
                                 "backend": "sma", "serial_number": "987654321",
                                 "profile": "different_profile"}[key]
        inverter.async_probe.return_value["serial_number"] = updated_data.get("serial_number", "123456789")
        hass.config_entries.async_update_entry(entry, data=updated_data, options=updated_options)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            after = entry.runtime_data
            assert after.settings["input_number.maxsoc"] == 91
            assert {s.entity_id for s in hass.states.async_all()} == entities_before
            assert after._demand_forecast.cells == (cells if retained else {})
            assert after._demand_forecast.history.rows == (rows if retained else {})
            assert after._operating_report.snapshot()["restarts"] == (1 if retained else 0)
            # History preservation never relaxes the independently stored writer binding.
            if change in ("host", "port", "unit_id", "serial_number", "missing_serial"):
                assert after.write_enabled is False
            if retained:
                assert after._demand_forecast.history.binding == after._demand_forecast.history_binding(
                    after._load_source_fingerprint, updated_options, dt_util.DEFAULT_TIME_ZONE)
                assert json.loads(after._demand_forecast.fingerprint)[0] == after._load_source_fingerprint
        finally:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


@pytest.mark.parametrize("serial", [None, "", " ", 123, True])
def test_unproven_sma_identity_keeps_transport_binding(serial):
    connection = {"host": "192.0.2.10", "serial_number": serial}
    assert _load_device_identity(connection) == connection


def test_huawei_fingerprint_keeps_all_bindings():
    connection = {"backend": "huawei_solar", "serial_number": "123456789",
                  "host": "provider", "huawei_device_id": "synthetic-device"}
    assert _load_device_identity(connection) == connection


@pytest.mark.parametrize("value", [None, "broken", "[]", '{"device": []}',
                                   '{"device": {}, "plant": NaN}'])
def test_malformed_load_fingerprint_is_not_migrated(value):
    assert _canonical_load_fingerprint(value) is None


@pytest.mark.parametrize("value", [None, "broken", '"old"', '["old"]', '["other", {}, "UTC"]'])
def test_migration_never_rebinds_unrelated_or_malformed_nested_history(value):
    assert _rebind_load_prefix(value, "old", "new", 3) == value
