"""SMA STP SE transport adapter using HA's shared ModbusUnit.

Read registers and model IDs: official SMA parameter export
https://files.sma.de/downloads/PARAMETER-HTML_STPxx-3SE-40_30109R_V11.zip
(2022 V11, checked 2026-09-12). Write sequences port the tracked July 2026
sma_stp_se_adapter.yaml in Optic00/ha-modbus-akku-adapter. Several CmpBMS
registers are community/device-test evidence, absent from that SMA export.
They are never assumed readable, and successful writes are not proof of effect.

The caller owns activation, source freshness, entity lifecycle, and renewal at
most every 120 seconds. This object serializes complete transactions, prevents
stale commands and cleans up interrupted transactions before releasing its lock.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from modbus_connection import ModbusConnectionError, ModbusError, ModbusUnit

from .device import DeviceError as SmaError, StaleCommandError

AUTO = "Akku Automatisch"
DYNAMIC = "Akku Dynamisch"
PAUSE = "Akku Pause"
ONLY_CHARGE = "Akku nur Laden"
GRID_CHARGE = "Akku Netzladen"
ONLY_DISCHARGE = "Akku nur Entladen"
FAST_CHARGE = "Akku schnell Laden"
FAST_DISCHARGE = "Akku schnell Entladen"
CHARGE_02C = "Akku 0.2C Laden"
MODES = (
    AUTO,
    DYNAMIC,
    PAUSE,
    ONLY_CHARGE,
    GRID_CHARGE,
    ONLY_DISCHARGE,
    FAST_CHARGE,
    FAST_DISCHARGE,
    CHARGE_02C,
)
RAIL_MODES = frozenset((AUTO, FAST_CHARGE, FAST_DISCHARGE, GRID_CHARGE, CHARGE_02C))
LOCK_MODES = frozenset((PAUSE, ONLY_CHARGE, ONLY_DISCHARGE))
MODEL_NAMES = {
    19048: "STP5.0-3SE-40",
    19049: "STP6.0-3SE-40",
    19050: "STP8.0-3SE-40",
    19051: "STP10.0-3SE-40",
}
MODEL_POWER_W = {19048: 5000, 19049: 6000, 19050: 8000, 19051: 10000}
RECONCILE_SECONDS = 120
SETTLING_SECONDS = 330
SETTLING_CHARGE_W = 500
REGISTER_GAP_SECONDS = 0.5
COMMAND_DELAY_SECONDS = 2.0
BMS_TRANSACTION_SECONDS = 9.0
STATUS_MAX_AGE_SECONDS = 30.0


class UnsupportedDeviceError(SmaError):
    """The read-only identity does not match the STP SE profile."""


class InvalidDataError(SmaError, ValueError):
    """A register or command contains invalid data."""


class InverterNotReadyError(SmaError):
    """The current operating status does not permit writes."""


def encode_s32(value: int) -> list[int]:
    """Encode exact signed big-endian S32; 0 W is exactly [0, 0]."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataError("S32 requires an integer")
    if not -(1 << 31) <= value < (1 << 31):
        raise InvalidDataError("S32 value outside its representable range")
    raw = value & 0xFFFFFFFF
    return [raw >> 16, raw & 0xFFFF]


def decode_u32(words: Sequence[int]) -> int:
    """Decode a complete big-endian word pair without accepting corrupt words."""
    if len(words) != 2 or any(
        isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= 65535
        for word in words
    ):
        raise InvalidDataError("Expected two unsigned 16-bit register words")
    return (words[0] << 16) | words[1]


def decode_s32(words: Sequence[int]) -> int:
    raw = decode_u32(words)
    return raw - (1 << 32) if raw & (1 << 31) else raw


def power_window(target: int, floor: int, *, locked: bool = False) -> tuple[int, int]:
    """Preserve zero locks and never generate an equal positive min/max pair."""
    ceiling = 0 if locked or target < floor else target
    return (floor if 0 < floor < ceiling else 0, ceiling)


class SmaDevice:
    """One verified STP SE unit; never opens or closes a connection itself."""

    supported_modes = MODES
    supports_control_release = False
    command_execution_basis = "modbus_write_sequence"
    setpoint_readback_capability = "not_supported"
    setpoint_readback_limitation = "bms_and_setpoints_not_read_back"

    def __init__(
        self,
        unit: ModbusUnit,
        *,
        allowed_statuses: Sequence[int] = (235, 2119),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_only: bool = False,
    ) -> None:
        accepted = frozenset(int(value) for value in allowed_statuses)
        if not accepted or not accepted <= {235, 1463, 2119}:
            raise ValueError("Only confirmed operating codes 235, 1463, 2119 are allowed")
        self.unit = unit
        self._read_only = read_only
        self.blocked_write_attempts = 0
        self.allowed_statuses = accepted
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._model_id: int | None = None
        self._status: int | None = None
        self._status_at: float | None = None
        self._dynamic_since: float | None = None
        self.last_write: datetime | None = None
        self.last_mode: str | None = None
        self.last_error: str | None = None
        self.last_read_errors: dict[str, str] = {}
        # API checked against modbus-connection 4.10.0, not its newer docs.
        self.unit.set_message_spacing(0.06)

    @property
    def model(self) -> str | None:
        return MODEL_NAMES.get(self._model_id)

    @property
    def max_power_w(self) -> int:
        if self._model_id not in MODEL_POWER_W:
            raise UnsupportedDeviceError("Device identity has not been verified")
        return MODEL_POWER_W[self._model_id]

    async def _read(self, address: int, *, signed: bool = False) -> int:
        words = await self.unit.read_holding_registers(address, 2)
        raw = decode_u32(words)
        # SMA NaN sentinel: U32=FFFFFFFF, S32=80000000.
        if raw == (0x80000000 if signed else 0xFFFFFFFF):
            raise InvalidDataError(f"Register {address} reports unavailable data")
        return decode_s32(words) if signed else raw

    async def _read_status(self) -> int:
        # Invalidate the cache before I/O so failures cannot leave an old gate open.
        self._status = None
        self._status_at = None
        value = await self._read(33003)
        self._status, self._status_at = value, self._monotonic()
        return value

    async def _probe(self) -> dict[str, Any]:
        self.last_probe_registers = {}
        device_class = await self._read(30051)
        self.last_probe_registers["device_class"] = device_class
        model_id = await self._read(30053)
        self.last_probe_registers["model_id"] = model_id
        if device_class != 8009 or model_id not in MODEL_NAMES:
            self._model_id = None
            raise UnsupportedDeviceError("Device identity is not a supported SMA STP SE")
        status = await self._read_status()
        self.last_probe_registers["status"] = status
        self._model_id = model_id
        try:
            serial_number = await self._read(30057)
        except ModbusError, InvalidDataError, TimeoutError:
            serial_number = 0
        return {
            "inverter_status": status,
            "device_class": device_class,
            "model_id": model_id,
            "serial_number": str(serial_number) if serial_number else None,
            "model": MODEL_NAMES[model_id],
            "max_power_w": MODEL_POWER_W[model_id],
        }

    async def async_probe(self) -> dict[str, Any]:
        """Read identity and status only, including when writes are disabled."""
        async with self._lock:
            return await self._probe()

    async def async_read(self) -> dict[str, Any]:
        """Read hardware values, preserving unavailable readings as None.

        PV generation is the sum of both inverter DC inputs. AC power and the
        derived house consumption cover this inverter only; additional inverters
        require the coordinator's external source selection. Battery power is
        positive when charging, negative when discharging.
        """
        async with self._lock:
            if self._model_id is None:
                await self._probe()
            readings: dict[str, Any] = {}
            self.last_read_errors = {}
            self._status = self._status_at = None
            specs = (
                ("inverter_status", 33003, False, 1, 0, 0xFFFFFFFF - 1),
                ("soc", 30845, False, 1, 0, 100),
                ("battery_temp", 30849, True, 0.1, -60, 100),
                ("battery_capacity_kwh", 40187, False, 0.001, 0.001, 1000),
                ("battery_charge_w", 31393, False, 1, 0, 100000),
                ("battery_discharge_w", 31395, False, 1, 0, 100000),
                ("pv_power_w", 30775, True, 1, -100000, 100000),
                ("grid_import_w", 30865, True, 1, 0, 1000000),
                ("grid_export_w", 30867, True, 1, 0, 1000000),
                ("pv_dc_a_w", 30773, True, 1, 0, 100000),
                ("pv_dc_b_w", 30961, True, 1, 0, 100000),
            )
            readings.update({f"sensor.opti_{spec[0]}": None for spec in specs})
            for name, address, signed, scale, minimum, maximum in specs:
                key = f"sensor.opti_{name}"
                try:
                    raw = await self._read(address, signed=signed)
                    value = raw * scale
                    if not minimum <= value <= maximum:
                        raise InvalidDataError(f"Register {address} outside plausible range")
                    readings[key] = value
                    if name == "inverter_status":
                        self._status, self._status_at = raw, self._monotonic()
                except (ModbusConnectionError, TimeoutError) as err:
                    self.last_read_errors["transport"] = str(err)
                    break  # Do not hammer an unreachable device once per sensor.
                except (ModbusError, InvalidDataError) as err:
                    readings[key] = None
                    self.last_read_errors[key] = str(err)
            for name, sources, function in (
                (
                    "battery_power_w",
                    ("battery_charge_w", "battery_discharge_w"),
                    lambda charge, discharge: charge - discharge,
                ),
                ("pv_generation_w", ("pv_dc_a_w", "pv_dc_b_w"), lambda a, b: a + b),
                (
                    "house_balance_w",
                    ("pv_power_w", "grid_import_w", "grid_export_w"),
                    lambda ac, imported, exported: max(0, ac + imported - exported),
                ),
            ):
                values = [readings[f"sensor.opti_{source}"] for source in sources]
                readings[f"sensor.opti_{name}"] = (
                    function(*values) if all(value is not None for value in values) else None
                )
            return readings

    def _ensure_status(self) -> None:
        if (
            self._status not in self.allowed_statuses
            or self._status_at is None
            or self._monotonic() - self._status_at > STATUS_MAX_AGE_SECONDS
        ):
            raise InverterNotReadyError("Operating status unavailable, stale, or not permitted")

    def _ensure_current(self, is_current: Callable[[], bool]) -> None:
        if not is_current():
            raise StaleCommandError("Command or source data changed during the transaction")
        self._ensure_status()

    def _parameters(self, mode: str, parameters: Mapping[str, float]) -> dict[str, int]:
        numbers: dict[str, float] = {}
        for key, value in parameters.items():
            try:
                number = float(value)
            except (TypeError, ValueError) as err:
                raise InvalidDataError(f"Invalid parameter: {key}") from err
            if isinstance(value, bool) or not math.isfinite(number):
                raise InvalidDataError(f"Non-finite or boolean parameter: {key}")
            numbers[key] = number
        needed = {
            DYNAMIC: "charge_power_w",
            ONLY_CHARGE: "charge_power_w",
            GRID_CHARGE: "charge_power_w",
            FAST_CHARGE: "charge_setpoint_w",
            FAST_DISCHARGE: "discharge_setpoint_w",
            CHARGE_02C: "capacity_wh",
        }.get(mode)
        if needed is not None and needed not in numbers:
            raise InvalidDataError(f"Required parameter missing: {needed}")
        if mode == CHARGE_02C and numbers["capacity_wh"] <= 0:
            raise InvalidDataError("0.2C requires an available positive battery capacity")
        limit = self.max_power_w
        result = {
            key: min(limit, max(0, int(numbers.get(key, default))))
            for key, default in (
                ("charge_power_w", 0),
                ("charge_setpoint_w", 0),
                ("discharge_setpoint_w", 0),
                ("min_charge_w", 0),
                ("max_charge_w", limit),
                ("min_discharge_w", 0),
                ("max_discharge_w", limit),
            )
        }
        result["charge_power_w"] = min(result["charge_power_w"], result["max_charge_w"])
        result["charge_setpoint_w"] = min(result["charge_setpoint_w"], result["max_charge_w"])
        result["discharge_setpoint_w"] = min(
            result["discharge_setpoint_w"], result["max_discharge_w"]
        )
        result["power_02c_w"] = min(
            result["max_charge_w"], max(0, round(0.2 * numbers.get("capacity_wh", 0)))
        )
        return result

    async def _write(self, address: int, value: int) -> None:
        if self._read_only:
            self.blocked_write_attempts += 1
            raise PermissionError("Shadow device cannot write Modbus registers")
        await self.unit.write_registers(address, encode_s32(value))

    async def _bms(
        self,
        windows: tuple[int, int, int, int],
        opmod: int,
        guard: Callable[[], None],
        *,
        opmod_address: int = 41259,
    ) -> None:
        # A incomplete/late set is not a successful refresh. The caller cleans up.
        async with asyncio.timeout(BMS_TRANSACTION_SECONDS):
            registers = (
                *zip((40793, 40795, 40797, 40799), windows, strict=True),
                (40801, 0),
                (opmod_address, opmod),
            )
            for index, (address, value) in enumerate(registers):
                guard()
                await self._write(address, value)
                if index < len(registers) - 1:
                    await self._sleep(REGISTER_GAP_SECONDS)
            guard()

    async def _cleanup(self) -> None:
        """Best-effort conservative pause; status gate still applies to cleanup."""
        self._ensure_status()
        async with asyncio.timeout(12):
            await self._write(40151, 803)
            await self._sleep(REGISTER_GAP_SECONDS)
            await self._bms((0, 0, 0, 0), 303, self._ensure_status)
        self.last_mode = PAUSE
        self.last_write = datetime.now(UTC)
        self._dynamic_since = None

    async def _protected_cleanup(self) -> str | None:
        # Keep the serialization lock until cleanup has actually ended, even if
        # the coordinator cancels twice. Never leave a detached writer behind.
        task = asyncio.create_task(self._cleanup())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except BaseException as err:
            self.last_mode = None
            return f"Cleanup failed ({type(err).__name__}): {err}"
        return None

    async def async_apply(
        self,
        mode: str,
        parameters: dict[str, float],
        is_current: Callable[[], bool],
    ) -> None:
        """Apply a complete fresh command, or raise after attempted safe cleanup.

        Repeating an unchanged mode deliberately renews the full command. Callers
        must do so every 120s and invalidate is_current for *parameter/source*
        changes as well as mode changes. Disabling writes during a transaction
        invalidates the command but still permits its bounded safety cleanup.
        """
        if self._read_only:
            self.blocked_write_attempts += 1
            raise PermissionError("Shadow device cannot apply a battery mode")
        async with self._lock:
            touched = False
            try:
                if mode not in MODES:
                    raise InvalidDataError("Unknown adapter mode")
                if not is_current():
                    raise StaleCommandError("Command superseded before execution")
                if self._model_id is None:
                    await self._probe()
                else:
                    await self._read_status()
                self._ensure_current(is_current)
                p = self._parameters(mode, parameters)

                def guard() -> None:
                    self._ensure_current(is_current)

                if mode == DYNAMIC:
                    if self.last_mode != DYNAMIC or self._dynamic_since is None:
                        self._dynamic_since = self._monotonic()
                else:
                    self._dynamic_since = None

                # Unknown start state may still have an old 802 rail active.
                # CmpBMS writes during 802 are discarded: deactivate it FIRST.
                # AUTO has no explicit power setpoint: its current limits must
                # reach the BMS even on repeat or after another rail command.
                if mode in RAIL_MODES and (self.last_mode not in RAIL_MODES or mode == AUTO):
                    guard()
                    touched = True
                    await self._write(40151, 803)
                    await self._sleep(REGISTER_GAP_SECONDS)
                    await self._bms((0, p["max_charge_w"], 0, p["max_discharge_w"]), 1438, guard)
                    guard()
                if mode in RAIL_MODES:
                    guard()
                    touched = True  # A timed-out write may still have reached the device.
                    await self._write(40151, 802)
                    await self._sleep(COMMAND_DELAY_SECONDS)
                    guard()
                    if mode == AUTO:
                        await self._write(40151, 803)
                    else:
                        power = {
                            FAST_CHARGE: -p["charge_setpoint_w"],
                            FAST_DISCHARGE: p["discharge_setpoint_w"],
                            GRID_CHARGE: -p["charge_power_w"],
                            CHARGE_02C: -p["power_02c_w"],
                        }[mode]
                        await self._write(40149, power)
                    guard()
                else:
                    target = p["charge_power_w"]
                    if (
                        mode == DYNAMIC
                        and self._dynamic_since is not None
                        and self._monotonic() - self._dynamic_since < SETTLING_SECONDS
                    ):
                        target = min(target, SETTLING_CHARGE_W)
                    charge = power_window(
                        target, p["min_charge_w"], locked=mode in (PAUSE, ONLY_DISCHARGE)
                    )
                    discharge = power_window(
                        p["max_discharge_w"],
                        p["min_discharge_w"],
                        locked=mode in (PAUSE, ONLY_CHARGE),
                    )
                    guard()
                    touched = True
                    await self._write(40151, 803)
                    await self._sleep(REGISTER_GAP_SECONDS)
                    # Current tracked blueprint uses 40236 for Dynamic. Older
                    # prose says 41259. Preserve the implemented device sequence.
                    opmod = {PAUSE: 303, ONLY_CHARGE: 2289, ONLY_DISCHARGE: 2290, DYNAMIC: 1438}[
                        mode
                    ]
                    await self._bms(
                        (*charge, *discharge),
                        opmod,
                        guard,
                        opmod_address=40236 if mode == DYNAMIC else 41259,
                    )
                self.last_write = datetime.now(UTC)
                self.last_mode = mode
                self.last_error = None
            except BaseException as err:
                self.last_error = f"{type(err).__name__}: {err}"
                if touched:
                    cleanup_error = await self._protected_cleanup()
                    if cleanup_error:
                        if isinstance(err, StaleCommandError):
                            err.cleanup_failed = True
                        self.last_error += f"; {cleanup_error}"
                raise
