"""Own scheduling, persistence and the single writing controller per inverter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import json
from pathlib import Path
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers import sun
from homeassistant.util import dt as dt_util

from .alerts import HealthAlerts
from .arbitrage import build_arbitrage_estimate
from .command_evidence import build_command_evidence
from .device import PendingCommandError, DeviceAdapter, StaleCommandError
from .engine import Evaluation
from .const import (
    DOMAIN,
    MODES,
    NO_RELOAD_OPTION_KEYS,
    RECONCILE_SECONDS,
    SOURCE_DEFINITIONS,
    UPDATE_SECONDS,
)
from .definitions import NUMBER_DEFINITIONS, SWITCH_DEFINITIONS
from .sources import build_inputs, finite
from .plant import plant_entity_ids, plant_semantic_fingerprint
from .load_profile import LoadProfile
from .demand import DemandForecast
from .demand_comparison import build_strategy_comparison
from .peak_load import peak_load_profile
from .ev_preparation import EVPreparation, apply_preparation, command_signals
from .observation import SourceObservation
from .recovery import RecoveryState
from .shadow import ShadowRecorder
from .reporting import OperatingReport, reserve_plan
from .tibber_prices import REFRESH_SECONDS, RETRY_SECONDS, TibberPriceError, TibberPriceSnapshot, async_fetch_prices

_LOGGER = logging.getLogger(__name__)


def validate_setting(key: str, value: Any, current: dict) -> float | bool:
    """Validate individual values AND limits that must remain ordered."""
    if key in SWITCH_DEFINITIONS:
        if not isinstance(value, bool):
            raise HomeAssistantError("Schalter benötigen einen Wahrheitswert")
        return value
    definition = NUMBER_DEFINITIONS.get(key)
    number = finite(value)
    if definition is None or number is None or not definition["min"] <= number <= definition["max"]:
        raise HomeAssistantError("Ungültiger Einstellwert")
    updated = dict(current, **{key: number})
    pairs = [("minsoc", "maxsoc"), ("akkusteuerung_min_ladestaerke", "akkusteuerung_max_ladestaerke"),
             ("akkusteuerung_min_entladestaerke", "akkusteuerung_max_entladestaerke"),
             ("akkusteuerung_ueberschuss_veto_aus_grenze", "akkusteuerung_ueberschuss_veto_grenze")]
    for lower, upper in pairs:
        if float(updated[f"input_number.{lower}"]) > float(updated[f"input_number.{upper}"]):
            raise HomeAssistantError("Die Untergrenze darf nicht über der Obergrenze liegen")
    return number


class OptiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Refresh measurements and evaluate before every eligible write."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: DeviceAdapter, engine: Any):
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=timedelta(seconds=UPDATE_SECONDS))
        self.entry = entry
        self.connection_config = dict(entry.data)
        self.source_options = {
            key: value
            for key, value in entry.options.items()
            if key not in NO_RELOAD_OPTION_KEYS
        }
        self._writer_binding = [entry.data.get(k) for k in ("host", "port", "unit_id", "serial_number")]
        if entry.data.get("backend") == "huawei_solar":
            self._writer_binding += [entry.data.get(k) for k in (
                "huawei_entry_id", "huawei_device_id", "huawei_controls", "huawei_sources", "huawei_grid_surplus", "shadow_mode")]
        self._command_pending = False
        self._pause_pending = False
        self._pause_binding = None
        self._pause_retry_at = None
        self.device = device
        self.strategy_enabled = entry.options.get("strategy_enabled", True) is True
        self.engine = engine
        self._load_profile = LoadProfile()
        self._operating_report = OperatingReport()
        self._demand_forecast = DemandForecast()
        self._ev_preparation = EVPreparation()
        self._source_observation = SourceObservation()
        self._recovery = RecoveryState()
        self._history_import_running = False
        self._load_source_fingerprint = json.dumps({
            "plant": plant_semantic_fingerprint(entry.options),
            "legacy_house": entry.options.get("sources", {}).get("house_consumption") if entry.options.get("plant_mode", "legacy") == "legacy" else None,
            "single_inverter": entry.options.get("single_inverter", False),
            "device": self.connection_config,
        }, sort_keys=True)
        self._load_profile_status: dict = {}
        self.shadow_mode = entry.data.get("shadow_mode", entry.data.get("backend") == "huawei_solar") is True
        self._shadow = ShadowRecorder(Path(hass.config.path("opti_akku_shadow")))
        self._shadow_snapshot = self._shadow.snapshot()
        self.settings = {key: value["default"] for key, value in
                         {**NUMBER_DEFINITIONS, **SWITCH_DEFINITIONS}.items()}
        self.manual_mode: str | None = None
        self.write_enabled = False
        self._revision = 0
        self._stopping = False
        self._probed = False
        self._online = False
        self._identity: dict = {}
        self._last_signature: tuple | None = None
        self._last_apply: float | None = None
        self._write_monitor_since: float | None = None
        self._last_error: str | None = None
        self._unsubscribe_sources = None
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True)
        self._measurements: dict = {}
        self._violation_since: float | None = None
        self._violation_mode: str | None = None
        self._update_lock = asyncio.Lock()
        self._serial_owner: str | None = None
        self._stop_lock = asyncio.Lock()
        self._stopped = False
        self._engine_snapshot: dict = {}
        self._manual_charge_blocked = False
        self._manual_discharge_blocked = False
        self._manual_ev_latched = False
        self._manual_ev_clear_since: datetime | None = None
        self._settings_revision = None
        self._price_snapshot: TibberPriceSnapshot | None = None
        self._price_task: asyncio.Task | None = None
        self._price_next_fetch: datetime | None = None
        self._price_provider_error: str | None = None
        self._price_last_success: datetime | None = None
        self.alerts = HealthAlerts(hass, entry)
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_ha_stopping))
        entry.async_on_unload(self._release_device)

    async def async_restore(self) -> None:
        """Restore settings; every setup still requires fresh device/source reads."""
        stored = await self._store.async_load() or {}
        candidates = stored.get("settings", {})
        self._settings_revision = self.entry.options.get("settings_revision")
        if self._settings_revision and self._settings_revision != stored.get("settings_revision"):
            candidates = {**candidates, **self.entry.options.get("settings", {})}
        # Validate against the complete saved set so restoring ordered limits
        # doesn't depend on the order in which the settings were serialized.
        invalid = []
        for key, value in candidates.items():
            if key not in self.settings:
                continue
            definition = NUMBER_DEFINITIONS.get(key)
            number = finite(value)
            valid = (isinstance(value, bool) if key in SWITCH_DEFINITIONS else
                     number is not None and definition["min"] <= number <= definition["max"])
            if valid:
                self.settings[key] = value if key in SWITCH_DEFINITIONS else number
            else:
                invalid.append(key)
        for lower, upper in [("minsoc", "maxsoc"), ("akkusteuerung_min_ladestaerke", "akkusteuerung_max_ladestaerke"),
                             ("akkusteuerung_min_entladestaerke", "akkusteuerung_max_entladestaerke"),
                             ("akkusteuerung_ueberschuss_veto_aus_grenze", "akkusteuerung_ueberschuss_veto_grenze")]:
            low, high = f"input_number.{lower}", f"input_number.{upper}"
            if self.settings[low] > self.settings[high]:
                invalid.extend([low, high])
                for key in (low, high):
                    self.settings[key] = NUMBER_DEFINITIONS[key]["default"]
        if invalid:
            self._last_error = "Ungültige gespeicherte Einstellungen zurückgesetzt: " + ", ".join(sorted(set(invalid)))
            _LOGGER.warning(self._last_error)
        self.engine.restore(stored.get("engine", {}))
        self._demand_forecast.restore(stored.get("demand_forecast", {}))
        self._source_observation.restore(stored.get("source_observation", {}))
        if stored.get("load_source_fingerprint") == self._load_source_fingerprint:
            self._operating_report.restore(stored.get("operating_report", {}))
        previous_load_source = stored.get("load_source_fingerprint")
        if ((previous_load_source is not None and previous_load_source != self._load_source_fingerprint)
                or (previous_load_source is None and self.entry.options.get("plant_mode", "legacy") != "legacy")):
            self.engine.reset_load_statistics()
        self._load_profile.restore(stored.get("load_profile", {}), now=dt_util.utcnow(),
                                   fingerprint=json.dumps(plant_semantic_fingerprint(self.entry.options)))
        self._engine_snapshot = self.engine.snapshot()
        self.write_enabled = bool(stored.get("write_enabled", False)
                                  and self.entry.options.get("single_writer_confirmed", False)
                                  and stored.get("writer_binding") == self._writer_binding
                                  and stored.get("strategy_enabled", True) is self.strategy_enabled
                                  and not invalid
                                  and not self.shadow_mode
                                  and self.strategy_enabled and bool(self.supported_modes))
        if getattr(type(self.device), "PHASED", False) and stored.get("pause_pending"):
            self._pause_binding = stored.get("pause_binding", stored.get("writer_binding"))
            self._pause_pending = True
            if (self._pause_binding != self._writer_binding
                    or not self.entry.options.get("single_writer_confirmed", False)):
                self.write_enabled = False
                self._last_error = "Frühere Huawei-Pause unbestätigt; geänderte Bindung oder Freigabe verhindert automatische Nachholung. Gerät manuell prüfen."
        if self.shadow_mode:
            self._shadow.restore(stored.get("shadow", {}))
            self._shadow_snapshot = self._shadow.snapshot()
        # A forced manual charging/discharging command is never resumed after
        # a restart. Strategy decisions are recomputed from current inputs.
        self.manual_mode = None
        self._manual_charge_blocked = stored.get("manual_charge_blocked") is True
        self._manual_discharge_blocked = stored.get("manual_discharge_blocked") is True
        self._manual_ev_latched = stored.get("manual_ev_latched") is True
        if self.write_enabled and getattr(type(self.device), "PHASED", False):
            self._pause_pending = True
            self._pause_binding = self._writer_binding
            # Restored automatic control is a new write session too. Persist
            # its owed pause before setup can perform its first device call.
            await self._store.async_save(self._stored_data())

    def _claim_device(self, identity: dict) -> None:
        serial = identity.get("serial_number")
        expected = self.entry.data.get("serial_number")
        if expected and serial != expected:
            raise HomeAssistantError("Geräteidentität hat sich geändert; Verbindung neu konfigurieren")
        if self.shadow_mode:
            return
        if not serial:
            return
        owners = self.hass.data.setdefault(f"{DOMAIN}_serial_owners", {})
        if serial in owners and owners[serial] != self.entry.entry_id:
            raise HomeAssistantError("Dieser Wechselrichter hat bereits eine Opti-Steuerung")
        owners[serial] = self.entry.entry_id
        self._serial_owner = serial

    @callback
    def _release_device(self) -> None:
        owners = self.hass.data.get(f"{DOMAIN}_serial_owners", {})
        if owners.get(self._serial_owner) == self.entry.entry_id:
            owners.pop(self._serial_owner, None)
        self._serial_owner = None

    def _stored_data(self) -> dict:
        return {"settings": dict(self.settings), "settings_revision": self._settings_revision,
                "demand_forecast": self._demand_forecast.snapshot(),
                "source_observation": self._source_observation.snapshot(),
                "engine": self._engine_snapshot, "operating_report": self._operating_report.snapshot(), "load_profile": self._load_profile.snapshot(),
                "load_source_fingerprint": self._load_source_fingerprint,
                "write_enabled": self.write_enabled,
                "writer_binding": self._writer_binding,
                "pause_pending": self._pause_pending,
                "pause_binding": self._pause_binding,
                "strategy_enabled": self.strategy_enabled,
                "manual_charge_blocked": self._manual_charge_blocked,
                "manual_discharge_blocked": self._manual_discharge_blocked,
                "manual_ev_latched": self._manual_ev_latched,
                "shadow": self._shadow_snapshot}

    @callback
    def async_listen_sources(self) -> None:
        ids = list(dict.fromkeys([*filter(None, self.entry.options.get("sources", {}).values()), *plant_entity_ids(self.entry.options), *self.entry.data.get("huawei_sources", {}).values()]))
        if not ids:
            return

        @callback
        def changed(event) -> None:
            if self._stopping:
                return
            sources = self.entry.options.get("sources", {})
            keys = [key for key, entity_id in sources.items() if entity_id == event.data["entity_id"]]
            new_state = event.data.get("new_state")
            # A live power meter may report every second. Such observations
            # queue reevaluation without cancelling every multi-second write.
            # Invalid data, EV state, prices and forecasts invalidate at once.
            ordinary_power = all(
                (key in SOURCE_DEFINITIONS and SOURCE_DEFINITIONS[key][2] == "power")
                or key in ("ev1_power", "ev2_power") for key in keys
            )
            if not ordinary_power or new_state is None or finite(new_state.state) is None:
                self._revision += 1
            self.entry.async_create_background_task(self.hass, self.async_request_refresh(),
                                                   "Opti source refresh")

        self._unsubscribe_sources = async_track_state_change_event(self.hass, ids, changed)
        self.entry.async_on_unload(self._unsubscribe)

    @callback
    def _unsubscribe(self) -> None:
        if self._unsubscribe_sources is not None:
            unsubscribe, self._unsubscribe_sources = self._unsubscribe_sources, None
            unsubscribe()

    def _add_sun(self, states: dict, attrs: dict, now: datetime) -> None:
        state = self.hass.states.get("sun.sun")
        if state is not None:
            states["sun.sun"] = state.state
            attrs["sun.sun"] = dict(state.attributes)
            return
        # Use HA's location and timezone; users need no sun helper or mapping.
        try:
            sunrise = sun.get_astral_event_date(self.hass, "sunrise", now)
            sunset = sun.get_astral_event_date(self.hass, "sunset", now)
            above = sunrise is not None and sunset is not None and sunrise <= now <= sunset
            states["sun.sun"] = "above_horizon" if above else "below_horizon"
            attrs["sun.sun"] = {"next_setting": sun.get_astral_event_next(self.hass, "sunset").isoformat(),
                                "next_rising": sun.get_astral_event_next(self.hass, "sunrise").isoformat()}
        except (ValueError, TypeError):
            states["sun.sun"] = "unavailable"

    def _add_manual_ev_safety(self, states: dict, now: datetime) -> None:
        """Keep EV safety independent from strategy evaluation and its timers."""
        if not self.settings["input_boolean.opti_ev_akku_pause"]:
            self._manual_ev_latched = False
            self._manual_ev_clear_since = None
        else:
            source_map = self.entry.options.get("sources", {})
            configured = any(source_map.get(f"ev{i}_mode") and source_map.get(f"ev{i}_charging") for i in (1, 2))
            values = [states.get(f"binary_sensor.opti_ev_lp{i}_schnell") for i in (1, 2)]
            if not configured or any(value != "off" for value in values):
                self._manual_ev_latched = True
                self._manual_ev_clear_since = None
            elif self._manual_ev_latched:
                self._manual_ev_clear_since = self._manual_ev_clear_since or now
                if (now - self._manual_ev_clear_since).total_seconds() >= 300:
                    self._manual_ev_latched = False
                    self._manual_ev_clear_since = None
        states["binary_sensor.opti_ev_schnellladung"] = "on" if self._manual_ev_latched else "off"

    def _safe_mode(self, requested: str, states: dict) -> tuple[str, str | None]:
        soc = finite(states.get("sensor.opti_soc"))
        capacity = finite(states.get("sensor.opti_battery_capacity_kwh"))
        if not self._online or soc is None or not 0 <= soc <= 100 or capacity is None or capacity <= 0:
            return "Akku Pause", "Ungültige Batterie- oder Verbindungsdaten"
        if not self.settings["input_boolean.akku_opti_automatik"]:
            return "Akku Pause", "Steuerung deaktiviert"
        house = finite(states.get("sensor.opti_house_consumption_w"))
        if self.strategy_enabled and (house is None or house < 0):
            return "Akku Pause", "Gültigen Hausverbrauch auswählen oder Ein-Wechselrichter-Bilanz bestätigen"
        if finite(states.get("sensor.opti_battery_power_w")) is None:
            return "Akku Pause", "Batterieleistungsmessung fehlt"
        temperature = finite(states.get("sensor.opti_battery_temp"))
        if temperature is None or not -20 <= temperature <= 55:
            return "Akku Pause", "Batterietemperatur fehlt oder liegt außerhalb der Steuerungsgrenzen"
        # Resolve all manual direction restrictions together: an EV override
        # must not bypass an independent SoC or temperature restriction.
        if self.manual_mode:
            minimum = self.settings["input_number.minsoc"]
            maximum = self.settings["input_number.maxsoc"]
            hysteresis = min(3, (maximum - minimum) / 2)
            if soc >= maximum:
                self._manual_charge_blocked = True
            elif soc <= maximum - hysteresis:
                self._manual_charge_blocked = False
            if soc <= minimum:
                self._manual_discharge_blocked = True
            elif soc >= minimum + hysteresis:
                self._manual_discharge_blocked = False
            reasons = []
            charge_allowed = True
            discharge_allowed = True
            if temperature < 5 or temperature >= 50:
                charge_allowed = False
                reasons.append("Temperaturgrenze sperrt manuelle Ladung")
            if self._manual_charge_blocked:
                charge_allowed = False
                reasons.append("Max-SoC sperrt manuelle Ladung")
            if self._manual_discharge_blocked:
                discharge_allowed = False
                reasons.append("Min-SoC sperrt manuelle Entladung")
            ev_blocked = (self.settings["input_boolean.opti_ev_akku_pause"]
                          and states.get("binary_sensor.opti_ev_schnellladung") == "on")
            if ev_blocked:
                discharge_allowed = False
                reasons.append("EV-Ladung sperrt die Entladung des Hausakkus")

            effective = requested
            if requested in ("Akku Automatisch", "Akku Dynamisch"):
                if not charge_allowed and not discharge_allowed:
                    effective = "Akku Pause"
                elif not discharge_allowed:
                    effective = "Akku nur Laden"
                elif not charge_allowed:
                    effective = "Akku nur Entladen"
            elif requested in ("Akku schnell Entladen", "Akku nur Entladen") and not discharge_allowed:
                # During EV charging allow PV charging of the home battery,
                # provided its independent charge restrictions permit it.
                effective = "Akku nur Laden" if ev_blocked and charge_allowed else "Akku Pause"
            elif requested in ("Akku schnell Laden", "Akku Netzladen", "Akku nur Laden", "Akku 0.2C Laden") and not charge_allowed:
                effective = "Akku Pause"
            if effective != requested:
                return effective, "; ".join(reasons)
        return requested, None

    def _parameters(self, states: dict) -> dict[str, float]:
        def setting(suffix):
            return float(self.settings[f"input_number.{suffix}"])
        parameters = {"charge_power_w": (finite(states.get("sensor.opti_charge_power_w")) or 0) if self.strategy_enabled else min(setting("akkusteuerung_ladestaerke_soll"), setting("akkusteuerung_max_ladestaerke")),
                "charge_setpoint_w": setting("akkusteuerung_ladestaerke_soll"),
                "discharge_setpoint_w": setting("akkusteuerung_entladestaerke_soll"),
                "min_charge_w": setting("akkusteuerung_min_ladestaerke"),
                "max_charge_w": setting("akkusteuerung_max_ladestaerke"),
                "min_discharge_w": setting("akkusteuerung_min_entladestaerke"),
                "max_discharge_w": setting("akkusteuerung_max_entladestaerke"),
                "max_soc": setting("maxsoc"),
                "capacity_wh": (finite(states.get("sensor.opti_battery_capacity_kwh")) or 0) * 1000}
        temperature = finite(states.get("sensor.opti_battery_temp"))
        if self.manual_mode and temperature is not None and temperature >= 45:
            # Fixed presets and automatic SMA operation also obey the engine's
            # hot-battery limit; max_charge_w caps the driver's 0.2C preset.
            ceiling = min(parameters["max_charge_w"], parameters["charge_power_w"])
            if not self.strategy_enabled:
                ceiling = min(ceiling, parameters["max_charge_w"] * 0.5,
                              parameters["charge_setpoint_w"] * 0.5)
                parameters["charge_power_w"] = min(parameters["charge_power_w"], ceiling)
            parameters["max_charge_w"] = ceiling
            parameters["charge_setpoint_w"] = min(parameters["charge_setpoint_w"], ceiling)
        return parameters

    async def _async_update_data(self) -> dict[str, Any]:
        self._schedule_price_fetch()
        async with self._update_lock:
            data = await self._async_update_locked()
        self.alerts.update(data)
        data["notification_error"] = self.alerts.delivery_error
        return data

    @callback
    def _schedule_price_fetch(self) -> None:
        """Start provider I/O separately; never await the network under the device lock."""
        options = self.entry.options
        max_age = finite(options.get("price_max_age", 7200))
        if (self._stopping or not self.strategy_enabled or options.get("price_provider") != "tibber"
                or options.get("price_unit") != "EUR/kWh"
                or options.get("tibber_eur_confirmed") is not True
                or not options.get("tibber_home")
                or not options.get("tibber_entry_id")
                or max_age is None or max_age < 60
                or (self._price_task is not None and not self._price_task.done())
                or (self._price_next_fetch is not None and dt_util.utcnow() < self._price_next_fetch)):
            return
        self._price_task = self.entry.async_create_background_task(
            self.hass, self._async_fetch_tibber(), "Opti Tibber prices", eager_start=False)

    async def _async_fetch_tibber(self) -> None:
        """Retain transiently unavailable cached data only within its original TTL."""
        try:
            homes = await async_fetch_prices(self.hass)
            snapshot = homes.get(self.entry.options.get("tibber_home"))
            if snapshot is None:
                raise TibberPriceError("tibber_home_mismatch")
            if snapshot.entry_id != self.entry.options.get("tibber_entry_id"):
                raise TibberPriceError("tibber_entry_mismatch")
        except TibberPriceError as err:
            self._price_provider_error = err.code
            self._price_next_fetch = dt_util.utcnow() + timedelta(seconds=RETRY_SECONDS)
            if err.code in ("tibber_home_mismatch", "tibber_home_count", "tibber_config_entries", "tibber_entry_mismatch"):
                self._price_snapshot = None
                self._revision += 1
        else:
            self._price_snapshot = snapshot
            self._price_last_success = dt_util.utcnow()
            self._price_provider_error = None
            max_age = finite(self.entry.options.get("price_max_age", 7200)) or 60
            refresh = min(REFRESH_SECONDS, max(15, max_age / 2))
            self._price_next_fetch = dt_util.utcnow() + timedelta(seconds=refresh)
            self._revision += 1
        if not self._stopping:
            await self.async_request_refresh()

    def _current_price_snapshot(self) -> TibberPriceSnapshot | None:
        snapshot = self._price_snapshot
        if snapshot is not None:
            expected = self.entry.options.get("tibber_entry_id")
            if snapshot.entry_id != expected or [e.entry_id for e in self.hass.config_entries.async_entries("tibber")] != [expected]:
                return None
        return snapshot

    async def _async_update_locked(self) -> dict[str, Any]:
        if self._stopping:
            return self.data or {}
        revision = self._revision
        read_started = time.monotonic()
        battery_power_observed_at = None
        read_errors = {}
        try:
            async with asyncio.timeout(25):
                self._identity = await self.device.async_probe()
                self._claim_device(self._identity)
                self._probed = True
                self._measurements = await self.device.async_read()
                # SMA values originate in this Modbus transaction. Huawei's
                # adapter reads HA entity states whose device timestamp is not
                # exposed here; using the coordinator read time would make a
                # cached pre-command value look newer than the command.
                if (
                    self.entry.data.get("backend") != "huawei_solar"
                    and finite(self._measurements.get("sensor.opti_battery_power_w"))
                    is not None
                ):
                    battery_power_observed_at = dt_util.utcnow()
            read_errors = getattr(self.device, "last_read_errors", {})
            self._online = (finite(self._measurements.get("sensor.opti_inverter_status")) is not None
                            and not (isinstance(read_errors, dict) and "transport" in read_errors))
            if self.entry.data.get("backend") == "huawei_solar":
                from .huawei import REQUIRED_ROLES
                self._online = self._identity.get("backend_health") is True and not (read_errors.keys() & REQUIRED_ROLES)
        except Exception as err:  # Device/protocol failures must never retain valid telemetry.
            _LOGGER.debug("Opti measurement update failed: %s", type(err).__name__)
            read_errors = {"transport": type(err).__name__}
            self._online = False
            self._measurements = {key: "unavailable" for key in self._measurements}
            self._last_error = f"Lesefehler: {type(err).__name__}"
            self._last_signature = None
        if (self._pause_pending and not self.write_enabled and not self.shadow_mode and self._online
                and not self._stopping and self._pause_binding == self._writer_binding
                and self.entry.options.get("single_writer_confirmed", False)
                and (self._pause_retry_at is None or time.monotonic() - self._pause_retry_at >= 120)):
            await self._async_finish_huawei_pause()
        now = dt_util.utcnow()
        monotonic_now = time.monotonic()
        connection = {"status": "ready" if self._online else "offline", "write_ready":self._online}
        if self.entry.data.get("backend") != "huawei_solar":
            allowed = getattr(self.device, "allowed_statuses", (235, 2119))
            if not isinstance(allowed, (tuple, list, set, frozenset)):
                allowed = (235, 2119)
            connection = self._recovery.update(self._online,
                finite(self._measurements.get("sensor.opti_inverter_status")), monotonic_now, allowed)
        self._write_monitor_since = (self._write_monitor_since or monotonic_now) if self.write_enabled and not self.shadow_mode else None
        captured_options = dict(self.entry.options)
        input_options = dict(captured_options)
        if not self.strategy_enabled:
            input_options["price_provider"] = "entities"
            input_options["sources"] = {k: v for k, v in input_options.get("sources", {}).items()
                                        if not k.startswith(("price_", "forecast_"))}
        states, attributes, source_errors = build_inputs(self._measurements, input_options, self.hass.states, now,
                                                         price_snapshot=self._current_price_snapshot())
        if input_options.get("plant_mode", "legacy") != "legacy":
            profile = self._load_profile.observe(finite(states.get("sensor.opti_base_load_raw_w")), now,
                json.dumps(plant_semantic_fingerprint(input_options)),
                min_load_w=min(5000, max(0, finite(attributes.get("sensor.opti_house_raw_w", {}).get("forecast_min_load_w")) or 0)))
            self._load_profile_status = {"coverage_seconds": profile.coverage_seconds, "warming_up": profile.warming_up, "valid": profile.raw_mean_w is not None}
            for key in ("sensor.opti_house_consumption_60min_w", "sensor.haus_stromverbrauch_60_min"):
                states[key] = profile.forecast_w if profile.forecast_w is not None else "unavailable"
                attributes[key] = dict(self._load_profile_status)
            states["sensor.opti_load_profile_mean_w"] = profile.raw_mean_w if profile.raw_mean_w is not None else "unavailable"
        self._add_sun(states, attributes, now)
        states.update({key: ("on" if val else "off") if isinstance(val, bool) else val for key, val in self.settings.items()})
        states["input_select.akkusteuerung_modus"] = self.manual_mode or (self.data or {}).get("mode", "Akku Pause")
        try:
            peak_profile = peak_load_profile(
                self._demand_forecast, now,
                {"online": self._online, "states": states, "source_errors": source_errors,
                 "demand_forecast": (self.data or {}).get("demand_forecast", {})},
                self.settings, captured_options, self.hass.states,
                dt_util.DEFAULT_TIME_ZONE, self._load_source_fingerprint)
        except Exception:
            _LOGGER.exception("Peak load profile unavailable; using configured load")
            peak_profile = {"status": "fallback", "hours": {}, "methods": {}, "reason": "profile_error"}
        states["sensor.opti_peak_load_profile"] = peak_profile["status"]
        attributes["sensor.opti_peak_load_profile"] = peak_profile
        ev_cfg = captured_options.get("ev_preparation", {})
        ev_signals = command_signals(ev_cfg, self.hass.states, now)
        ev_preparation = self._ev_preparation.update(
            ev_cfg, self.hass.states, now, states, finite(self.settings.get("input_number.maxsoc")),
            self.strategy_enabled and self.manual_mode is None and self._online
            and self.settings.get("input_boolean.akku_opti_automatik") is True,
            can_prepare=not source_errors and states.get("sun.sun") == "above_horizon")
        previous_target_level = finite(
            self._engine_snapshot.get("attributes", {})
            .get("sensor.opti_target_soc", {})
            .get("level")
        )
        def evaluate():
            result = self.engine.evaluate(states, attributes, dt_util.as_local(now))
            return result, self.engine.snapshot()

        if self.strategy_enabled:
            result, self._engine_snapshot = await self.hass.async_add_executor_job(evaluate)
        else:
            self._add_manual_ev_safety(states, now)
            result = Evaluation(states, attributes, "Beobachtung", "Strategie deaktiviert", {})
        result, ev_report = apply_preparation(result, ev_preparation)
        for key, value in result.helper_updates.items():
            if key in self.settings and revision == self._revision:
                try:
                    self.settings[key] = validate_setting(key, value in (True, "on", "true") if key in SWITCH_DEFINITIONS else value, self.settings)
                except HomeAssistantError:
                    self._last_error = "Ungültige interne Einstellungsänderung"
        requested = self.manual_mode or result.mode
        if not self.strategy_enabled and self.manual_mode is None:
            mode, safety_reason = "Beobachtung", "Strategie deaktiviert; keine weiteren Steuerbefehle"
        else:
            mode, safety_reason = self._safe_mode(requested, result.states)
        reason = safety_reason or ("Manuelle Auswahl" if self.manual_mode else result.reason)
        params = self._parameters(result.states)
        if ev_cfg.get("enabled") is True and self.strategy_enabled and self.manual_mode is None:
            # This feature never imposes minimum export/discharge or minimum
            # charging that could pull power from the grid.
            params["min_discharge_w"] = 0
            if ev_report.get("status") == "preparing" and safety_reason is None:
                params["min_charge_w"] = 0
                params["charge_power_w"] = min(params["charge_power_w"], ev_report.get("surplus_before_battery_w", 0))
        signature = (mode, *sorted(params.items()))
        battery = finite(result.states.get("sensor.opti_battery_power_w"))
        violation = (not self.shadow_mode and self.write_enabled and battery is not None and self._last_apply is not None
                     and ((mode in ("Akku Pause", "Akku nur Laden", "Akku Netzladen") and battery < -200)
                          or (mode in ("Akku Pause", "Akku nur Entladen") and battery > 200)))
        if mode != self._violation_mode:
            self._violation_since = None
        self._violation_mode = mode
        self._violation_since = (self._violation_since or monotonic_now) if violation else None
        persistent_violation = self._violation_since is not None and monotonic_now - self._violation_since >= 30
        command_result = "not_attempted"
        write_failed = False
        last_write_before_command = getattr(self.device, "last_write", None)
        due = self._last_apply is None or monotonic_now - self._last_apply >= RECONCILE_SECONDS
        if (not self.shadow_mode and self.write_enabled and self._online and connection["write_ready"] and self._identity.get("serial_number") and not self._stopping
                and mode in self.supported_modes
                and (due or signature != self._last_signature or persistent_violation)):
            try:
                def current() -> bool:
                    if not self.write_enabled or self._stopping or self._revision != revision:
                        return False
                    if dict(self.entry.options) != captured_options:
                        return False
                    # Do not extend the validity of the captured measurements
                    # while waiting for a shared bus or a delayed write.
                    if time.monotonic() - read_started > 30:
                        return False
                    checked_at = dt_util.utcnow()
                    if ev_cfg.get("enabled") is True and command_signals(ev_cfg, self.hass.states, checked_at) != ev_signals:
                        return False
                    _, fresh_attributes, fresh_errors = build_inputs(self._measurements, input_options, self.hass.states, checked_at,
                                                                     price_snapshot=self._current_price_snapshot())
                    source_keys = set(input_options.get("sources", {}))
                    source_keys.update(f"plant:{key}" for key in plant_entity_ids(self.entry.options))
                    source_keys.update(key for key in source_errors if key.startswith("plant:"))
                    if any(key.startswith("plant:") for key in fresh_errors) and not any(key.startswith("plant:") for key in source_errors):
                        return False
                    if input_options.get("price_provider", "entities") != "entities":
                        source_keys.update(("price_current", "price_series"))
                        # Crossing a price interval invalidates the captured
                        # decision even if the cached next interval is valid.
                        if (attributes.get("sensor.opti_price_current_ct_kwh")
                                != fresh_attributes.get("sensor.opti_price_current_ct_kwh")):
                            return False
                    previously_valid = {key for key in source_keys if key not in source_errors}
                    return not (previously_valid & fresh_errors.keys())

                await self.device.async_apply(mode, params, current)
                command_result = "confirmed"
                self._command_pending = False
                self._last_signature = signature
                self._last_apply = time.monotonic()
                self._last_error = "Sperrverletzung beobachtet; Sollwerte erneuert" if persistent_violation else None
            except PendingCommandError as err:
                command_result = "safe_phase_confirmed" if err.confirmed else "pending"
                if err.confirmed:
                    self._last_apply = time.monotonic()
                self._command_pending = True
                self._last_signature = None
            except StaleCommandError as err:
                command_result = "failed" if err.cleanup_failed else "superseded"
                write_failed = bool(err.cleanup_failed)
                self._command_pending = False
                self._last_signature = None
                # Superseded decisions are expected when sources change during I/O.
                # Keep the original write age; repeated supersession still trips the watchdog.
                self._last_error = ("Schreibvorgang nicht bestätigt: Sicherheits-Pause fehlgeschlagen"
                                    if err.cleanup_failed else None)
            except Exception as err:
                command_result = "failed"
                write_failed = True
                self._command_pending = False
                self._last_signature = None
                self._last_error = f"Schreibvorgang nicht bestätigt: {type(err).__name__}"
        result.states["binary_sensor.opti_connection"] = "on" if self._online else "off"
        result.states["binary_sensor.opti_block_violation"] = "on" if persistent_violation else "off"
        result.states["binary_sensor.opti_write_stalled"] = "on" if not self.shadow_mode and self.write_enabled and self._write_monitor_since is not None and monotonic_now - (self._last_apply or self._write_monitor_since) > 240 else "off"
        metadata = {}
        resources = getattr(self.engine, "resources", {})
        for block in resources.get("template_blocks", []):
            for kind in ("sensor", "binary_sensor"):
                for entity in block.get(kind, []):
                    identity = entity.get("unique_id", "")
                    metadata[f"{kind}.{identity}"] = entity
        for key, (_canonical, label, family) in SOURCE_DEFINITIONS.items():
            label = {"pv_power": "Wechselrichterleistung AC", "price_series": "Strompreisreihe", "cell_spread": "Zellspreizung in Ruhe"}.get(key, label)
            metadata[_canonical] = {"name": label, "unit_of_measurement": {"power": "W", "energy": "kWh", "price": "ct/kWh", "voltage_spread": "mV"}[family], "state_class": "measurement"}
        for identity in result.states:
            if identity.startswith("sensor.opti_") and identity.endswith("_w"):
                metadata.setdefault(identity, {}).update(unit_of_measurement="W", device_class="power", state_class="measurement")
        metadata.update({
            "sensor.opti_soc": {"name": "Ladezustand", "unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement"},
            "sensor.opti_battery_temp": {"name": "Batterietemperatur", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
            "sensor.opti_battery_capacity_kwh": {"name": "Batteriekapazität", "unit_of_measurement": "kWh", "device_class": "energy_storage", "state_class": "measurement"},
            "binary_sensor.opti_connection": {"name": "Verbindung", "device_class": "connectivity"},
            "binary_sensor.opti_block_violation": {"name": "Sperrverletzung", "device_class": "problem"},
            "binary_sensor.opti_write_stalled": {"name": "Schreibstillstand", "device_class": "problem"},
        })
        command_evidence = build_command_evidence(
            self.device,
            command_result=command_result,
            write_enabled=self.write_enabled,
            shadow=self.shadow_mode,
            write_ready=connection["write_ready"],
            persistent_violation=persistent_violation,
            battery_power_w=battery,
            battery_power_observed_at=battery_power_observed_at,
            execution_completed_this_update=(
                command_result in {"confirmed", "safe_phase_confirmed"}
                or getattr(self.device, "last_write", None) != last_write_before_command
            ),
        )
        data = {"states": result.states, "attributes": result.attributes, "metadata": metadata,
                "mode": mode, "reason": reason, "engine_requested_mode": result.mode,
                "command_result_this_update": command_result, "write_enabled": self.write_enabled and not self.shadow_mode,
                "pause_pending": self._pause_pending and not self.write_enabled,
                "command_confirmation": "waiting_ready" if self.write_enabled and not connection["write_ready"] else "pending" if self._command_pending else "idle_or_confirmed",
                "command_evidence": command_evidence,
                "control_release": ("not_confirmed" if getattr(self.device, "supports_control_release", False) is True else "not_supported"),
                "connection_status": connection,
                "online": self._online, "last_write": getattr(self.device, "last_write", None),
                "last_error": self._last_error, "source_errors": source_errors,
                "device_errors": dict(read_errors) if isinstance(read_errors, dict) else {}, "load_profile": self._load_profile_status,
                "price_provider_error": self._price_provider_error,
                "price_last_success": self._price_last_success,
                "notification_error": self.alerts.delivery_error,
                "identity": self._identity, "manual_mode": self.manual_mode, "strategy_enabled": self.strategy_enabled}
        try:
            data["ev_preparation"] = {**ev_report, "controls_battery": bool(ev_report.get("controls_battery") and self.write_enabled and not self.shadow_mode and safety_reason is None), "observation_only": self.shadow_mode or not self.write_enabled}
            data["reserve_plan"] = reserve_plan(data, self.settings, now, shadow=self.shadow_mode)
            data["operating_report"] = self._operating_report.observe(now, data, data["reserve_plan"], write_failed=write_failed)
        except Exception as err:  # Reporting must never interrupt battery control.
            _LOGGER.debug("Operating report unavailable: %s", type(err).__name__)
            data["reserve_plan"] = {"status": "no_valid_plan"}
            data["operating_report"] = {"status": "error", "error_type": type(err).__name__}
        try:
            data["demand_forecast"] = self._demand_forecast.update(
                now, data, self.settings, captured_options, self.hass.states,
                dt_util.DEFAULT_TIME_ZONE, self._load_source_fingerprint)
        except Exception as err:  # Observation must never alter control or its health alerts.
            _LOGGER.debug("Demand observation unavailable: %s", type(err).__name__)
            data["demand_forecast"] = {"status": "error", "observation_only": True}
        demand_cfg = captured_options.get("demand_forecast", {})
        demand_comparison_enabled = (
            isinstance(demand_cfg, dict) and demand_cfg.get("enabled") is True
        )
        if self.shadow_mode or demand_comparison_enabled:
            try:
                demand_sources = (
                    demand_cfg.get("sources", {}) if isinstance(demand_cfg, dict) else {}
                )
                comparison_states = {
                    entity_id: self.hass.states.get(entity_id)
                    for entity_id in demand_sources.values()
                    if isinstance(entity_id, str)
                }

                def compare():
                    return build_strategy_comparison(
                        self._demand_forecast, now, data, self.settings, captured_options,
                        comparison_states, dt_util.DEFAULT_TIME_ZONE,
                        self._load_source_fingerprint, previous_target_level,
                    )

                comparison = await self.hass.async_add_executor_job(compare)
            except Exception as err:  # Comparison must remain isolated from control and reports.
                _LOGGER.debug("Demand strategy comparison unavailable: %s", type(err).__name__)
                comparison = {"status": "error", "observation_only": True}
            data["demand_forecast"] = {
                **data["demand_forecast"], "strategy_comparison": comparison
            }
        try:
            terminal_context = None
            if demand_comparison_enabled:
                demand_report = data.get("demand_forecast", {})
                terminal_context = {
                    "forecast_slots": demand_report.get("forecast_slots"),
                    "price_series": result.attributes.get("sensor.opti_price_series"),
                    "now": now,
                    "timezone": dt_util.DEFAULT_TIME_ZONE,
                    "refill_from": demand_report.get("pv_cover_from"),
                    "battery_capacity_kwh": finite(
                        result.states.get("sensor.opti_battery_capacity_kwh")
                    ),
                    "current_soc": finite(result.states.get("sensor.opti_soc")),
                    "minimum_soc": finite(self.settings.get("input_number.minsoc")),
                    "maximum_soc": finite(self.settings.get("input_number.maxsoc")),
                    "profile_ready": demand_report.get("profile_ready") is True,
                }
            data["arbitrage_estimate"] = build_arbitrage_estimate(
                captured_options.get("arbitrage_estimate"),
                finite(result.states.get("sensor.opti_price_current_ct_kwh")),
                strategy_enabled=self.strategy_enabled,
                terminal_context=terminal_context,
            )
        except Exception as err:  # A display-only estimate must never interrupt control.
            _LOGGER.debug("Arbitrage estimate unavailable: %s", type(err).__name__)
            data["arbitrage_estimate"] = {
                "status": "error", "informational_only": True, "controls_battery": False
            }
        try:
            probe = getattr(self.device, "last_probe_registers", {})
            data["source_observation"] = self._source_observation.update(
                now, self._measurements, self.hass.states, captured_options, self._online,
                source_errors, connection, probe if isinstance(probe, dict) else {})
        except Exception as err:
            _LOGGER.debug("Source observation unavailable: %s", type(err).__name__)
            data["source_observation"] = {"status":"error", "controls_battery":False}
        if self.shadow_mode:
            reference_id = self._shadow_snapshot.get("reference_entity", "")
            reference = self.hass.states.get(reference_id) if reference_id else None
            settings = dict(self.settings)
            try:
                self._shadow_snapshot = await self.hass.async_add_executor_job(
                    self._shadow.record, now, data, settings, reference.state if reference else None, MODES)
            except (OSError, ValueError):
                self._shadow.state["status"] = "error"
                self._shadow.state["error"] = "journal_write_failed"
                self._shadow_snapshot = self._shadow.snapshot()
            data["shadow_status"] = self._shadow_snapshot["status"]
            data["shadow_summary"] = {key: value for key, value in self._shadow_snapshot.items()
                                      if key not in ("settings", "reference_entity")}
            attempts = getattr(self.device, "blocked_write_attempts", 0)
            data["shadow_summary"]["blocked_write_attempts"] = attempts if isinstance(attempts, int) else 0
        self._store.async_delay_save(self._stored_data, 5)
        return data

    async def async_import_demand_history(self):
        """Manual import into observer only, never hold the writer lock over I/O."""
        from datetime import datetime
        from functools import partial
        from homeassistant.components.recorder import get_instance
        from .demand_history import HistoricalPrior, read_recorder
        cfg = self.entry.options.get("demand_forecast", {})
        if self._history_import_running or self._stopping or cfg.get("enabled") is not True:
            raise HomeAssistantError("Enable observation; wait for any existing import")
        if cfg.get("sources", {}).get("heat_power"):
            raise HomeAssistantError("Historical import currently requires a whole-house profile")
        house = cfg.get("history_house")
        if not house and self.entry.options.get("plant_mode", "legacy") == "legacy":
            house = self.entry.options.get("sources", {}).get("house_consumption")
        if not house:
            raise HomeAssistantError("Select the historical sensor with identical house-load scope")
        now = dt_util.utcnow()
        timezone = dt_util.DEFAULT_TIME_ZONE
        today = now.astimezone(timezone).date()
        first_online = min((key[:10] for key in self._demand_forecast.cells), default=today.isoformat())
        end_day = min(today, datetime.fromisoformat(first_online).date())
        start = datetime.combine(today - timedelta(days=42), datetime.min.time(), timezone).astimezone(dt_util.UTC)
        end = datetime.combine(end_day, datetime.min.time(), timezone).astimezone(dt_util.UTC)
        if start >= end:
            raise HomeAssistantError("No historical interval before online observations")
        binding = self._demand_forecast.history_binding(self._load_source_fingerprint, self.entry.options, timezone)
        sources = {**cfg.get("sources", {}), "house": house}
        self._history_import_running = True
        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                partial(read_recorder, self.hass, sources, start, end))
            async with self._update_lock:
                # Entry unload/options changes while Recorder runs invalidate the result.
                if self._stopping or binding != self._demand_forecast.history_binding(
                        self._load_source_fingerprint, self.entry.options, dt_util.DEFAULT_TIME_ZONE):
                    raise HomeAssistantError("Configuration changed during import")
                candidate = HistoricalPrior()
                candidate.replace(rows, binding, now)
                previous = self._demand_forecast.history
                self._demand_forecast.history = candidate
                try:
                    await self._store.async_save(self._stored_data())
                except Exception:
                    self._demand_forecast.history = previous
                    raise
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError("Historical import failed; no controller settings changed") from err
        finally:
            self._history_import_running = False
        # Publish diagnostics without scheduling an extra actuator update.
        if self.data is not None:
            self.async_set_updated_data({**self.data, "demand_forecast": {
                **self.data.get("demand_forecast", {}),
                "historical_prior": self._demand_forecast.history.summary()}})

    async def async_start_shadow(self) -> None:
        if not self.shadow_mode or not self._online or self._stopping:
            raise HomeAssistantError("Shadow-Aufzeichnung benötigt eine aktive lesende Verbindung")
        async with self._update_lock:
            now = dt_util.utcnow()
            settings = dict(self.settings)
            reference = self.entry.options.get("shadow_reference_mode", "")
            def start():
                version = json.loads(Path(__file__).with_name("manifest.json").read_text())["version"]
                return self._shadow.start(now, settings, reference, version)
            try:
                self._shadow_snapshot = await self.hass.async_add_executor_job(start)
            except (ValueError, OSError) as err:
                raise HomeAssistantError("Shadow-Aufzeichnung läuft bereits oder kann nicht angelegt werden") from err
            await self._store.async_save(self._stored_data())
        await self.async_refresh()

    async def async_stop_shadow(self) -> None:
        if not self.shadow_mode or self._stopping:
            raise HomeAssistantError("Keine aktive Shadow-Integration")
        async with self._update_lock:
            try:
                self._shadow_snapshot = await self.hass.async_add_executor_job(
                    self._shadow.stop, dt_util.utcnow())
            except OSError as err:
                raise HomeAssistantError("Shadow-Protokoll kann nicht abgeschlossen werden") from err
            await self._store.async_save(self._stored_data())
        await self.async_refresh()

    def _validate_ev_enable(self, settings: dict) -> None:
        if not settings.get("input_boolean.opti_ev_akku_pause"):
            return
        sources = self.entry.options.get("sources", {})
        if not any(sources.get(f"ev{n}_mode") and sources.get(f"ev{n}_charging") for n in (1, 2)):
            raise HomeAssistantError("Bitte zuerst einen evcc-Ladepunkt mit Modus und Ladestatus konfigurieren")

    async def async_set_setting(self, key: str, value: Any) -> None:
        async with self._update_lock:
            value = validate_setting(key, value, self.settings)
            if key == "input_boolean.opti_ev_akku_pause":
                self._validate_ev_enable({key: value})
            self.settings[key] = value
            self._revision += 1
            await self._store.async_save(self._stored_data())
        await self.async_refresh()

    async def async_apply_settings(self, settings: dict, revision: str) -> None:
        """Apply one validated wizard transaction without reloading the device."""
        async with self._update_lock:
            merged = {**self.settings, **settings}
            validated = {key: validate_setting(key, value, merged) for key, value in merged.items()}
            if "input_boolean.opti_ev_akku_pause" in settings:
                self._validate_ev_enable(validated)
            self.settings = validated
            self._settings_revision = revision
            self._revision += 1
            await self._store.async_save(self._stored_data())
        await self.async_refresh()

    @property
    def supported_modes(self) -> tuple[str, ...]:
        modes = getattr(self.device, "supported_modes", MODES)
        supported = tuple(modes) if isinstance(modes, (tuple, list)) else MODES
        # Dynamic and computed grid charging depend on a strategy power target.
        return supported if self.strategy_enabled else tuple(mode for mode in supported if mode not in ("Akku Dynamisch", "Akku Netzladen"))

    async def async_set_mode(self, mode: str) -> None:
        if mode == "Beobachtung" and not self.strategy_enabled:
            await self.async_set_write_enabled(False)
            self.manual_mode = None
            self._revision += 1
            await self.async_refresh()
            return
        if mode == "Strategie" and not self.strategy_enabled:
            raise HomeAssistantError("Strategie zuerst in den Optionen aktivieren")
        if mode != "Strategie" and mode not in self.supported_modes:
            raise HomeAssistantError("Unbekannter Akkumodus")
        self.manual_mode = None if mode == "Strategie" else mode
        self._revision += 1
        await self.async_refresh()

    async def async_set_write_enabled(self, enabled: bool) -> None:
        if enabled and self._pause_pending and self._pause_binding != self._writer_binding:
            raise HomeAssistantError("Unbestätigte Pause gehört zu einer anderen Bindung. Vor neuer Steuerung alten Gerätepfad wiederherstellen und Pause nachholen.")
        if enabled and not self.strategy_enabled and self.manual_mode is None:
            raise HomeAssistantError("Ohne Strategie zuerst einen manuellen Gerätemodus auswählen")
        if enabled and not self.supported_modes:
            raise HomeAssistantError("Dieses Gerätebackend unterstützt noch keine geprüften Schreibaktionen")
        if enabled and self.shadow_mode:
            raise HomeAssistantError("Shadow-Modus erlaubt grundsätzlich keine Schreibzugriffe")
        if enabled and not self.entry.options.get("single_writer_confirmed", False):
            raise HomeAssistantError("Bitte zuerst in den Optionen bestätigen, dass andere Akkusteuerungen deaktiviert sind")
        if enabled and (not self._online or self._stopping):
            raise HomeAssistantError("Wechselrichter ist nicht erreichbar")
        if enabled and not self._identity.get("serial_number"):
            raise HomeAssistantError("Ohne bestätigte Geräteidentität bleibt die Schreibfreigabe gesperrt")
        self._revision += 1
        if not enabled:
            was_enabled = self.write_enabled
            self.write_enabled = False
            self._command_pending = False
            if not self.shadow_mode and was_enabled and self._online and self._identity.get("serial_number"):
                # Explicitly stop our last command before giving up control.
                try:
                    if getattr(type(self.device), "PHASED", False):
                        await self._async_finish_huawei_pause()
                    else:
                        await self.device.async_apply("Akku Pause", self._parameters((self.data or {}).get("states", {})), lambda: not self.write_enabled)
                except Exception as err:
                    self._last_error = f"Pause beim Abschalten nicht bestätigt: {type(err).__name__}"
        else:
            self.write_enabled = True
            if getattr(type(self.device), "PHASED", False):
                # Saved before the first control call. Survives a crash or an
                # offline disable; only the original binding may finish it.
                self._pause_pending = True
                self._pause_binding = self._writer_binding
            self._last_signature = None
        await self._store.async_save(self._stored_data())
        await self.async_refresh()

    async def _async_finish_huawei_pause(self) -> None:
        if self._pause_binding != self._writer_binding:
            self._last_error = "Ausstehende Huawei-Pause gehört zu einer anderen Gerätebindung"
            return
        self._pause_retry_at = time.monotonic()
        try:
            await self.device.async_shutdown_control()
        except Exception as err:
            self._last_error = f"Huawei-Pause ausstehend: {type(err).__name__}; wird bei gleicher Bindung erneut versucht"
        else:
            self._pause_pending = False
            self._last_error = None
        await self._store.async_save(self._stored_data())

    async def async_stop(self) -> None:
        async with self._stop_lock:
            if not self._stopped:
                await self._async_stop_once()
                self._stopped = True

    async def _async_ha_stopping(self, event) -> None:
        await self.async_stop()

    async def _async_stop_once(self) -> None:
        self._stopping = True
        self._command_pending = False
        self._revision += 1
        self._unsubscribe()
        await self.alerts.async_stop()
        if self._price_task is not None and not self._price_task.done():
            self._price_task.cancel()
            try:
                await self._price_task
            except asyncio.CancelledError:
                pass
        await self.async_shutdown()
        async with self._update_lock:
            if not self.shadow_mode and self.write_enabled and self._online and self._identity.get("serial_number"):
                try:
                    if getattr(type(self.device), "PHASED", False):
                        await self._async_finish_huawei_pause()
                    else:
                        await self.device.async_apply("Akku Pause", self._parameters((self.data or {}).get("states", {})), lambda: True)
                except Exception as err:
                    self._last_error = f"Pause beim Entladen der Integration nicht bestätigt: {type(err).__name__}"
            await self._store.async_save(self._stored_data())
            self._release_device()
