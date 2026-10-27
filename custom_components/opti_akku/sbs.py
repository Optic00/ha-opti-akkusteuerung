"""Read-only SMA Sunny Boy Storage profile for a first device test (#170).

The SMA Modbus profile assigns one register address per parameter across SMA
products, but not every product supports every register (SMA Modbus general
TI, version 1.0). This profile therefore reads the STP SE addresses of the same
parameters plus community evidence for the SBS (SBS 2.5 community list,
ioBroker project Maverick78de for SBS 3.7-10, see the adapter repository's
modbus-register-referenz.md). None of these reads has been verified on an
SBS 3.7-10 with this integration yet.

The profile never writes: it supports no mode and every command is refused.
A raw register snapshot in the probe result lets a tester compare values with
the SMA app before any control path for the SBS exists.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Any

from modbus_connection import ModbusConnectionError, ModbusError, ModbusUnit

from .sma import MODEL_NAMES, InvalidDataError, UnsupportedDeviceError, decode_s32, decode_u32

PROFILE_SMA_SBS = "sma_sbs"
STP_SE_DEVICE_CLASS = 8009
# Operation.RunStt (SMA profile, used by the STP SE) first, then the SBS 2.5
# community "Betriebsstatus" register.
STATUS_REGISTERS = (33003, 33001)
INFORMATION_NOT_AVAILABLE = 16777213
REGISTER_PROBE_SECONDS = 600.0
# (address, signed, evidence). Raw values only; meaning and scale are what the
# device test has to confirm. The serial number (30057) is deliberately absent.
REGISTER_PROBE = (
    (30051, False, "stp_se"),
    (30053, False, "stp_se_maverick"),
    (33003, False, "stp_se"),
    (33001, False, "sbs25_community"),
    (30775, True, "stp_se_maverick"),
    (30843, True, "sbs25_community"),
    (30845, False, "stp_se_maverick"),
    (30849, True, "stp_se_sbs25"),
    (30851, False, "sbs25_community"),
    (30865, True, "stp_se"),
    (30867, True, "stp_se_maverick"),
    (31061, False, "sbs25_community"),
    (31393, False, "stp_se_sbs25"),
    (31395, False, "stp_se_sbs25"),
    (40035, False, "maverick"),
    (40073, False, "maverick"),
    (40187, False, "stp_se"),
    (40189, False, "maverick"),
    (40191, False, "maverick"),
)


class SbsDevice:
    """One SMA Sunny Boy Storage, strictly read-only."""

    supported_modes: tuple[str, ...] = ()
    supports_control_release = False
    command_execution_basis = "none_read_only_profile"
    setpoint_readback_capability = "not_supported"
    setpoint_readback_limitation = "read_only_profile"
    supports_write_value_evidence = False

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.unit = unit
        self.blocked_write_attempts = 0
        self.last_read_errors: dict[str, str] = {}
        self.last_probe_registers: dict[str, Any] = {}
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._model_id: int | None = None
        self._status_address: int | None = None
        self._status_source: int | None = None
        self._register_probe: list[dict[str, Any]] = []
        self._register_probe_at: float | None = None
        self.unit.set_message_spacing(0.06)

    @property
    def model(self) -> str | None:
        return None if self._model_id is None else f"SMA Sunny Boy Storage (Gerätetyp {self._model_id})"

    async def _read(self, address: int, *, signed: bool = False) -> int:
        words = await self.unit.read_holding_registers(address, 2)
        raw = decode_u32(words)
        # SMA NaN sentinel: U32=FFFFFFFF, S32=80000000.
        if raw == (0x80000000 if signed else 0xFFFFFFFF):
            raise InvalidDataError(f"Register {address} reports unavailable data")
        return decode_s32(words) if signed else raw

    async def _read_status(self) -> int:
        """Prefer the confirmed status register; remember which one answered.

        16777213 is SMA's "information not available"; another register with a
        real status wins, otherwise that code is reported as it is.
        """
        # The last real answer is tried first, never exclusively.
        addresses = STATUS_REGISTERS if self._status_address is None else (
            self._status_address, *(address for address in STATUS_REGISTERS if address != self._status_address))
        last_error: Exception | None = None
        placeholder: tuple[int, int] | None = None
        for address in addresses:
            try:
                value = await self._read(address)
            except (ModbusConnectionError, TimeoutError):
                raise
            except (ModbusError, InvalidDataError) as err:
                last_error = err
                continue
            if value == INFORMATION_NOT_AVAILABLE:
                placeholder = placeholder or (address, value)
                continue
            self._status_address = self._status_source = address
            return value
        if placeholder is not None:
            # Keep probing all candidates until one reports a real status.
            self._status_address, self._status_source = None, placeholder[0]
            return placeholder[1]
        self._status_address = self._status_source = None
        raise InvalidDataError("No status register available") from last_error

    async def _snapshot_registers(self) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        for address, signed, evidence in REGISTER_PROBE:
            row: dict[str, Any] = {"address": address, "evidence": evidence}
            try:
                row["raw"] = await self._read(address, signed=signed)
            except ModbusConnectionError:
                row["error"] = "transport"
                snapshot.append(row)
                break  # Do not hammer an unreachable device once per register.
            except TimeoutError:
                # Before ModbusError: ModbusTimeoutError is both. Several silent
                # registers would otherwise exceed the coordinator's time limit.
                row["error"] = "timeout"
                snapshot.append(row)
                break
            except InvalidDataError:
                row["error"] = "not_available"
            except ModbusError:
                row["error"] = "modbus_error"
            snapshot.append(row)
        return snapshot

    async def _probe(self) -> dict[str, Any]:
        self.last_probe_registers = {}
        device_class = await self._read(30051)
        self.last_probe_registers["device_class"] = device_class
        model_id = await self._read(30053)
        self.last_probe_registers["model_id"] = model_id
        if device_class == STP_SE_DEVICE_CLASS and model_id in MODEL_NAMES:
            self._model_id = None
            raise UnsupportedDeviceError("STP SE detected; use the SMA STP SE profile")
        status = await self._read_status()
        self.last_probe_registers["status"] = status
        self._model_id = model_id
        try:
            serial_number = await self._read(30057)
        except ModbusError, InvalidDataError, TimeoutError:
            serial_number = 0
        now = self._monotonic()
        if self._register_probe_at is None or now - self._register_probe_at >= REGISTER_PROBE_SECONDS:
            # Stamp first: a snapshot cut short by a timeout waits for the next interval.
            self._register_probe_at = now
            self._register_probe = await self._snapshot_registers()
        return {
            "profile": PROFILE_SMA_SBS,
            "inverter_status": status,
            "status_register": self._status_source,
            "device_class": device_class,
            "model_id": model_id,
            "serial_number": str(serial_number) if serial_number else None,
            "model": self.model,
            "register_probe": [dict(row) for row in self._register_probe],
        }

    async def async_probe(self) -> dict[str, Any]:
        """Read identity, status and the raw register snapshot; never writes."""
        async with self._lock:
            return await self._probe()

    async def async_read(self) -> dict[str, Any]:
        """Read SBS-native values; PV generation must come from an HA source.

        sensor.opti_pv_power_w is the SBS's own AC power, which the plant
        balance treats as native AC output. Battery power is positive when
        charging, negative when discharging.
        """
        async with self._lock:
            if self._model_id is None:
                await self._probe()
            readings: dict[str, Any] = {}
            self.last_read_errors = {}
            specs = (
                ("soc", 30845, False, 1, 0, 100),
                ("battery_temp", 30849, True, 0.1, -60, 100),
                ("battery_capacity_kwh", 40187, False, 0.001, 0.001, 1000),
                ("battery_charge_w", 31393, False, 1, 0, 100000),
                ("battery_discharge_w", 31395, False, 1, 0, 100000),
                ("pv_power_w", 30775, True, 1, -100000, 100000),
                ("grid_import_w", 30865, True, 1, 0, 1000000),
                ("grid_export_w", 30867, True, 1, 0, 1000000),
            )
            readings["sensor.opti_inverter_status"] = None
            readings.update({f"sensor.opti_{spec[0]}": None for spec in specs})
            try:
                readings["sensor.opti_inverter_status"] = await self._read_status()
            except (ModbusConnectionError, TimeoutError) as err:
                self.last_read_errors["transport"] = str(err)
                return self._derive(readings)
            except (ModbusError, InvalidDataError) as err:
                self.last_read_errors["sensor.opti_inverter_status"] = str(err)
            for name, address, signed, scale, minimum, maximum in specs:
                key = f"sensor.opti_{name}"
                try:
                    value = await self._read(address, signed=signed) * scale
                    if not minimum <= value <= maximum:
                        raise InvalidDataError(f"Register {address} outside plausible range")
                    readings[key] = value
                except (ModbusConnectionError, TimeoutError) as err:
                    self.last_read_errors["transport"] = str(err)
                    break
                except (ModbusError, InvalidDataError) as err:
                    self.last_read_errors[key] = str(err)
            return self._derive(readings)

    @staticmethod
    def _derive(readings: dict[str, Any]) -> dict[str, Any]:
        charge, discharge = readings["sensor.opti_battery_charge_w"], readings["sensor.opti_battery_discharge_w"]
        readings["sensor.opti_battery_power_w"] = (
            charge - discharge if charge is not None and discharge is not None else None
        )
        values = [readings[f"sensor.opti_{key}"] for key in ("pv_power_w", "grid_import_w", "grid_export_w")]
        readings["sensor.opti_house_balance_w"] = (
            max(0, values[0] + values[1] - values[2]) if all(value is not None for value in values) else None
        )
        # The SBS has no PV input; total PV comes from the configured HA source.
        readings["sensor.opti_pv_generation_w"] = None
        return readings

    async def async_apply(
        self, mode: str, parameters: Mapping[str, float], is_current: Callable[[], bool]
    ) -> None:
        """The SBS profile has no control path yet."""
        self.blocked_write_attempts += 1
        raise PermissionError("SMA SBS profile is read-only")
