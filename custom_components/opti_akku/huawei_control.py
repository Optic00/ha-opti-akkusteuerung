"""Huawei LUNA2000 command semantics over existing HA entities and services.

No Modbus socket and no dependency on private Huawei Solar Python internals.
A service return is not a physical-effect guarantee. TOU has no independent
readback in this API; it is always installed behind zero charge/discharge
limits before activation. Pause is a restriction, not a return to autonomous
operation. Persistent Huawei settings do not have the forced-command lease.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import math
import time
from typing import Any

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .device import DeviceError, StaleCommandError, PendingCommandError
from .sma import (
    AUTO,
    DYNAMIC,
    PAUSE,
    ONLY_CHARGE,
    ONLY_DISCHARGE,
    GRID_CHARGE,
    FAST_CHARGE,
    FAST_DISCHARGE,
    CHARGE_02C,
    MODES,
)

CONTROL_DOMAINS = {
    "mode": "select",
    "excess_pv": "select",
    "grid_charge": "switch",
    "charge_limit": "number",
    "discharge_limit": "number",
    "grid_limit": "number",
    "cutoff_soc": "number",
    "forced_status": "sensor",
}
NUMBER_KEYS = {
    "charge_limit": "storage_maximum_charging_power",
    "discharge_limit": "storage_maximum_discharging_power",
    "grid_limit": "storage_power_of_charge_from_grid",
    "cutoff_soc": "storage_grid_charge_cutoff_state_of_charge",
}
MSC = "maximise_self_consumption"
TOU = "time_of_use_luna2000"
TOU_PERIODS = "00:00-23:59/1234567/+"
CHARGING = frozenset((GRID_CHARGE, FAST_CHARGE, CHARGE_02C))


class HuaweiControlError(DeviceError):
    """A command or its state confirmation failed."""


class HuaweiController:
    """Serialized, guarded transactions for one explicitly bound LUNA battery."""

    def __init__(
        self,
        device: Any,
        controls: dict[str, str],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            not isinstance(controls, dict)
            or set(controls) != set(CONTROL_DOMAINS)
            or any(not isinstance(v, str) or not v for v in controls.values())
            or len(set(controls.values())) != len(controls)
        ):
            raise HuaweiControlError("All Huawei actuator roles must be distinct entity IDs")
        self.device = device
        self.hass = device.hass
        self.controls = dict(controls)
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_mode: str | None = None
        self.last_write = None
        self._signature = None
        self._steps: list[tuple[str, Any]] = []
        self._pending = None
        self._touched = False
        self._expected: dict[str, Any] = {}
        self._refresh_requested = False
        self._cleanup_at: float | None = None
        self._cleanup_ok = False

    def validate(self, *, check_states: bool = True) -> str:
        """Recheck ownership, availability, units, capabilities before every call."""
        selected = self.device._validated_device()
        if not selected.model or not selected.model.startswith("SUN2000-"):
            raise HuaweiControlError("A SUN2000 inverter with LUNA2000 controls is required")
        registry = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        battery_id = None
        for role, entity_id in self.controls.items():
            entry = registry.async_get(entity_id)
            if (
                entry is None
                or entry.platform != "huawei_solar"
                or entry.config_entry_id != self.device.entry_id
                or entry.domain != CONTROL_DOMAINS[role]
                or entry.disabled_by is not None
            ):
                raise HuaweiControlError(f"Invalid actuator binding: {role}")
            target = devices.async_get(entry.device_id) if entry.device_id else None
            if target is None or not (
                target.id == selected.id or target.via_device_id == selected.id
            ):
                raise HuaweiControlError(f"Actuator is outside selected inverter: {role}")
            if role in NUMBER_KEYS:
                if target.id != selected.id or entry.translation_key != NUMBER_KEYS[role]:
                    raise HuaweiControlError(f"Wrong Huawei battery parameter: {role}")
            if role in ("mode", "excess_pv", "forced_status", "grid_charge"):
                if target.model != "Batteries" or target.via_device_id != selected.id:
                    raise HuaweiControlError("Battery controls must belong to the Batteries child")
                if battery_id is not None and battery_id != target.id:
                    raise HuaweiControlError("Battery controls belong to different batteries")
                battery_id = target.id
            if not check_states:
                continue
            state = self._state(role)
            if role in ("charge_limit", "discharge_limit", "grid_limit", "cutoff_soc"):
                expected = "%" if role == "cutoff_soc" else "W"
                if state.attributes.get("unit_of_measurement") != expected:
                    raise HuaweiControlError(f"Unsupported actuator unit: {role}")
                self._number(state.state)
                low = self._number(state.attributes.get("min"))
                high = self._number(state.attributes.get("max"))
                if low > high or low < 0:
                    raise HuaweiControlError(f"Invalid actuator range: {role}")
                if role != "cutoff_soc" and low != 0:
                    raise HuaweiControlError("Power controls must support a true zero limit")
            elif role == "mode" and not {MSC, TOU} <= set(state.attributes.get("options", [])):
                raise HuaweiControlError("LUNA2000 TOU and self-consumption modes are required")
            elif role == "excess_pv" and not {"charge", "fed_to_grid"} <= set(
                state.attributes.get("options", [])
            ):
                raise HuaweiControlError("Required excess-PV options are missing")
            elif role == "grid_charge" and state.state not in ("on", "off"):
                raise HuaweiControlError("Grid charge switch unavailable")
        for domain, service in (
            ()
            if not check_states
            else (
                ("number", "set_value"),
                ("select", "select_option"),
                ("switch", "turn_on"),
                ("switch", "turn_off"),
                ("huawei_solar", "set_tou_periods"),
                ("huawei_solar", "forcible_discharge"),
                ("huawei_solar", "stop_forcible_charge"),
                ("homeassistant", "update_entity"),
            )
        ):
            if not self.hass.services.has_service(domain, service):
                raise HuaweiControlError(f"Required action missing: {domain}.{service}")
        assert battery_id is not None
        return battery_id

    def _state(self, role: str) -> Any:
        state = self.hass.states.get(self.controls[role])
        if state is None or state.state in ("unknown", "unavailable", ""):
            raise HuaweiControlError(f"Actuator state unavailable: {role}")
        return state

    @staticmethod
    def _number(value: Any) -> float:
        if isinstance(value, bool):
            raise HuaweiControlError("Boolean command value")
        try:
            number = float(value)
        except (ValueError, TypeError, OverflowError) as err:
            raise HuaweiControlError("Non-numeric command value") from err
        if not math.isfinite(number):
            raise HuaweiControlError("Non-finite command value")
        return number

    def _target(self, role: str, value: float) -> float:
        state = self._state(role)
        low, high = (self._number(state.attributes.get(k)) for k in ("min", "max"))
        if not low <= value <= high:
            raise HuaweiControlError(f"Requested value outside hardware bounds: {role}")
        step = self._number(state.attributes.get("step", 1))
        if step <= 0:
            raise HuaweiControlError("Invalid actuator step")
        # Power ceilings round down, never upwards beyond a strategy limit.
        return round(low + math.floor((value - low + 1e-8) / step) * step, 6)

    @staticmethod
    def _guard(current: Callable[[], bool]) -> None:
        if not current():
            raise StaleCommandError("Huawei command superseded")

    async def _call(
        self, domain: str, service: str, data: dict, current: Callable[[], bool]
    ) -> None:
        self.validate(check_states=False)
        self._guard(current)
        async with asyncio.timeout(20):
            await self.hass.services.async_call(domain, service, data, blocking=True)
        self._guard(current)

    def _matches(self, role: str, value: str | float) -> bool:
        state = self._state(role)
        return (
            abs(self._number(state.state) - value) < 0.01
            if isinstance(value, (int, float))
            else state.state.lower() == value.lower()
        )

    async def _set(self, role: str, value: str | float, current: Callable[[], bool]) -> None:
        self._guard(current)
        self.validate(check_states=False)
        if self._matches(role, value):
            return
        domain = CONTROL_DOMAINS[role]
        data = {"entity_id": self.controls[role]}
        if domain == "number":
            value = self._target(role, float(value))
            service = "set_value"
            data["value"] = value
        elif domain == "select":
            service = "select_option"
            data["option"] = value
        else:
            service = "turn_on" if value == "on" else "turn_off"
        for _ in range(2):
            await self._call(domain, service, data, current)
            for _ in range(15):
                self._guard(current)
                if self._matches(role, value):
                    return
                await self._sleep(1)
            if self._matches(role, value):
                return
        raise HuaweiControlError(f"Readback did not confirm {role}")

    async def _force(
        self, service: str, extra: dict, expected: str, current: Callable[[], bool]
    ) -> None:
        # Even a pre-existing 'Stopped'/'Discharging' string is not evidence that
        # this call took effect. Require a newly reported configuration state.
        for _ in range(2):
            sent = dt_util.utcnow()
            await self._call(
                "huawei_solar",
                service,
                {"device_id": self.validate(check_states=False), **extra},
                current,
            )
            await self._call(
                "homeassistant",
                "update_entity",
                {"entity_id": self.controls["forced_status"]},
                current,
            )
            for _ in range(15):
                self._guard(current)
                state = self._state("forced_status")
                if state.state.lower() == expected and state.last_reported >= sent:
                    return
                await self._sleep(1)
        raise HuaweiControlError("Forced-command status not freshly confirmed")

    def _targets(self, mode: str, parameters: Mapping[str, float]) -> dict[str, float | str]:
        if mode not in MODES:
            raise HuaweiControlError("Unsupported Huawei mode")
        if mode == PAUSE:
            return {"charge_limit": 0.0, "discharge_limit": 0.0, "grid_limit": 0.0}

        def param(key: str) -> float:
            value = self._number(parameters.get(key))
            if value < 0:
                raise HuaweiControlError(f"Negative parameter: {key}")
            return value

        charge = min(
            param("max_charge_w"), self._number(self._state("charge_limit").attributes["max"])
        )
        discharge = min(
            param("max_discharge_w"), self._number(self._state("discharge_limit").attributes["max"])
        )
        if mode in (DYNAMIC, GRID_CHARGE):
            charge = min(charge, param("charge_power_w"))
        if mode == FAST_CHARGE:
            charge = min(charge, param("charge_setpoint_w"))
        if mode == CHARGE_02C:
            charge = min(charge, param("capacity_wh") * 0.2)
        if mode in (ONLY_DISCHARGE, FAST_DISCHARGE):
            charge = 0
        if mode in CHARGING or mode == ONLY_CHARGE:
            discharge = 0
        if mode == FAST_DISCHARGE:
            discharge = min(discharge, param("discharge_setpoint_w"))
        targets = {
            "charge_limit": self._target("charge_limit", charge),
            "discharge_limit": self._target("discharge_limit", discharge),
            "grid_limit": self._target(
                "grid_limit", min(charge, self._number(self._state("grid_limit").attributes["max"]))
            ),
        }
        if mode in CHARGING or (
            self.device.grid_charge_for_surplus and mode in (AUTO, DYNAMIC, ONLY_CHARGE)
        ):
            targets["cutoff_soc"] = self._target("cutoff_soc", param("max_soc"))
        return targets

    async def _pause(self, current: Callable[[], bool]) -> None:
        # Try every restriction even when another fails. Never restore positive
        # limits or change mode after an unconfirmed zero restriction.
        failures = []
        for role, target in (
            ("charge_limit", 0.0),
            ("discharge_limit", 0.0),
            ("grid_limit", 0.0),
            ("grid_charge", "off"),
        ):
            try:
                await self._set(role, target, current)
            except Exception as err:
                failures.append(err)
        try:
            await self._force("stop_forcible_charge", {}, "stopped", current)
        except Exception as err:
            failures.append(err)
        if not failures:
            await self._set("mode", MSC, current)
        if failures:
            raise HuaweiControlError("Huawei pause not fully confirmed") from failures[0]

    async def _cleanup(self) -> bool:
        if self._cleanup_at is not None and time.monotonic() - self._cleanup_at < 30:
            if not self._cleanup_ok:
                return False
            try:
                self.validate(check_states=False)
                if all(
                    self._matches(role, value)
                    for role, value in (
                        ("charge_limit", 0.0),
                        ("discharge_limit", 0.0),
                        ("grid_limit", 0.0),
                        ("grid_charge", "off"),
                        ("mode", MSC),
                        ("forced_status", "stopped"),
                    )
                ):
                    return True
            except HuaweiControlError:
                pass

        async def bounded():
            async with asyncio.timeout(80):
                await self._pause(lambda: True)

        task = asyncio.create_task(bounded())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except BaseException:
            self._cleanup_ok = False
        else:
            self._cleanup_ok = True
        self._cleanup_at = time.monotonic()
        return self._cleanup_ok

    def _plan(self, mode: str, targets: dict) -> list[tuple[str, Any]]:
        # Conservative transition entry. Reconciliation within a mode avoids
        # cycling both limits to zero unless a foreign mode/force is observed.
        expected_mode = TOU if mode in CHARGING else MSC
        transitioning = (
            self.last_mode != mode
            or self._state("mode").state != expected_mode
            or (mode != FAST_DISCHARGE and self._state("forced_status").state.lower() != "stopped")
        )
        steps = []
        if transitioning:
            steps += [
                ("charge_limit", 0.0),
                ("discharge_limit", 0.0),
                ("grid_limit", 0.0),
                ("grid_charge", "off"),
                ("force_stop", {}),
                ("mode", MSC),
            ]
        if mode == PAUSE:
            return steps or [
                ("charge_limit", 0.0),
                ("discharge_limit", 0.0),
                ("grid_limit", 0.0),
                ("grid_charge", "off"),
            ]
        for role in ("charge_limit", "discharge_limit", "grid_limit"):
            if not transitioning and self._number(self._state(role).state) > targets[role]:
                steps.append((role, targets[role]))
        if mode in CHARGING:
            if transitioning:
                steps.append(("tou", TOU_PERIODS))
            steps += [
                ("cutoff_soc", targets["cutoff_soc"]),
                ("excess_pv", "fed_to_grid"),
                ("mode", TOU),
                ("grid_charge", "on"),
            ]
        else:
            grid = (
                "on"
                if self.device.grid_charge_for_surplus and mode in (AUTO, DYNAMIC, ONLY_CHARGE)
                else "off"
            )
            steps += [("mode", MSC), ("excess_pv", "charge")]
            if grid == "on":
                steps.append(("cutoff_soc", targets["cutoff_soc"]))
            steps.append(("grid_charge", grid))
        steps += [
            (role, targets[role]) for role in ("charge_limit", "discharge_limit", "grid_limit")
        ]
        if mode == FAST_DISCHARGE:
            steps.append(
                ("force_discharge", {"power": int(targets["discharge_limit"]), "duration": 30})
                if targets["discharge_limit"] > 0
                else ("force_stop", {})
            )
        return steps

    def _confirmed(self, role: str, value: Any, sent: Any) -> bool:
        if role.startswith("force_"):
            state = self._state("forced_status")
            expected = "stopped" if role == "force_stop" else "discharging"
            return state.state.lower() == expected and state.last_reported >= sent
        return self._matches(role, value)

    async def _advance(self, role: str, value: Any, current: Callable[[], bool]) -> None:
        # Poll on coordinator ticks, never stretch the captured decision's
        # 30-second freshness window by sleeping for configuration readback.
        if self._pending:
            sent, at, attempts = self._pending
            if self._confirmed(role, value, sent):
                self._pending = None
                return
            if role.startswith("force_") and not self._refresh_requested:
                await self._call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": self.controls["forced_status"]},
                    current,
                )
                self._refresh_requested = True
                if self._confirmed(role, value, sent):
                    self._pending = None
                    return
                raise PendingCommandError("Awaiting forced-status refresh")
            if time.monotonic() - at < 15:
                raise PendingCommandError("Awaiting Huawei readback")
            if attempts >= 2:
                raise HuaweiControlError(f"Readback did not confirm {role}")
        else:
            attempts = 0
            if role in CONTROL_DOMAINS and self._matches(role, value):
                return
        if role in CONTROL_DOMAINS:
            domain = CONTROL_DOMAINS[role]
            data = {"entity_id": self.controls[role]}
            if domain == "number":
                value = self._target(role, float(value))
                service = "set_value"
                data["value"] = value
            elif domain == "select":
                service = "select_option"
                data["option"] = value
            else:
                service = "turn_on" if value == "on" else "turn_off"
        else:
            domain = "huawei_solar"
            service = {
                "tou": "set_tou_periods",
                "force_stop": "stop_forcible_charge",
                "force_discharge": "forcible_discharge",
            }[role]
            data = {
                "device_id": self.validate(),
                **({"periods": value} if role == "tou" else value),
            }
        sent = dt_util.utcnow()
        self._touched = True
        self._cleanup_at = None
        await self._call(domain, service, data, current)
        if role == "tou":
            # API limitation, explicitly documented: no independent table readback.
            return
        self._refresh_requested = False
        self._pending = (sent, time.monotonic(), attempts + 1)
        if self._confirmed(role, value, sent):
            self._pending = None
            return
        raise PendingCommandError("Awaiting Huawei readback")

    def _tighten_pending(self, targets: dict) -> None:
        """Keep progress under noisy power updates, never loosen an in-flight cap."""
        old = dict(self._signature[1:])
        safe = {key: min(value, targets[key]) for key, value in old.items()}
        prior_first = self._steps[0]
        self._steps = [
            (
                role,
                min(value, safe[role])
                if role in safe
                else (
                    {**value, "power": min(value["power"], int(safe["discharge_limit"]))}
                    if role == "force_discharge"
                    else value
                ),
            )
            for role, value in self._steps
        ]
        # Already enabled bounds must tighten before any remaining release.
        restrictions = []
        for role, value in safe.items():
            if role in self._expected and self._number(self._state(role).state) > value:
                restrictions.append((role, value))
                self._expected.pop(role, None)
        self._steps = restrictions + [("force_stop", {}) if role == "force_discharge" and value["power"] == 0 else (role, value) for role, value in self._steps]
        if self._steps[0] != prior_first:
            self._pending = None
        self._signature = (self._signature[0], *sorted(safe.items()))

    async def apply(
        self, mode: str, parameters: Mapping[str, float], current: Callable[[], bool]
    ) -> None:
        async with self._lock:
            phase_started = time.monotonic()
            try:
                self._guard(current)
                self.validate()
                targets = self._targets(mode, parameters)
                signature = (mode, *sorted(targets.items()))
                if self._steps and mode == self._signature[0] and signature != self._signature:
                    self._tighten_pending(targets)
                elif self._steps and signature != self._signature:
                    if not await self._cleanup():
                        raise HuaweiControlError("Superseded command cleanup failed")
                    self._steps = []
                    self._pending = None
                    self.last_mode = None
                if not self._steps:
                    self._signature = signature
                    self._expected = {}
                    self._steps = self._plan(mode, targets)
                while self._steps:
                    self._guard(current)
                    role, value = self._steps[0]
                    for checked_role, expected in self._expected.items():
                        if (
                            checked_role != role
                            and not (role.startswith("force_") and checked_role == "forced_status")
                            and not self._matches(checked_role, expected)
                        ):
                            raise HuaweiControlError(
                                "Confirmed restriction changed during transaction"
                            )
                    await self._advance(role, value, current)
                    if role in CONTROL_DOMAINS:
                        self._expected[role] = value
                    elif role.startswith("force_"):
                        self._expected["forced_status"] = (
                            "stopped" if role == "force_stop" else "discharging"
                        )
                    # Huawei may modify the grid switch when changing TOU.
                    # A later explicit switch step re-establishes its target.
                    if role in ("tou", "mode"):
                        self._expected.pop("grid_charge", None)
                    self._steps.pop(0)
                    if self._steps and time.monotonic() - phase_started >= 5:
                        raise PendingCommandError(
                            "Continue with fresh inputs on next coordinator tick"
                        )
                self._guard(current)
                if any(not self._matches(role, value) for role, value in self._expected.items()):
                    raise HuaweiControlError("Final Huawei target verification failed")
                self.last_mode = mode
                self.last_write = dt_util.utcnow()
                self.last_error = None
                self._touched = False
                if self._signature != signature:
                    pending = PendingCommandError("Conservative target confirmed; reconcile newer target next tick")
                    pending.confirmed = True
                    raise pending
            except PendingCommandError:
                raise
            except BaseException as err:
                self.last_mode = None
                clean = (
                    await self._cleanup() if self._touched or self.last_write is not None else True
                )
                self._steps = []
                self._pending = None
                self._touched = False
                self.last_error = (
                    f"{type(err).__name__}; cleanup {'confirmed' if clean else 'FAILED'}"
                )
                if isinstance(err, StaleCommandError):
                    err.cleanup_failed = not clean
                raise
