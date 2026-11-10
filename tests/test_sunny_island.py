"""Read-only SMA Sunny Island profile (#194); no Home Assistant needed."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

from tests.test_sbs import NAN_U32, FakeUnit

_SOURCE = Path(__file__).resolve().parents[1] / "custom_components/opti_akku"
_package = ModuleType("opti_si_under_test")
_package.__path__ = [str(_SOURCE)]
sys.modules[_package.__name__] = _package
for name in ("sma", "sbs", "sunny_island"):
    spec = importlib.util.spec_from_file_location(f"opti_si_under_test.{name}", _SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
si = sys.modules["opti_si_under_test.sunny_island"]
sbs = sys.modules["opti_si_under_test.sbs"]
sma = sys.modules["opti_si_under_test.sma"]


@pytest.fixture
def unit():
    """A Sunny Island as far as the sources describe it: no RunStt, no charge/discharge registers."""
    unit = FakeUnit()
    unit.values.update({30053: 9475, 40029: 569, 30775: -4600, 30865: 4800, 30867: 0,
                        30853: 1767, 31009: 20, 40236: 2424, 40801: 0})
    for address in (33003, 33001, 31393, 31395):
        del unit.values[address]
    return unit


async def test_probe_identifies_a_sunny_island_by_opermode(unit):
    device = si.SunnyIslandDevice(unit)
    probe = await device.async_probe()
    assert probe["profile"] == "sma_sunny_island"
    assert (probe["model_id"], probe["inverter_status"], probe["status_register"]) == (9475, 569, 40029)
    assert probe["model"] == "SMA Sunny Island (Gerätetyp 9475)"
    rows = {row["address"]: row for row in probe["register_probe"]}
    assert 30057 not in rows
    assert rows[40029] == {"address": 40029, "evidence": "issue_194", "raw": 569}
    assert rows[40236] == {"address": 40236, "evidence": "maverick_si", "raw": 2424}
    # Control registers the device does not answer are reported, not hidden.
    assert rows[40149] == {"address": 40149, "evidence": "maverick_si_evcc", "error": "modbus_error"}
    assert rows[31393]["error"] == "modbus_error"
    assert len(rows) == len(probe["register_probe"])
    assert 569 in device.allowed_statuses


async def test_runstt_wins_when_the_device_reports_it(unit):
    unit.values[33003] = 235
    probe = await si.SunnyIslandDevice(unit).async_probe()
    assert (probe["inverter_status"], probe["status_register"]) == (235, 33003)


async def test_runstt_is_rechecked_after_opermode_answered(unit):
    """Review finding: a cached OperMode must not hide a later RunStt value."""
    unit.values[33003] = 16777213
    device = si.SunnyIslandDevice(unit)
    assert (await device.async_read())["sensor.opti_inverter_status"] == 569
    unit.values[33003] = 1463
    assert (await device.async_read())["sensor.opti_inverter_status"] == 1463


async def test_probe_rejects_an_stp_se_identity(unit):
    unit.values.update({30051: 8009, 30053: 19051})
    with pytest.raises(sma.UnsupportedDeviceError):
        await si.SunnyIslandDevice(unit).async_probe()


async def test_battery_power_falls_back_to_inverted_ac_power(unit):
    readings = await si.SunnyIslandDevice(unit).async_read()
    assert readings["sensor.opti_inverter_status"] == 569
    assert readings["sensor.opti_soc"] == 61
    assert readings["sensor.opti_pv_power_w"] == -4600
    assert readings["sensor.opti_battery_power_w"] == 4600  # charging is positive
    assert readings["sensor.opti_house_balance_w"] == 200
    assert readings["sensor.opti_pv_generation_w"] is None


async def test_charge_and_discharge_registers_win_over_the_fallback(unit):
    unit.values.update({31393: 0, 31395: 2500, 30775: 2600})
    readings = await si.SunnyIslandDevice(unit).async_read()
    assert readings["sensor.opti_battery_power_w"] == -2500


async def test_battery_power_stays_missing_without_ac_power(unit):
    unit.values[30775] = 0x80000000
    unit.values[31393] = NAN_U32
    readings = await si.SunnyIslandDevice(unit).async_read()
    assert readings["sensor.opti_battery_power_w"] is None
    assert readings["sensor.opti_pv_power_w"] is None


@pytest.mark.parametrize("mode", sma.MODES)
async def test_every_command_is_refused_without_touching_the_device(unit, mode):
    device = si.SunnyIslandDevice(unit)
    await device.async_probe()
    with pytest.raises(PermissionError, match="Sunny Island"):
        await device.async_apply(mode, {}, lambda: True)
    assert device.blocked_write_attempts == 1
    assert device.supported_modes == ()
    assert not unit.writes
    assert not hasattr(device, "_write")


def test_registry_lists_both_read_only_profiles():
    assert si.READ_ONLY_SMA_PROFILES == {"sma_sbs": sbs.SbsDevice, "sma_sunny_island": si.SunnyIslandDevice}
    addresses = [row[0] for row in si.SunnyIslandDevice.probe_registers]
    assert len(addresses) == len(set(addresses))
    assert 30057 not in addresses
