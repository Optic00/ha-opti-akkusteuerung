"""Storage delays must not take the device lock or publish an unfinished import."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from threading import Event
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import flush_store

from .test_coordinator import coordinator as coordinator, entry as entry


@pytest.mark.parametrize("wizard", [False, True])
async def test_settings_save_keeps_reads_and_command_renewal_running(coordinator, wizard):
    coordinator.write_enabled = True
    await coordinator.async_refresh()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_save(data):
        entered.set()
        await release.wait()

    key = "input_number.minsoc"
    value = coordinator.settings[key] + 1
    with patch.object(coordinator._store, "async_save", new=blocked_save):
        caller = asyncio.create_task(
            coordinator.async_apply_settings({key: value}, "new-revision") if wizard
            else coordinator.async_set_setting(key, value))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not coordinator._update_lock.locked()
            reads = coordinator.device.async_read.await_count
            writes = coordinator.device.async_apply.await_count
            coordinator._last_apply = time.monotonic() - 121
            await asyncio.wait_for(coordinator.async_refresh(), 2)
            assert coordinator.device.async_read.await_count == reads + 1
            assert coordinator.device.async_apply.await_count == writes + 1
            assert coordinator.settings[key] == value
            assert not caller.done()  # The user action still waits for its save.
        finally:
            release.set()
            await asyncio.wait_for(caller, 5)


@pytest.mark.parametrize("pause_fails", [False, True])
async def test_huawei_pause_retry_does_not_wait_for_store(coordinator, pause_fails):
    coordinator._pause_pending = True
    coordinator._pause_binding = coordinator._writer_binding
    coordinator.device.async_shutdown_control = AsyncMock(
        side_effect=RuntimeError("synthetic device outage") if pause_fails else None)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_write(data):
        entered.set()
        await release.wait()

    with patch.object(coordinator._store, "_async_write_data", new=blocked_write), patch.object(
        coordinator._store, "async_delay_save"
    ) as delayed:
        writer = asyncio.create_task(coordinator._store.async_save(coordinator._stored_data()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(coordinator.async_refresh(), 2)
            await asyncio.wait_for(coordinator.async_refresh(), 2)
            coordinator.device.async_shutdown_control.assert_awaited_once()
            assert coordinator._pause_pending is pause_fails
            assert not coordinator.write_enabled
            assert coordinator.device.async_read.await_count == 2
            assert any(call.args[1] == 0 for call in delayed.call_args_list)
            coordinator.device.async_apply.assert_not_awaited()
        finally:
            release.set()
            await asyncio.wait_for(writer, 5)


async def test_huawei_shutdown_releases_device_lock_before_waiting_for_store(coordinator):
    class Phased:
        PHASED = True

    coordinator.device = Phased()
    coordinator.device.async_shutdown_control = AsyncMock()
    coordinator.write_enabled = True
    coordinator._online = True
    coordinator._identity = {"serial_number": "synthetic-test-device"}
    coordinator._pause_pending = True
    coordinator._pause_binding = coordinator._writer_binding
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_save(data):
        entered.set()
        await release.wait()

    with patch.object(coordinator._store, "async_save", new=blocked_save), patch.object(
        coordinator._store, "async_delay_save"
    ):
        stop = asyncio.create_task(coordinator.async_stop())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            coordinator.device.async_shutdown_control.assert_awaited_once()
            assert not coordinator._update_lock.locked()
            assert not stop.done()
        finally:
            release.set()
            await asyncio.wait_for(stop, 5)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "options_changed", "stopping"])
async def test_import_save_is_staged_without_blocking_control(coordinator, hass, entry, outcome):
    hass.config_entries.async_update_entry(entry, options={**entry.options,
        "demand_forecast": {"enabled": True, "history_house": "sensor.history"}})
    coordinator.write_enabled = True
    await coordinator.async_refresh()
    previous = coordinator._demand_forecast.history
    at = (dt_util.utcnow() - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    recorder = Mock()
    recorder.async_add_executor_job = AsyncMock(return_value={at.isoformat(): {"house_w": 500}})
    entered, release = asyncio.Event(), asyncio.Event()
    written = []

    async def blocked_save(data):
        written.append(data)
        entered.set()
        await release.wait()
        if outcome == "error":
            raise OSError("synthetic disk failure")

    with patch("homeassistant.components.recorder.get_instance", return_value=recorder), patch.object(
        coordinator._store, "async_save", new=blocked_save
    ), patch.object(coordinator._store, "async_delay_save") as delayed:
        caller = asyncio.create_task(coordinator.async_import_demand_history())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not coordinator._update_lock.locked()
            assert coordinator._demand_forecast.history is previous
            assert not previous.rows
            assert written[0]["demand_forecast"]["history"]["rows"]
            assert coordinator._stored_data()["demand_forecast"]["history"]["rows"]
            reads = coordinator.device.async_read.await_count
            writes = coordinator.device.async_apply.await_count
            coordinator._last_apply = time.monotonic() - 121
            await asyncio.wait_for(coordinator.async_refresh(), 2)
            assert coordinator.device.async_read.await_count == reads + 1
            assert coordinator.device.async_apply.await_count == writes + 1
            assert coordinator._demand_forecast.history is previous
            if outcome == "options_changed":
                hass.config_entries.async_update_entry(entry, options={**entry.options,
                    "demand_forecast": {"enabled": False}})
            if outcome == "stopping":
                coordinator._stopping = True
            if outcome in ("options_changed", "stopping"):
                assert not coordinator._stored_data()["demand_forecast"]["history"]["rows"]
            if outcome == "cancel":
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                assert coordinator._history_import_running
                assert coordinator._demand_forecast.history is previous
                with pytest.raises(HomeAssistantError):
                    await coordinator.async_import_demand_history()
            release.set()
            if outcome == "success":
                await asyncio.wait_for(caller, 5)
                assert coordinator._demand_forecast.history.rows
            elif outcome == "cancel":
                await asyncio.wait_for(coordinator._async_journal_idle(), 5)
                assert coordinator._demand_forecast.history.rows
            else:
                with pytest.raises(HomeAssistantError):
                    await asyncio.wait_for(caller, 5)
                assert coordinator._demand_forecast.history is previous
            assert coordinator._pending_history is None
            assert not coordinator._history_import_running
            # The final delayed snapshot contains the committed or repaired state.
            assert delayed.call_args.args[0]()["demand_forecast"]["history"] == (
                coordinator._demand_forecast.history.snapshot())
        finally:
            release.set()
            coordinator._stopping = False
            if not caller.done():
                await asyncio.wait_for(caller, 5)


@pytest.mark.parametrize("cancel", [False, True])
async def test_import_survives_real_store_coalescing(coordinator, hass, entry, hass_storage, cancel):
    """A periodic snapshot replacing Store._data preserves an accepted import."""
    hass.config_entries.async_update_entry(entry, options={**entry.options,
        "demand_forecast": {"enabled": True, "history_house": "sensor.history"}})
    await coordinator.async_refresh()
    at = (dt_util.utcnow() - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    recorder = Mock()
    recorder.async_add_executor_job = AsyncMock(return_value={at.isoformat(): {"house_w": 500}})
    entered, release = asyncio.Event(), asyncio.Event()
    write_data = coordinator._store._async_write_data

    async def blocked_first_write(data):
        if not entered.is_set():
            entered.set()
            await release.wait()
        await write_data(data)

    with patch("homeassistant.components.recorder.get_instance", return_value=recorder), patch.object(
        coordinator._store, "_async_write_data", new=blocked_first_write
    ):
        first = asyncio.create_task(coordinator._store.async_save(coordinator._stored_data()))
        caller = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            caller = asyncio.create_task(coordinator.async_import_demand_history())
            async with asyncio.timeout(2):
                while coordinator._pending_history is None:
                    await asyncio.sleep(0)
            # This replaces Store's pending data with the normal delayed callback.
            await asyncio.wait_for(coordinator.async_refresh(), 2)
            assert not coordinator._demand_forecast.history.rows
            if cancel:
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
            release.set()
            await asyncio.wait_for(first, 5)
            if not cancel:
                await asyncio.wait_for(caller, 5)
            await asyncio.wait_for(coordinator._async_journal_idle(), 5)
            await asyncio.wait_for(hass.async_block_till_done(), 5)
        finally:
            release.set()
            await asyncio.wait_for(first, 5)
            if caller is not None and not caller.done():
                await asyncio.wait_for(caller, 5)

    await flush_store(coordinator._store)
    # The HA fixture serializes real Store writes into this JSON-backed map.
    saved = hass_storage[f"opti_akku.{entry.entry_id}"]["data"]
    assert saved["demand_forecast"]["history"]["rows"]
    assert coordinator._demand_forecast.history.rows
    assert coordinator._pending_history is None


async def test_cancelled_import_keeps_store_lock_until_executor_finishes(
    coordinator, hass, entry, hass_storage
):
    """A real executor keeps writing after caller cancellation; it must remain ordered."""
    hass.config_entries.async_update_entry(entry, options={**entry.options,
        "demand_forecast": {"enabled": True, "history_house": "sensor.history"}})
    await coordinator.async_refresh()
    await coordinator.async_start_comparison()
    await coordinator._async_journal_idle()
    assert coordinator._shadow_snapshot["status"] == "running"
    assert coordinator._shadow_snapshot["samples"] == 0
    at = (dt_util.utcnow() - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    recorder = Mock()
    recorder.async_add_executor_job = AsyncMock(return_value={at.isoformat(): {"house_w": 500}})
    entered, release = Event(), Event()
    key = f"opti_akku.{entry.entry_id}"

    async def executor_write(data):
        payload = {**data}
        if "data_func" in payload:
            payload["data"] = payload.pop("data_func")()
        frozen = json.loads(json.dumps(deepcopy(payload)))

        def write():
            if frozen["data"]["demand_forecast"]["history"]["rows"]:
                entered.set()
                assert release.wait(5)
            hass_storage[key] = frozen

        await hass.async_add_executor_job(write)

    with patch("homeassistant.components.recorder.get_instance", return_value=recorder), patch.object(
        coordinator._store, "_async_write_data", new=executor_write
    ):
        caller = asyncio.create_task(coordinator.async_import_demand_history())
        setting = None
        try:
            assert await hass.async_add_executor_job(entered.wait, 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            for _ in range(2):
                for owner in tuple(coordinator._journal_owners):
                    owner.cancel()
                await asyncio.sleep(0)
            assert coordinator._store._write_lock.locked()
            assert coordinator._history_import_running
            assert not coordinator._demand_forecast.history.rows
            setting = asyncio.create_task(coordinator.async_set_setting("input_number.minsoc", 21))
            await asyncio.sleep(0)
            assert not setting.done()
            reads = coordinator.device.async_read.await_count
            await asyncio.wait_for(coordinator.async_refresh(), 2)
            assert coordinator.device.async_read.await_count == reads + 1
            # The owned import suppresses journal samples, leaving a truthful gap.
            assert coordinator._shadow_snapshot["samples"] == 0
            assert len(coordinator._journal_jobs) == 1
        finally:
            release.set()
            await asyncio.wait_for(coordinator._async_journal_idle(), 5)
            if setting is not None:
                await asyncio.wait_for(setting, 5)
            if not caller.done():
                await asyncio.wait_for(caller, 5)
        await flush_store(coordinator._store)

    assert hass_storage[key]["data"]["demand_forecast"]["history"]["rows"]
    assert hass_storage[key]["data"]["settings"]["input_number.minsoc"] == 21
    assert coordinator._demand_forecast.history.rows
    assert coordinator._pending_history is None
    assert not coordinator._history_import_running
