"""Wire-intent regression tests; no Home Assistant or hardware needed.

The fake implements the exact raw Unit API verified against 4.10.0. These tests
prove encoding, sequence, and cancellation behavior, not inverter enforcement.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest
from modbus_connection import ModbusError

# Load only the transport module, deliberately avoiding HA setup side effects.
_SOURCE = Path(__file__).resolve().parents[1] / "custom_components/opti_akku/sma.py"
# A private namespace resolves the pure shared contract without importing HA's
# integration entrypoint. The same relative import is used in production.
_package = ModuleType("opti_driver_under_test")
_package.__path__ = [str(_SOURCE.parent)]
sys.modules[_package.__name__] = _package
_spec = importlib.util.spec_from_file_location("opti_driver_under_test.sma", _SOURCE)
sma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sma)

PARAMETERS = {
    "charge_power_w": 3000,
    "charge_setpoint_w": 2000,
    "discharge_setpoint_w": 2500,
    "min_charge_w": 200,
    "max_charge_w": 10000,
    "min_discharge_w": 100,
    "max_discharge_w": 8000,
    "capacity_wh": 12800,
}


@pytest.mark.parametrize("mode", sma.MODES)
async def test_read_only_driver_blocks_every_mode_and_direct_write(device, mode):
    _, unit, _ = device
    adapter = sma.SmaDevice(unit, read_only=True)
    await adapter.async_probe()
    with pytest.raises(PermissionError):
        await adapter.async_apply(mode, PARAMETERS, lambda: True)
    with pytest.raises(PermissionError):
        await adapter._write(40151, 803)
    assert adapter.blocked_write_attempts == 2
    assert not unit.writes


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.delays = []
        self.on_sleep = None

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds
        if self.on_sleep:
            await self.on_sleep(seconds)
        await asyncio.sleep(0)


class FakeUnit:
    def __init__(self):
        self.values = {
            30051: 8009,
            30053: 19051,
            30057: 3012345678,
            33003: 235,
            30845: 54,
            30849: 237,
            40187: 12800,
            31393: 2000,
            31395: 0,
            30775: 3200,
            30865: 0,
            30867: 1000,
            30773: 3000,
            30961: 2400,
        }
        self.reads = []
        self.writes = []
        self.on_write = None
        self.fail_reads = set()
        self.fail_write_number = None
        self.fail_all_writes = False
        self.spacing = None

    def set_message_spacing(self, value):
        self.spacing = value

    async def read_holding_registers(self, address, count):
        self.reads.append((address, count))
        if address in self.fail_reads:
            raise ModbusError("injected read failure")
        raw = self.values[address]
        if isinstance(raw, list):
            return raw
        raw &= 0xFFFFFFFF
        return [raw >> 16, raw & 0xFFFF]

    async def write_registers(self, address, values):
        self.writes.append((address, values))
        if self.fail_all_writes or len(self.writes) == self.fail_write_number:
            raise ModbusError("injected write failure")
        if self.on_write:
            await self.on_write(address, values)


@pytest.fixture
def device():
    unit, clock = FakeUnit(), Clock()
    return sma.SmaDevice(unit, monotonic=clock, sleep=clock.sleep), unit, clock


def raw_writes(unit):
    return [(address, sma.decode_s32(value)) for address, value in unit.writes]


def last_bms(unit):
    result = {}
    for address, value in raw_writes(unit):
        if address in (40793, 40795, 40797, 40799, 40801, 41259, 40236):
            result[address] = value
    return result


@pytest.mark.parametrize("power", [0, 1, 3000, 10000, 65536, 2147483647])
def test_s32_exact_signed_encoding(power):
    assert sma.decode_s32(sma.encode_s32(-power)) == -power
    assert sma.decode_s32(sma.encode_s32(power)) == power
    if power == 3000:
        assert sma.encode_s32(-power) == [65535, 62536]
    if power == 0:
        assert sma.encode_s32(-power) == [0, 0]


@pytest.mark.parametrize("words", [[], [1], [1, 2, 3], [65536, 0], [-1, 0], [0, True]])
def test_invalid_register_words(words):
    with pytest.raises(sma.InvalidDataError):
        sma.decode_u32(words)


async def test_probe_is_readonly_and_verifies_model(device):
    adapter, unit, _ = device
    result = await adapter.async_probe()
    assert result == {
        "inverter_status": 235,
        "device_class": 8009,
        "model_id": 19051,
        "model": "STP10.0-3SE-40",
        "max_power_w": 10000,
        "serial_number": "3012345678",
    }
    assert unit.reads == [(30051, 2), (30053, 2), (33003, 2), (30057, 2)]
    assert unit.writes == []
    assert unit.spacing == 0.06


@pytest.mark.parametrize("address,value", [(30051, 8001), (30053, 12345)])
async def test_probe_rejects_wrong_hardware(device, address, value):
    adapter, unit, _ = device
    unit.values[address] = value
    with pytest.raises(sma.UnsupportedDeviceError):
        await adapter.async_probe()
    assert unit.writes == []


async def test_read_signed_values_and_derived_values(device):
    adapter, unit, _ = device
    unit.values[30849] = -35
    unit.values[31393] = 0
    unit.values[31395] = 1200
    values = await adapter.async_read()
    assert values["sensor.opti_battery_temp"] == -3.5
    assert values["sensor.opti_battery_power_w"] == -1200
    assert values["sensor.opti_battery_capacity_kwh"] == 12.8
    assert values["sensor.opti_pv_power_w"] == 3200
    assert values["sensor.opti_pv_generation_w"] == 5400
    assert values["sensor.opti_house_balance_w"] == 2200
    assert "sensor.opti_house_consumption_w" not in values
    assert not unit.writes


@pytest.mark.parametrize(
    "address,value,key",
    [
        (30845, 0xFFFFFFFF, "soc"),
        (30845, 101, "soc"),
        (30849, 0x80000000, "battery_temp"),
        (30865, 0x80000000, "grid_import_w"),
        (30865, -1, "grid_import_w"),
        (40187, 0, "battery_capacity_kwh"),
    ],
)
async def test_invalid_readings_stay_unavailable(device, address, value, key):
    adapter, unit, _ = device
    unit.values[address] = value
    values = await adapter.async_read()
    assert values[f"sensor.opti_{key}"] is None
    assert f"sensor.opti_{key}" in adapter.last_read_errors
    if key == "grid_import_w":
        assert values["sensor.opti_house_balance_w"] is None
    assert not unit.writes


async def test_partial_read_failure_invalidates_dependent_values(device):
    adapter, unit, _ = device
    unit.fail_reads = {31393, 30961}
    values = await adapter.async_read()
    assert values["sensor.opti_battery_power_w"] is None
    assert values["sensor.opti_pv_generation_w"] is None
    assert values["sensor.opti_soc"] == 54


@pytest.mark.parametrize(
    "mode,power",
    [
        (sma.FAST_CHARGE, -2000),
        (sma.GRID_CHARGE, -3000),
        (sma.FAST_DISCHARGE, 2500),
        (sma.CHARGE_02C, -2560),
    ],
)
async def test_all_four_command_modes(device, mode, power):
    adapter, unit, clock = device
    await adapter.async_apply(mode, PARAMETERS, lambda: True)
    writes = raw_writes(unit)
    assert writes[:7] == [
        (40151, 803),
        (40793, 0),
        (40795, 10000),
        (40797, 0),
        (40799, 8000),
        (40801, 0),
        (41259, 1438),
    ]
    assert writes[-2:] == [(40151, 802), (40149, power)]
    assert 2 in clock.delays
    assert adapter.last_write is not None and adapter.last_mode == mode
    assert adapter.last_error is None


async def test_automatic_preserves_reset_sequence(device):
    adapter, unit, _ = device
    await adapter.async_apply(sma.AUTO, {}, lambda: True)
    assert raw_writes(unit)[-2:] == [(40151, 802), (40151, 803)]
    assert not any(address == 40149 for address, _ in unit.writes)


@pytest.mark.parametrize(
    "mode,windows,opmod,address",
    [
        (sma.PAUSE, (0, 0, 0, 0), 303, 41259),
        (sma.ONLY_CHARGE, (200, 3000, 0, 0), 2289, 41259),
        (sma.ONLY_DISCHARGE, (0, 0, 100, 8000), 2290, 41259),
        (sma.DYNAMIC, (200, 500, 100, 8000), 1438, 40236),
    ],
)
async def test_all_four_bms_modes(device, mode, windows, opmod, address):
    adapter, unit, _ = device
    await adapter.async_apply(mode, PARAMETERS, lambda: True)
    assert raw_writes(unit) == [
        (40151, 803),
        *zip((40793, 40795, 40797, 40799), windows),
        (40801, 0),
        (address, opmod),
    ]
    assert adapter.last_mode == mode


async def test_dynamic_settling_and_renewal(device):
    adapter, unit, clock = device
    await adapter.async_apply(sma.DYNAMIC, PARAMETERS, lambda: True)
    assert last_bms(unit)[40795] == 500
    unit.writes.clear()
    clock.now += 120
    await adapter.async_apply(sma.DYNAMIC, PARAMETERS, lambda: True)
    assert len(unit.writes) == 7 and last_bms(unit)[40795] == 500
    clock.now += 330
    unit.writes.clear()
    await adapter.async_apply(sma.DYNAMIC, PARAMETERS, lambda: True)
    assert len(unit.writes) == 7 and last_bms(unit)[40795] == 3000


@pytest.mark.parametrize(
    "floor,target,expected",
    [(200, 100, (0, 0)), (200, 200, (0, 200)), (0, 0, (0, 0)), (200, 3000, (200, 3000))],
)
def test_power_window_invariant(floor, target, expected):
    assert sma.power_window(target, floor) == expected
    assert sma.power_window(target, floor, locked=True) == (0, 0)


@pytest.mark.parametrize(
    "mode,key",
    [
        (sma.FAST_CHARGE, "charge_setpoint_w"),
        (sma.FAST_DISCHARGE, "discharge_setpoint_w"),
        (sma.GRID_CHARGE, "charge_power_w"),
    ],
)
async def test_manual_command_limits(device, mode, key):
    adapter, unit, _ = device
    params = PARAMETERS | {key: 123456, "max_charge_w": 5000, "max_discharge_w": 4000}
    await adapter.async_apply(mode, params, lambda: True)
    expected = 4000 if mode == sma.FAST_DISCHARGE else -5000
    assert raw_writes(unit)[-1] == (40149, expected)


async def test_model_rating_clamps_power(device):
    adapter, unit, _ = device
    unit.values[30053] = 19048
    await adapter.async_apply(sma.GRID_CHARGE, PARAMETERS | {"charge_power_w": 10000}, lambda: True)
    assert raw_writes(unit)[-1] == (40149, -5000)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, True, "unknown"])
async def test_invalid_parameters_cannot_write(device, bad):
    adapter, unit, _ = device
    with pytest.raises(sma.InvalidDataError):
        await adapter.async_apply(
            sma.GRID_CHARGE, PARAMETERS | {"charge_power_w": bad}, lambda: True
        )
    assert unit.writes == []


async def test_missing_capacity_and_mode_rejected(device):
    adapter, unit, _ = device
    with pytest.raises(sma.InvalidDataError):
        await adapter.async_apply(sma.CHARGE_02C, {"capacity_wh": 0}, lambda: True)
    with pytest.raises(sma.InvalidDataError):
        await adapter.async_apply("Akku Unicorn", {}, lambda: True)
    assert not unit.writes


@pytest.mark.parametrize("status", [1469, 16777213, 1463, 0xFFFFFFFF])
async def test_status_gate_blocks_all_writes(device, status):
    adapter, unit, _ = device
    unit.values[33003] = status
    with pytest.raises((sma.InverterNotReadyError, sma.InvalidDataError)):
        await adapter.async_apply(sma.PAUSE, PARAMETERS, lambda: True)
    assert unit.writes == []


@pytest.mark.parametrize("status", [235, 2119])
async def test_grid_and_derating_status_allowed(device, status):
    adapter, unit, _ = device
    unit.values[33003] = status
    await adapter.async_apply(sma.PAUSE, PARAMETERS, lambda: True)
    assert adapter.last_mode == sma.PAUSE


async def test_apply_rechecks_status_instead_of_using_old_read(device):
    adapter, unit, _ = device
    await adapter.async_read()
    unit.values[33003] = 1469
    with pytest.raises(sma.InverterNotReadyError):
        await adapter.async_apply(sma.PAUSE, PARAMETERS, lambda: True)
    assert not unit.writes


async def test_stale_before_start_has_no_side_effect(device):
    adapter, unit, _ = device
    with pytest.raises(sma.StaleCommandError):
        await adapter.async_apply(sma.FAST_CHARGE, PARAMETERS, lambda: False)
    assert unit.writes == [] and unit.reads == []


async def test_stale_during_802_delay_cleans_up_without_old_setpoint(device):
    adapter, unit, clock = device
    current = True

    async def change(seconds):
        nonlocal current
        if seconds == 2:
            current = False

    clock.on_sleep = change
    with pytest.raises(sma.StaleCommandError):
        await adapter.async_apply(sma.FAST_CHARGE, PARAMETERS, lambda: current)
    assert not any(address == 40149 for address, _ in unit.writes)
    assert raw_writes(unit)[-7:] == [
        (40151, 803),
        (40793, 0),
        (40795, 0),
        (40797, 0),
        (40799, 0),
        (40801, 0),
        (41259, 303),
    ]
    assert adapter.last_mode == sma.PAUSE


@pytest.mark.parametrize("stale_address", [40793, 40795, 40797, 40799, 40801, 41259])
async def test_stale_during_release_conservatively_locks(device, stale_address):
    adapter, unit, _ = device
    current = True

    async def change(address, values):
        nonlocal current
        if address == stale_address:
            current = False

    unit.on_write = change
    with pytest.raises(sma.StaleCommandError):
        await adapter.async_apply(sma.GRID_CHARGE, PARAMETERS, lambda: current)
    assert raw_writes(unit)[-1] == (41259, 303)
    assert all(last_bms(unit)[address] == 0 for address in (40793, 40795, 40797, 40799))
    assert not any(address == 40149 for address, _ in unit.writes)


async def test_cancel_during_delay_finishes_cleanup_before_return(device):
    adapter, unit, clock = device
    in_delay = asyncio.Event()

    async def block(seconds):
        if seconds == 2:
            in_delay.set()
            await asyncio.Event().wait()

    clock.on_sleep = block
    task = asyncio.create_task(adapter.async_apply(sma.FAST_CHARGE, PARAMETERS, lambda: True))
    await in_delay.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert raw_writes(unit)[-1] == (41259, 303)
    assert not adapter._lock.locked()
    assert not any(address == 40149 for address, _ in unit.writes)


async def test_repeated_cancellation_cannot_detach_cleanup_writer(device):
    adapter, unit, clock = device
    in_delay, cleanup_delay, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def block(seconds):
        if seconds == 2:
            in_delay.set()
            await asyncio.Event().wait()
        elif len(unit.writes) >= 9:
            cleanup_delay.set()
            await release_cleanup.wait()

    clock.on_sleep = block
    task = asyncio.create_task(adapter.async_apply(sma.FAST_CHARGE, PARAMETERS, lambda: True))
    await in_delay.wait()
    task.cancel()
    await cleanup_delay.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and adapter._lock.locked()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert raw_writes(unit)[-1] == (41259, 303)
    assert not adapter._lock.locked()


async def test_cancel_midflight_802_write_still_attempts_cleanup(device):
    adapter, unit, _ = device
    in_write = asyncio.Event()

    async def block(address, values):
        if address == 40151 and values == [0, 802]:
            in_write.set()
            await asyncio.Event().wait()

    unit.on_write = block
    task = asyncio.create_task(adapter.async_apply(sma.GRID_CHARGE, PARAMETERS, lambda: True))
    await in_write.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert raw_writes(unit)[-1] == (41259, 303)


async def test_error_in_write_cleans_up_and_surfaces_failure(device):
    adapter, unit, _ = device
    unit.fail_write_number = 3
    with pytest.raises(ModbusError):
        await adapter.async_apply(sma.DYNAMIC, PARAMETERS, lambda: True)
    assert adapter.last_mode == sma.PAUSE
    assert "ModbusError" in adapter.last_error


async def test_cleanup_failure_does_not_claim_safety(device):
    adapter, unit, _ = device
    unit.fail_all_writes = True
    with pytest.raises(ModbusError):
        await adapter.async_apply(sma.PAUSE, PARAMETERS, lambda: True)
    assert adapter.last_mode is None
    assert adapter.last_write is None
    assert "Cleanup failed" in adapter.last_error


async def test_competing_commands_are_serialized_and_latest_wins(device):
    adapter, unit, clock = device
    in_delay, release_old = asyncio.Event(), asyncio.Event()
    current = True

    async def block(seconds):
        if seconds == 2:
            in_delay.set()
            await release_old.wait()

    clock.on_sleep = block
    old = asyncio.create_task(adapter.async_apply(sma.FAST_CHARGE, PARAMETERS, lambda: current))
    await in_delay.wait()
    current = False
    new = asyncio.create_task(adapter.async_apply(sma.ONLY_DISCHARGE, PARAMETERS, lambda: True))
    await asyncio.sleep(0)
    before_release = list(unit.writes)
    await asyncio.sleep(0)
    assert unit.writes == before_release
    release_old.set()
    with pytest.raises(sma.StaleCommandError):
        await old
    await new
    assert not any(address == 40149 for address, _ in unit.writes)
    assert adapter.last_mode == sma.ONLY_DISCHARGE
    assert raw_writes(unit)[-1] == (41259, 2290)


async def test_repeat_command_never_toggles_803_before_setpoint(device):
    adapter, unit, _ = device
    await adapter.async_apply(sma.CHARGE_02C, PARAMETERS, lambda: True)
    unit.writes.clear()
    await adapter.async_apply(sma.CHARGE_02C, PARAMETERS, lambda: True)
    assert raw_writes(unit) == [(40151, 802), (40149, -2560)]


@pytest.mark.parametrize("previous", [sma.AUTO, sma.FAST_CHARGE])
async def test_automatic_refreshes_reduced_charge_limit(device, previous):
    adapter, unit, _ = device
    await adapter.async_apply(previous, PARAMETERS, lambda: True)
    unit.writes.clear()
    await adapter.async_apply(sma.AUTO, {**PARAMETERS, "max_charge_w": 1000}, lambda: True)
    writes = raw_writes(unit)
    assert (40795, 1000) in writes
    assert writes[-2:] == [(40151, 802), (40151, 803)]


async def test_command_to_lock_releases_802_before_bms(device):
    adapter, unit, _ = device
    await adapter.async_apply(sma.GRID_CHARGE, PARAMETERS, lambda: True)
    unit.writes.clear()
    await adapter.async_apply(sma.PAUSE, PARAMETERS, lambda: True)
    assert raw_writes(unit)[0] == (40151, 803)
    assert raw_writes(unit)[-1] == (41259, 303)


@pytest.mark.parametrize("previous", [sma.PAUSE, sma.DYNAMIC])
async def test_lock_to_command_releases_complete_bms_windows(device, previous):
    adapter, unit, _ = device
    await adapter.async_apply(previous, PARAMETERS, lambda: True)
    unit.writes.clear()
    await adapter.async_apply(sma.GRID_CHARGE, PARAMETERS, lambda: True)
    writes = raw_writes(unit)
    assert writes[0] == (40151, 803)
    assert writes[1:5] == [(40793, 0), (40795, 10000), (40797, 0), (40799, 8000)]
    assert writes[6] == (41259, 1438)
    assert writes[7:] == [(40151, 802), (40149, -3000)]


async def test_bms_deadline_failure_is_not_recorded_as_success(device, monkeypatch):
    adapter, unit, _ = device
    monkeypatch.setattr(sma, "BMS_TRANSACTION_SECONDS", 0.02)
    stalled = False

    async def delay_once(address, values):
        nonlocal stalled
        if address == 40795 and not stalled:
            stalled = True
            await asyncio.sleep(0.05)

    unit.on_write = delay_once
    with pytest.raises(TimeoutError):
        await adapter.async_apply(sma.DYNAMIC, PARAMETERS, lambda: True)
    assert adapter.last_mode == sma.PAUSE
    assert "TimeoutError" in adapter.last_error
    assert raw_writes(unit)[-1] == (41259, 303)


async def test_status_age_cannot_hold_gate_open(device):
    adapter, unit, clock = device

    async def age_status(seconds):
        if seconds == 2:
            clock.now += 40

    clock.on_sleep = age_status
    with pytest.raises(sma.InverterNotReadyError):
        await adapter.async_apply(sma.GRID_CHARGE, PARAMETERS, lambda: True)
    assert raw_writes(unit)[-1] == (40151, 802)
    assert not any(address == 40149 for address, _ in unit.writes)
    assert adapter.last_mode is None and "Cleanup failed" in adapter.last_error


async def test_bms_family_not_read_back(device):
    adapter, unit, _ = device
    await adapter.async_read()
    await adapter.async_apply(sma.PAUSE, PARAMETERS, lambda: True)
    forbidden = {40149, 40151, 40236, 40793, 40795, 40797, 40799, 40801, 41259}
    assert not any(address in forbidden for address, _ in unit.reads)


async def test_actual_modbus_connection_unit_api():
    from modbus_connection.mock import MockModbusConnection

    connection = MockModbusConnection()
    unit = connection.for_unit(3)
    for address, raw in FakeUnit().values.items():
        words = [(raw >> 16) & 0xFFFF, raw & 0xFFFF]
        unit.holding.update({address: words[0], address + 1: words[1]})
    clock = Clock()
    adapter = sma.SmaDevice(unit, monotonic=clock, sleep=clock.sleep)
    assert (await adapter.async_probe())["model_id"] == 19051
    assert (await adapter.async_read())["sensor.opti_soc"] == 54
    await adapter.async_apply(sma.GRID_CHARGE, PARAMETERS, lambda: True)
    assert [unit.holding[40149], unit.holding[40150]] == [65535, 62536]
    assert adapter.last_mode == sma.GRID_CHARGE
    await connection.close()


@pytest.mark.parametrize("fail_cleanup", [False, True])
async def test_superseded_command_reports_cleanup_outcome(device, fail_cleanup):
    adapter, unit, clock = device
    current = True
    async def change(seconds):
        nonlocal current
        if seconds == 2:
            current = False
            unit.fail_all_writes = fail_cleanup
    clock.on_sleep = change
    with pytest.raises(sma.StaleCommandError) as exc:
        await adapter.async_apply(sma.FAST_CHARGE, PARAMETERS, lambda: current)
    assert exc.value.cleanup_failed is fail_cleanup
    assert adapter.last_mode == (None if fail_cleanup else sma.PAUSE)
    assert sma.StaleCommandError().cleanup_failed is False
