"""Read-only SMA Sunny Boy Storage profile (#170); no Home Assistant needed."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest
from modbus_connection import ModbusConnectionError, ModbusError

_SOURCE = Path(__file__).resolve().parents[1] / "custom_components/opti_akku"
_package = ModuleType("opti_sbs_under_test")
_package.__path__ = [str(_SOURCE)]
sys.modules[_package.__name__] = _package
for name in ("sma", "sbs"):
    spec = importlib.util.spec_from_file_location(f"opti_sbs_under_test.{name}", _SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
sbs = sys.modules["opti_sbs_under_test.sbs"]
sma = sys.modules["opti_sbs_under_test.sma"]

NAN_U32 = 0xFFFFFFFF
NAN_S32 = 0x80000000


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeUnit:
    def __init__(self, **overrides):
        self.values = {
            30051: 8007,
            30053: 9357,
            30057: 3012345678,
            33003: 235,
            33001: 307,
            30775: -1800,
            30843: 1200,
            30845: 61,
            30849: 214,
            30851: 4950,
            30865: 0,
            30867: 400,
            31061: 1,
            31393: 1800,
            31395: 0,
            40035: 1785,
            40073: 10,
            40187: 10240,
            40189: 5000,
            40191: 5000,
        }
        self.values.update(overrides)
        self.reads = []
        self.writes = []
        self.fail_reads = set()
        self.disconnect_at = None
        self.spacing = None

    def set_message_spacing(self, value):
        self.spacing = value

    async def read_holding_registers(self, address, count):
        self.reads.append(address)
        if address == self.disconnect_at:
            raise ModbusConnectionError("injected disconnect")
        if address in self.fail_reads or address not in self.values:
            raise ModbusError("illegal data address")
        raw = self.values[address] & 0xFFFFFFFF
        return [raw >> 16, raw & 0xFFFF]

    async def write_registers(self, address, values):  # pragma: no cover - must never run
        self.writes.append((address, values))


@pytest.fixture
def unit():
    return FakeUnit()


async def test_probe_accepts_an_sbs_and_reports_raw_registers_without_serial(unit):
    device = sbs.SbsDevice(unit)
    probe = await device.async_probe()
    assert probe["profile"] == "sma_sbs"
    assert (probe["device_class"], probe["model_id"], probe["inverter_status"]) == (8007, 9357, 235)
    assert probe["status_register"] == 33003
    assert probe["model"] == "SMA Sunny Boy Storage (Gerätetyp 9357)"
    assert probe["serial_number"] == "3012345678"
    rows = {row["address"]: row for row in probe["register_probe"]}
    assert 30057 not in rows
    assert rows[30775]["raw"] == -1800
    assert rows[40189] == {"address": 40189, "evidence": "maverick", "raw": 5000}
    assert unit.spacing == 0.06


async def test_probe_rejects_an_stp_se_identity(unit):
    unit.values.update({30051: 8009, 30053: 19051})
    with pytest.raises(sma.UnsupportedDeviceError):
        await sbs.SbsDevice(unit).async_probe()


async def test_unsupported_and_nan_registers_are_reported_not_hidden(unit):
    unit.fail_reads.add(31061)
    unit.values[40073] = NAN_U32
    rows = {row["address"]: row for row in (await sbs.SbsDevice(unit).async_probe())["register_probe"]}
    assert rows[31061]["error"] == "modbus_error"
    assert rows[40073]["error"] == "not_available"
    assert rows[40191]["raw"] == 5000  # later registers are still read


async def test_register_snapshot_is_refreshed_only_every_ten_minutes(unit):
    clock = Clock()
    device = sbs.SbsDevice(unit, monotonic=clock)
    await device.async_probe()
    reads = len(unit.reads)
    await device.async_probe()
    assert len(unit.reads) - reads == 4  # identity, status and serial only
    clock.now += sbs.REGISTER_PROBE_SECONDS
    unit.values[40189] = 4600
    rows = {row["address"]: row for row in (await device.async_probe())["register_probe"]}
    assert rows[40189]["raw"] == 4600


async def test_status_falls_back_when_runstt_is_missing_or_a_placeholder(unit):
    unit.fail_reads.add(33003)
    device = sbs.SbsDevice(unit)
    probe = await device.async_probe()
    assert (probe["inverter_status"], probe["status_register"]) == (307, 33001)
    unit.fail_reads.clear()
    unit.values[33003] = sbs.INFORMATION_NOT_AVAILABLE
    fresh = await sbs.SbsDevice(unit).async_probe()
    assert (fresh["inverter_status"], fresh["status_register"]) == (307, 33001)


async def test_placeholder_status_is_kept_when_no_other_register_answers(unit):
    unit.values[33003] = sbs.INFORMATION_NOT_AVAILABLE
    unit.fail_reads.add(33001)
    probe = await sbs.SbsDevice(unit).async_probe()
    assert (probe["inverter_status"], probe["status_register"]) == (sbs.INFORMATION_NOT_AVAILABLE, 33003)


async def test_probe_fails_without_any_status_register(unit):
    unit.fail_reads.update({33003, 33001})
    with pytest.raises(sma.InvalidDataError):
        await sbs.SbsDevice(unit).async_probe()


async def test_read_maps_sbs_values_and_leaves_pv_generation_to_a_source(unit):
    readings = await sbs.SbsDevice(unit).async_read()
    assert readings["sensor.opti_inverter_status"] == 235
    assert readings["sensor.opti_soc"] == 61
    assert readings["sensor.opti_battery_temp"] == pytest.approx(21.4)
    assert readings["sensor.opti_battery_capacity_kwh"] == pytest.approx(10.24)
    assert readings["sensor.opti_battery_power_w"] == 1800
    assert readings["sensor.opti_pv_power_w"] == -1800
    assert readings["sensor.opti_house_balance_w"] == 0  # -1800 + 0 - 400, clamped
    assert readings["sensor.opti_pv_generation_w"] is None


async def test_read_keeps_unavailable_values_missing(unit):
    unit.values[31395] = NAN_U32
    unit.values[30775] = NAN_S32
    device = sbs.SbsDevice(unit)
    readings = await device.async_read()
    assert readings["sensor.opti_battery_power_w"] is None
    assert readings["sensor.opti_house_balance_w"] is None
    assert set(device.last_read_errors) == {"sensor.opti_battery_discharge_w", "sensor.opti_pv_power_w"}


async def test_transport_loss_stops_reading_and_is_reported(unit):
    device = sbs.SbsDevice(unit)
    await device.async_probe()
    unit.disconnect_at = 30849
    readings = await device.async_read()
    assert "transport" in device.last_read_errors
    assert readings["sensor.opti_soc"] == 61
    assert readings["sensor.opti_battery_temp"] is None
    assert 40187 not in unit.reads[-3:]


@pytest.mark.parametrize("mode", sma.MODES)
async def test_every_command_is_refused_without_touching_the_device(unit, mode):
    device = sbs.SbsDevice(unit)
    await device.async_probe()
    with pytest.raises(PermissionError):
        await device.async_apply(mode, {}, lambda: True)
    assert device.blocked_write_attempts == 1
    assert device.supported_modes == ()
    assert not unit.writes
    assert not hasattr(device, "_write")
