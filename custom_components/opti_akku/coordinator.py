"""Own scheduling, persistence and the single writing controller per inverter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from contextlib import contextmanager
import math
from copy import deepcopy
from datetime import datetime, timedelta
import logging
import json
from pathlib import Path
import time
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import sun
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .alerts import HealthAlerts, night_standby
from .arbitrage import (
    PrechargeComparison,
    apply_discharge_hold,
    build_arbitrage_estimate,
    precharge_assumptions,
    precharge_spread_check,
)
from .battery_model import ENGINE_ENTITY as BATTERY_MODEL_ENTITY
from .battery_model import BatteryModel
from .charge_curve import HYSTERESIS_POINTS, ChargeCurveError, active_points, curve_limit, parse_curve
from .command_evidence import build_command_evidence
from .device import PendingCommandError, DeviceAdapter, StaleCommandError
from .engine import Evaluation
from .const import (
    CYCLE_FAILURE_CLEAR_SECONDS,
    CYCLE_FAILURE_GRACE_SECONDS,
    CYCLE_STALL_SECONDS,
    CYCLE_WATCHDOG_INTERVAL_SECONDS,
    DOMAIN,
    FAILSAFE_FAST_RETRIES,
    FAILSAFE_RETRY_SECONDS,
    MODES,
    NO_RELOAD_OPTION_KEYS,
    RECONCILE_SECONDS,
    SOURCE_DEFINITIONS,
    UPDATE_SECONDS,
    VIOLATION_MAX_RENEWALS,
    VIOLATION_RENEW_SECONDS,
    VIOLATION_SETTLE_SECONDS,
)
from .errors import translated
from .definitions import NUMBER_DEFINITIONS, SWITCH_DEFINITIONS
from .sources import build_inputs, finite
from .plant import plant_entity_ids, plant_semantic_fingerprint
from .load_profile import LoadProfile
from .clipping import clipping_forecast
from .source_quality import DEFAULT_FORECAST_MAX_AGE
from .negative_price_hold import NegativePriceHoldShadow, negative_price_hold_check
from .peak_rank import HOLD_DECISION as PEAK_RANK_HOLD_DECISION
from .peak_rank import PeakRankShadow, apply_peak_rank_hold, peak_rank_check
from .peak_rank import model_assumptions as peak_rank_model_assumptions
from .planner_shadow import ForecastHistory, PlannerInputs, PlannerShadow, price_periods, pv_forecast_hourly
from .demand import EV_SCOPE_VERSION, DemandForecast, pv_intervals, ev_profile_sources, ev_profile_scope, profile_house_value
from .refill_comparison import RefillReserveComparison, refill_reserve_check
from .evcc_comparison import FETCH_INTERVAL as EVCC_FETCH_INTERVAL, EvccComparison, derive_plan, evcc_check, fetch_evopt, suggestion_status
from .demand_comparison import build_strategy_comparison, remaining_day_profile
from .peak_load import peak_load_profile
from .ev_preparation import EVPreparation, apply_preparation, command_signals
from .observation import SourceObservation
from .recovery import RecoveryState
from .shadow import DEMAND_FIELDS, MEASUREMENTS, ShadowRecorder
from .reporting import OperatingReport, reserve_plan
from .tibber_prices import READINESS_RETRY_SECONDS, REFRESH_SECONDS, RETRY_SECONDS, TibberPriceError, TibberPriceSnapshot, async_fetch_prices

_LOGGER = logging.getLogger(__name__)
# HA reloads 30 s after the last enable; the margin covers a busy event loop.
ENTITY_ENABLE_RELOAD_WINDOW = 90.0
# Unload to setup of the same reload; older handovers are stale.
RELOAD_HANDOVER_MAX_AGE = 60.0
# Errors a later confirmed write proves resolved (device reachable, sequence
# accepted). Everything else, e.g. restore or settings warnings, is kept.
WRITE_ERROR_PREFIXES = (
    "Lesefehler",
    "Schreibvorgang nicht bestätigt",
    "Sperrverletzung",
    "Pause beim Abschalten nicht bestätigt",
)


def _percent(value: float, rounding: Callable[[float], float] = round) -> str:
    """Bounded SoC text for a state (at most 255 characters in total).

    Two decimals keep the text short. A release threshold passes ``math.floor``
    or ``math.ceil`` so the shown value never promises an earlier release.
    """
    # round(..., 6) first: 13.1 * 100 is 1310.0000000000002 in binary floats.
    return f"{rounding(round(value * 100, 6)) / 100:g}"


# Sources whose valid updates only add information. During a renewal of the
# already confirmed mode they do not cancel the running write sequence; the
# next evaluation uses them. Everything else invalidates at once.
SOFT_SOURCE_KEYS = frozenset(
    {"forecast_today", "forecast_tomorrow", "forecast_remaining", "price_series", "cell_spread"}
)

# Top-level fields ShadowRecorder.record reads from a coordinator result.
_JOURNAL_SAMPLE_KEYS = ("mode", "reason", "online", "source_errors", "write_enabled",
                        "strategy_enabled", "command_confirmation", "device_errors",
                        "price_status")


def _build_metadata(engine: Any) -> dict:
    """Static entity descriptions do not depend on the current decision."""
    metadata = {}
    resources = getattr(engine, "resources", {})
    for block in resources.get("template_blocks", []):
        for kind in ("sensor", "binary_sensor"):
            for entity in block.get(kind, []):
                identity = entity.get("unique_id", "")
                metadata[f"{kind}.{identity}"] = dict(entity)
    for key, (_canonical, label, family) in SOURCE_DEFINITIONS.items():
        label = {"pv_power": "Wechselrichterleistung AC", "price_series": "Strompreisreihe", "cell_spread": "Zellspreizung in Ruhe", "cell_voltage_min": "Niedrigste Zellspannung"}.get(key, label)
        metadata[_canonical] = {"name": label, "unit_of_measurement": {"power": "W", "energy": "kWh", "price": "ct/kWh", "voltage_spread": "mV", "voltage": "V"}[family], "state_class": "measurement"}
    metadata.update({
        "sensor.opti_soc": {"name": "Ladezustand", "unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement"},
        "sensor.opti_battery_temp": {"name": "Batterietemperatur", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
        "sensor.opti_battery_capacity_kwh": {"name": "Batteriekapazität", "unit_of_measurement": "kWh", "device_class": "energy_storage", "state_class": "measurement"},
        "sensor.opti_forecast_remaining_load_profile_kwh": {"name": "Verbrauchsprofil Resttag", "unit_of_measurement": "kWh", "state_class": "measurement"},
        "sensor.opti_clipping_forecast_kwh": {"name": "Erwartete Energie über AC-Grenze", "unit_of_measurement": "kWh", "state_class": "measurement"},
        "sensor.opti_negative_price_hold": {"name": "Schattenvergleich PV-Ladung bis Preisfenster"},
        "sensor.opti_peak_rank": {"name": "Schattenvergleich Peak-Rangfolge"},
        "sensor.opti_plan_shadow": {"name": "Schattenplaner Horizont"},
        "sensor.opti_evcc_comparison": {"name": "Schattenvergleich evcc-Optimizer"},
        BATTERY_MODEL_ENTITY: {"name": "Akkumodell (Wirkungsgrad und Zykluskosten)"},
        "binary_sensor.opti_connection": {"name": "Verbindung", "device_class": "connectivity"},
        "binary_sensor.opti_block_violation": {"name": "Sperrverletzung", "device_class": "problem"},
        "binary_sensor.opti_write_stalled": {"name": "Schreibstillstand", "device_class": "problem"},
    })
    return metadata


def _expected_load_hours(profile: Any) -> dict[str, float]:
    """Peak load profile without its switching margin: the planner wants the expectation."""
    if not isinstance(profile, dict) or not isinstance(profile.get("hours"), dict):
        return {}
    margin = 1 + (finite(profile.get("margin_percent")) or 0) / 100
    methods = profile.get("methods") if isinstance(profile.get("methods"), dict) else {}
    hours = {}
    for key, value in profile["hours"].items():
        number = finite(value)
        if number is None:
            continue
        hours[key] = number / margin if "profile" in str(methods.get(key, "")) else number
    return hours


def _option_number(cfg: dict, key: str, default: float) -> float:
    value = finite(cfg.get(key, default))
    return default if value is None else value


def _load_device_identity(connection: dict) -> dict:
    """Separate an identified SMA's measurement identity from its transport."""
    identity = dict(connection)
    serial = identity.get("serial_number")
    if (identity.get("backend", "sma") in ("sma", "sma_modbus")
            and isinstance(serial, str) and serial.strip()):
        for key in ("host", "port", "unit_id"):
            identity.pop(key, None)
        identity["backend"] = "sma_modbus"
        identity.setdefault("shadow_mode", False)
    return identity


def _canonical_load_fingerprint(value: object) -> str | None:
    """Accept legacy bindings only when every remaining source field matches."""
    if not isinstance(value, str):
        return None
    try:
        fields = json.loads(value)
        if not isinstance(fields, dict) or not isinstance(fields.get("device"), dict):
            return None
        fields["device"] = _load_device_identity(fields["device"])
        return json.dumps(fields, sort_keys=True, allow_nan=False)
    except ValueError:
        return None


def _score_load_binding(load_fingerprint: str, options: dict) -> str:
    return json.dumps([
        load_fingerprint, plant_semantic_fingerprint(options),
        options.get("sources", {}).get("house_consumption"),
        options.get("single_inverter", False), ev_profile_scope(options),
    ], sort_keys=True)


def _rebind_load_prefix(value: str | None, old: str, new: str, length: int) -> str | None:
    """Keep nested source/timezone checks intact when migrating the load key."""
    if not isinstance(value, str):
        return value
    try:
        parts = json.loads(value)
    except ValueError:
        return value
    compatible_length = (
        len(parts) == length
        or (len(parts) == length + 1 and isinstance(parts[-1], dict)
            and parts[-1].get("ev_subtraction_version") == EV_SCOPE_VERSION)
    ) if isinstance(parts, list) else False
    if compatible_length and parts[0] == old:
        return json.dumps([new, *parts[1:]], sort_keys=True)
    return value


def validate_setting(key: str, value: Any, current: dict) -> float | bool:
    """Validate individual values AND limits that must remain ordered."""
    if key in SWITCH_DEFINITIONS:
        if not isinstance(value, bool):
            raise translated(ServiceValidationError, "switch_value_invalid")
        return value
    definition = NUMBER_DEFINITIONS.get(key)
    number = finite(value)
    if definition is None or number is None or not definition["min"] <= number <= definition["max"]:
        raise translated(ServiceValidationError, "setting_invalid")
    updated = dict(current, **{key: number})
    pairs = [("minsoc", "maxsoc"), ("akkusteuerung_min_ladestaerke", "akkusteuerung_max_ladestaerke"),
             ("akkusteuerung_min_entladestaerke", "akkusteuerung_max_entladestaerke"),
             ("akkusteuerung_ueberschuss_veto_aus_grenze", "akkusteuerung_ueberschuss_veto_grenze")]
    if key == "input_number.opti_zellspannung_reserve_v" and number != 0 and not 2.9 <= number <= 3.1:
        raise translated(ServiceValidationError, "cell_voltage_reserve_invalid")
    for lower, upper in pairs:
        if float(updated[f"input_number.{lower}"]) > float(updated[f"input_number.{upper}"]):
            raise translated(ServiceValidationError, "limits_order_invalid")
    return number


PV_PEAK_DOWNSTREAM_DECISIONS = frozenset({"arbitrage_hold", "ev_preparation"})
# Forecast snapshots are coalesced; a crash loses at most this much history.
HISTORY_SAVE_DELAY = 900
# Routine state (latches, counters, reports) is written once per interval on
# the coordinator's own clock. HA's Store postpones a pending delayed write
# whenever async_delay_save is called again with a later deadline, so the
# write is requested with delay 0 and at most once per interval. Latch
# transitions that restore depends on are written immediately. A clean stop
# flushes as well; a crash loses at most one interval of non-latch state.
STORE_SAVE_INTERVAL = 60
# Derived latches whose on/off transition must survive a crash right away.
PERSISTED_LATCHES = ("binary_sensor.opti_minsoc_schutz_aktiv", "binary_sensor.opti_pv_spitze",
                     "binary_sensor.opti_ladedeckel_aktiv", "binary_sensor.opti_peak_horizont_lang")
# Latch attributes that restore reads back as previous state (#155).
PERSISTED_LATCH_ATTRIBUTES = (
    ("binary_sensor.opti_peak_horizont_lang", "an_quelle"),
    *(("binary_sensor.opti_minsoc_schutz_aktiv", name)
      for name in ("soc_aktiv", "zell_aktiv", "reserve_v", "ausloeser")),
)


def pv_peak_release_applies(result, mode: str, manual_mode, strategy_enabled: bool) -> bool:
    """True when a downstream override chose a PV charge mode under a PV peak.

    The engine only lifts the throttle when its own preview is a PV charge
    mode. Arbitrage hold and EV preparation may turn 'nur Entladen' into 'nur
    Laden' afterwards; ``binary_sensor.opti_pv_spitze`` (mode-independent
    latch, incl. temperature, valid measurements and SoC < 87 %) decides. Grid
    charging and manual modes never qualify.
    """
    if manual_mode is not None or not strategy_enabled or mode not in ("Akku nur Laden", "Akku Dynamisch"):
        return False
    if result.decision_id not in PV_PEAK_DOWNSTREAM_DECISIONS:
        return False
    attributes = result.attributes.get("sensor.opti_charge_power_w", {})
    return result.states.get("binary_sensor.opti_pv_spitze") == "on" and attributes.get("drossel_frei") is not True


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
        self._metadata = _build_metadata(engine)
        self._load_profile = LoadProfile()
        self._score_load_profile = LoadProfile()
        self._operating_report = OperatingReport()
        self._precharge_comparison = PrechargeComparison()
        self._refill_comparison = RefillReserveComparison()
        self._evcc_comparison = EvccComparison()
        self._negative_price_hold = NegativePriceHoldShadow()
        self._peak_rank = PeakRankShadow()
        # ((decision_id, price slot start), reason) of a set peak rank hold.
        # Memory only: after a restart the fresh check decides.
        self._peak_rank_latch: tuple[tuple[str, Any], str] | None = None
        self._planner = PlannerShadow()
        self._battery_model = BatteryModel.default()
        self._planner_history = ForecastHistory()
        self._demand_forecast = DemandForecast()
        self._ev_preparation = EVPreparation()
        self._source_observation = SourceObservation()
        self._recovery = RecoveryState()
        self._history_import_running = False
        self._pending_history = None
        self._load_source_fingerprint = json.dumps({
            "plant": plant_semantic_fingerprint(entry.options),
            "legacy_house": entry.options.get("sources", {}).get("house_consumption") if entry.options.get("plant_mode", "legacy") == "legacy" else None,
            "single_inverter": entry.options.get("single_inverter", False),
            "device": _load_device_identity(self.connection_config),
        }, sort_keys=True)
        self._score_load_fingerprint = _score_load_binding(self._load_source_fingerprint, entry.options)
        self._load_profile_status: dict = {}
        self.shadow_mode = entry.data.get("shadow_mode", entry.data.get("backend") == "huawei_solar") is True
        self._shadow = ShadowRecorder(Path(hass.config.path("opti_akku_shadow")))
        self._shadow_snapshot = self._shadow.snapshot()
        self.settings = {key: value["default"] for key, value in
                         {**NUMBER_DEFINITIONS, **SWITCH_DEFINITIONS}.items()}
        self.manual_mode: str | None = None
        self.write_enabled = False
        self._write_restore_blocked: str | None = None
        self._revision = 0
        self._soft_revision = 0
        self._stopping = False
        self._probed = False
        self._online = False
        self._identity: dict = {}
        self._last_signature: tuple | None = None
        self._last_apply: float | None = None
        self._write_monitor_since: float | None = None
        self._last_error: str | None = None
        self._unsubscribe_sources = None
        # Internal cycle failure incident (memory only, never persisted).
        self._cycle_fail_first: float | None = None
        self._cycle_fail_last: float | None = None
        self._cycle_fail_since: datetime | None = None
        self._cycle_fail_type: str | None = None
        self._failsafe_active = False
        self._failsafe_attempt_at: float | None = None
        self._failsafe_failed_attempts = 0
        self._cycle_finished_at: float | None = None
        self._watchdog_started_at: float | None = None
        self._unsubscribe_watchdog = None
        self._auxiliary_failures: set[str] = set()
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True)
        # Forecast snapshots grow to a few hundred kB; keep them out of the settings store.
        self._history_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.planner_history", private=True)
        self._measurements: dict = {}
        self._violation_since: float | None = None
        self._violation_mode: str | None = None
        self._violation_renewals = 0
        self._applied_mode: str | None = None
        self._applied_mode_since: float | None = None
        self._update_lock = asyncio.Lock()
        # Held only by owned journal jobs, never by a cancellable caller: an
        # executor thread still writing always keeps the journal serialized.
        self._journal_lock = asyncio.Lock()
        self._journal_jobs: set[asyncio.Task] = set()
        self._journal_owners: set[asyncio.Task] = set()
        self._journal_lifecycle: asyncio.Task | None = None
        self._serial_owner: str | None = None
        self._stop_lock = asyncio.Lock()
        self._stopped = False
        self._entity_enabled_at: float | None = None
        self._unload_pause_confirmed = False
        self._engine_snapshot: dict = {}
        self._routine_save_at: float | None = None
        self._write_stall_paused_at: float | None = None
        self._manual_charge_blocked = False
        self._manual_discharge_blocked = False
        # Charge curve points held by their hysteresis; in memory only. None
        # after start: the first cycle takes the stricter reading (#205).
        self._charge_curve_held: frozenset[int] | None = None
        self._manual_ev_latched = False
        self._manual_ev_clear_since: datetime | None = None
        self._manual_cell_latched = False
        self._manual_cell_reserve_v = 0.0
        self._manual_cell_low_since: datetime | None = None
        self._settings_revision = None
        self._price_snapshot: TibberPriceSnapshot | None = None
        self._price_task: asyncio.Task | None = None
        self._evcc_task: asyncio.Task | None = None
        self._evcc_url: str | None = None
        self._evcc_next_fetch: datetime | None = None
        self._evcc_evopt: Any = None
        self._evcc_fetch_error: str | None = None
        self._price_next_fetch: datetime | None = None
        self._price_provider_error: str | None = None
        self._price_last_success: datetime | None = None
        self.alerts = HealthAlerts(hass, entry)
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_ha_stopping))
        entry.async_on_unload(self._release_device)

    @callback
    def note_entity_enabled(self) -> None:
        """One of this entry's entities was enabled; HA reloads the entry soon."""
        self._entity_enabled_at = time.monotonic()

    @property
    def entity_enable_noted(self) -> bool:
        return (self._entity_enabled_at is not None
                and time.monotonic() - self._entity_enabled_at <= ENTITY_ENABLE_RELOAD_WINDOW)

    @property
    def pause_unconfirmed(self) -> bool:
        """An owed pause is open (Huawei), or unload ended active writes without a confirmed pause."""
        if getattr(type(self.device), "PHASED", False) and self._pause_pending:
            return True
        return self._stopping and self.write_enabled and not self.shadow_mode and not self._unload_pause_confirmed

    def reload_handover(self) -> dict | None:
        """Manual control to keep across the reload HA does after enabling an entity (#183).

        The caller decides whether the unload follows an entity enable."""
        if self.shadow_mode:
            return None
        return {"manual_mode": self.manual_mode,
                "manual_charge_blocked": self._manual_charge_blocked,
                "manual_discharge_blocked": self._manual_discharge_blocked,
                "minsoc": self.settings["input_number.minsoc"],
                "maxsoc": self.settings["input_number.maxsoc"],
                "write_enabled": self.write_enabled,
                "manual_charge_limit": self.settings["input_boolean.opti_manuelle_ladegrenze"],
                "connection": dict(self.connection_config), "sources": dict(self.source_options)}

    def _usable_handover(self, handover: dict | None) -> dict | None:
        if not isinstance(handover, dict):
            return None
        age = time.monotonic() - handover.get("at", float("-inf"))
        # Only for the same connection and sources; any real reconfiguration
        # keeps the restart contract (no forced mode resumes).
        if (not 0 <= age <= RELOAD_HANDOVER_MAX_AGE or handover.get("connection") != self.connection_config
                or handover.get("sources") != self.source_options):
            return None
        return handover

    async def async_restore(self, handover: dict | None = None) -> None:
        """Restore settings; every setup still requires fresh device/source reads."""
        handover = self._usable_handover(handover)
        handover_mode = handover.get("manual_mode") if handover else None
        if handover_mode not in self.supported_modes:
            handover_mode = None
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
        # This temporary override follows the legacy initial:false contract,
        # including reloads and settings supplied through the migration wizard.
        self.settings["input_boolean.opti_manuelle_ladegrenze"] = False
        self.engine.restore(stored.get("engine", {}))
        self._precharge_comparison.restore(stored.get("precharge_comparison"))
        self._refill_comparison.restore(stored.get("refill_reserve_comparison"))
        self._evcc_comparison.restore(stored.get("evcc_comparison"))
        self._negative_price_hold.restore(stored.get("negative_price_hold"))
        self._peak_rank.restore(stored.get("peak_rank"))
        self._planner.restore(stored.get("planner_shadow"))
        try:
            # Diagnosis only: a broken history must never block the integration.
            self._planner_history.restore(await self._history_store.async_load())
        except Exception as err:  # noqa: BLE001 - any store failure stays in the diagnosis
            _LOGGER.warning("Planner forecast history unavailable: %s", type(err).__name__)
            self._planner_history.restore(None)
            self._planner_history.error = type(err).__name__
        self._demand_forecast.restore(stored.get("demand_forecast", {}))
        self._source_observation.restore(stored.get("source_observation", {}))
        previous_load_source = stored.get("load_source_fingerprint")
        same_load_source = (
            _canonical_load_fingerprint(previous_load_source) == self._load_source_fingerprint
        )
        if same_load_source:
            self._operating_report.restore(stored.get("operating_report", {}))
            self._demand_forecast.fingerprint = _rebind_load_prefix(
                self._demand_forecast.fingerprint, previous_load_source,
                self._load_source_fingerprint, 3)
            self._demand_forecast.history.binding = _rebind_load_prefix(
                self._demand_forecast.history.binding, previous_load_source,
                self._load_source_fingerprint, 4)
        if ((previous_load_source is not None and not same_load_source)
                or (previous_load_source is None and self.entry.options.get("plant_mode", "legacy") != "legacy")):
            self.engine.reset_load_statistics()
        if stored.get("score_load_fingerprint") != self._score_load_fingerprint:
            self.engine.reset_score_statistics()
        self._load_profile.restore(stored.get("load_profile", {}), now=dt_util.utcnow(),
                                   fingerprint=json.dumps(plant_semantic_fingerprint(self.entry.options)))
        self._score_load_profile.restore(stored.get("score_load_profile", {}), now=dt_util.utcnow(),
                                         fingerprint=self._score_load_fingerprint)
        self._engine_snapshot = self.engine.snapshot()
        restore_blockers = [code for code, blocked in (
            ("single_writer_not_confirmed", not self.entry.options.get("single_writer_confirmed", False)),
            ("writer_binding_changed", stored.get("writer_binding") != self._writer_binding),
            ("strategy_setting_changed", stored.get("strategy_enabled", True) is not self.strategy_enabled),
            ("invalid_settings", bool(invalid)),
            ("shadow_mode", self.shadow_mode),
            # Without strategy, writes need a manual mode; only the entity
            # enable reload hands one over.
            ("strategy_disabled", not self.strategy_enabled and handover_mode is None),
            ("no_supported_modes", not self.supported_modes),
        ) if blocked]
        self.write_enabled = bool(stored.get("write_enabled", False)) and not restore_blockers
        if handover is not None and handover.get("write_enabled") is not True:
            # The live state at unload wins over a store that may lag behind,
            # e.g. a write disable whose save did not complete.
            self.write_enabled = False
        if stored.get("write_enabled") is True and restore_blockers:
            # Never silently lose control across a restart: name the reason.
            self._write_restore_blocked = restore_blockers[0]
            message = "Schreibfreigabe nach Neustart nicht wiederhergestellt: " + ", ".join(restore_blockers)
            _LOGGER.warning(message)
            # Keep an earlier, more specific restore error (e.g. the reset setting).
            self._last_error = f"{self._last_error}; {message}" if self._last_error else message
        if getattr(type(self.device), "PHASED", False) and stored.get("pause_pending"):
            self._pause_binding = stored.get("pause_binding", stored.get("writer_binding"))
            self._pause_pending = True
            if (self._pause_binding != self._writer_binding
                    or not self.entry.options.get("single_writer_confirmed", False)):
                self.write_enabled = False
                self._last_error = "Frühere Huawei-Pause unbestätigt; geänderte Bindung oder Freigabe verhindert automatische Nachholung. Gerät manuell prüfen."
        self._shadow.restore(stored.get("shadow", {}))
        self._shadow_snapshot = self._shadow.snapshot()
        # A forced manual charging/discharging command is never resumed after
        # a restart. Strategy decisions are recomputed from current inputs.
        # Exception: the reload HA performs after an entity was enabled (#183).
        self.manual_mode = handover_mode
        if handover is not None:
            self.settings["input_boolean.opti_manuelle_ladegrenze"] = handover.get("manual_charge_limit") is True
        if handover_mode is not None:
            _LOGGER.info("Manueller Modus %s nach Neuladen durch Aktivieren einer Entität beibehalten", handover_mode)
        # SoC blocks belong to one manual session; the first cycle after a
        # start derives them from the current SoC. Old stored keys are ignored.
        # A handed-over manual session keeps its blocks while its limits are
        # unchanged; a limit change resets them like a change while running.
        self._reset_manual_soc_blocks()
        if (handover_mode is not None and handover is not None
                and handover.get("minsoc") == self.settings["input_number.minsoc"]
                and handover.get("maxsoc") == self.settings["input_number.maxsoc"]):
            self._manual_charge_blocked = handover.get("manual_charge_blocked") is True
            self._manual_discharge_blocked = handover.get("manual_discharge_blocked") is True
        self._manual_ev_latched = stored.get("manual_ev_latched") is True
        self._manual_cell_latched = stored.get("manual_cell_latched") is True
        self._manual_cell_reserve_v = finite(stored.get("manual_cell_reserve_v")) or (
            self.settings["input_number.opti_zellspannung_reserve_v"] if self._manual_cell_latched else 0.0)
        # Only an established hold survives restart; a pending debounce re-arms.
        self._manual_cell_low_since = None
        if self.write_enabled and getattr(type(self.device), "PHASED", False):
            self._pause_pending = True
            self._pause_binding = self._writer_binding
            # Restored automatic control is a new write session too. Persist
            # its owed pause before setup can perform its first device call.
            await self._store.async_save(self._stored_data())

    def _clear_write_error(self) -> None:
        """A confirmed device write resolves only communication/write errors.

        Called only after a confirmed write; a superseded command clears
        nothing. Restore warnings and rejected settings stay visible until
        something else replaces them; they are not fixed by a successful write.
        """
        if self._last_error and self._last_error.startswith(WRITE_ERROR_PREFIXES):
            self._last_error = None

    def _claim_device(self, identity: dict) -> None:
        serial = identity.get("serial_number")
        expected = self.entry.data.get("serial_number")
        if expected and serial != expected:
            raise translated(HomeAssistantError, "device_identity_changed")
        if self.shadow_mode:
            return
        if not serial:
            return
        owners = self.hass.data.setdefault(f"{DOMAIN}_serial_owners", {})
        if serial in owners and owners[serial] != self.entry.entry_id:
            raise translated(HomeAssistantError, "inverter_already_controlled")
        owners[serial] = self.entry.entry_id
        self._serial_owner = serial

    @callback
    def _release_device(self) -> None:
        owners = self.hass.data.get(f"{DOMAIN}_serial_owners", {})
        if owners.get(self._serial_owner) == self.entry.entry_id:
            owners.pop(self._serial_owner, None)
        self._serial_owner = None

    def _stored_data(self) -> dict:
        demand = self._demand_forecast.snapshot()
        pending = self._pending_history
        if (pending is not None and not self._stopping
                and pending.binding == self._demand_forecast.history_binding(
                    self._load_source_fingerprint, self.entry.options, dt_util.DEFAULT_TIME_ZONE)):
            # Store coalesces concurrent/delayed saves. Every snapshot must carry
            # the staged import until its save finishes, without exposing it to control.
            demand["history"] = pending.snapshot()
        return {"settings": dict(self.settings), "settings_revision": self._settings_revision,
                "demand_forecast": demand,
                "source_observation": self._source_observation.snapshot(),
                "engine": self._engine_snapshot, "operating_report": self._operating_report.snapshot(),
                "precharge_comparison": self._precharge_comparison.snapshot(),
                "refill_reserve_comparison": self._refill_comparison.snapshot(),
                "evcc_comparison": self._evcc_comparison.snapshot(),
                "negative_price_hold": self._negative_price_hold.snapshot(),
                "peak_rank": self._peak_rank.snapshot(),
                "planner_shadow": self._planner.snapshot(), "load_profile": self._load_profile.snapshot(),
                "score_load_profile": self._score_load_profile.snapshot(),
                "load_source_fingerprint": self._load_source_fingerprint,
                "score_load_fingerprint": self._score_load_fingerprint,
                "write_enabled": self.write_enabled,
                "writer_binding": self._writer_binding,
                "pause_pending": self._pause_pending,
                "pause_binding": self._pause_binding,
                "strategy_enabled": self.strategy_enabled,
                "manual_ev_latched": self._manual_ev_latched,
                "manual_cell_latched": self._manual_cell_latched,
                "manual_cell_reserve_v": self._manual_cell_reserve_v,
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
            # A live power meter (or cell-voltage reading) may report every
            # second. Such observations queue reevaluation without cancelling
            # every multi-second write. Valid forecast, price series and cell
            # spread updates only count as information (SOFT_SOURCE_KEYS).
            # Invalid data, EV state and the current price invalidate at once.
            ordinary_power = all(
                (key in SOURCE_DEFINITIONS and SOURCE_DEFINITIONS[key][2] in ("power", "voltage"))
                or key in ("ev1_power", "ev2_power") for key in keys
            )
            if not ordinary_power or new_state is None or finite(new_state.state) is None:
                soft = (
                    bool(keys)
                    and all(key in SOFT_SOURCE_KEYS for key in keys)
                    and new_state is not None
                    and new_state.state not in ("unavailable", "unknown")
                    and all(key == "price_series" or finite(new_state.state) is not None
                            for key in keys)
                )
                if soft:
                    self._soft_revision += 1
                else:
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

    def _add_manual_cell_safety(self, states: dict, now: datetime) -> None:
        """Mirror the cell debounce/hold without evaluating strategy timers.

        build_inputs has already rejected invalid and stale source readings.
        Keep this hold separate from the manual SoC hysteresis.
        """
        reserve = round(self.settings["input_number.opti_zellspannung_reserve_v"], 3)
        cell = finite(states.get("sensor.opti_cell_voltage_min_v"))
        cell = round(cell, 3) if cell is not None else None
        if reserve > 0 and cell is not None and cell <= reserve:
            if self._manual_cell_low_since is None:
                self._manual_cell_low_since = now
            debounced = (now - self._manual_cell_low_since).total_seconds() >= 60
        else:
            self._manual_cell_low_since = None
            debounced = False
        held = (self._manual_cell_latched and 0 < self._manual_cell_reserve_v <= reserve
                and (cell is None or cell < round(reserve + 0.20, 2)))
        self._manual_cell_latched = reserve > 0 and (debounced or held)
        self._manual_cell_reserve_v = reserve

    def _reset_manual_soc_blocks(self) -> None:
        """End the manual SoC hysteresis; the next cycle derives it from the SoC."""
        self._manual_charge_blocked = False
        self._manual_discharge_blocked = False

    def _manual_limits(self) -> dict[str, Any]:
        minimum = self.settings["input_number.minsoc"]
        maximum = self.settings["input_number.maxsoc"]
        return {"minsoc": float(minimum), "maxsoc": float(maximum),
                "hysteresis": float(min(3, (maximum - minimum) / 2)),
                "charge_blocked": self._manual_charge_blocked,
                "discharge_blocked": self._manual_discharge_blocked}

    def _safe_mode(self, requested: str, states: dict, attributes: dict | None = None) -> tuple[str, str | None]:
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
                reasons.append(
                    f"Max-SoC {_percent(maximum)} % sperrt Ladung (frei ab {_percent(maximum - hysteresis, math.floor)} %)"
                )
            if self._manual_discharge_blocked:
                discharge_allowed = False
                reasons.append(
                    f"Min-SoC {_percent(minimum)} % sperrt Entladung (frei ab {_percent(minimum + hysteresis, math.ceil)} %)"
                )
            # Unknown engine state/origin cannot prove cell discharge is safe.
            # Without strategy evaluation use the independent manual cell hold.
            cell_hold = (attributes or {}).get("binary_sensor.opti_minsoc_schutz_aktiv", {})
            cell_state = states.get("binary_sensor.opti_minsoc_schutz_aktiv")
            cell_origin = cell_hold.get("ausloeser") if isinstance(cell_hold, dict) else None
            cell_blocked = (cell_state not in ("on", "off")
                            or (cell_state == "on" and cell_origin != "soc")) if self.strategy_enabled else self._manual_cell_latched
            if (self.settings["input_number.opti_zellspannung_reserve_v"] > 0
                    and cell_blocked):
                discharge_allowed = False
                reasons.append("Zellspannungs-Reserve sperrt manuelle Entladung")
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
        if (curve := self._charge_curve_limit(states)) is not None:
            # Last step, minimum only: in every mode the curve can lower these
            # values, never raise them. Later steps in the update only lower
            # them further or raise charge_power_w up to max_charge_w (PV
            # peak), which is capped here too. The floor follows the ceiling
            # so a small curve power stays a small window, not a closed one.
            for key in ("charge_power_w", "charge_setpoint_w", "max_charge_w", "min_charge_w"):
                parameters[key] = min(parameters[key], curve[0])
        return parameters

    def _parse_charge_curve(self) -> tuple[tuple[int, int], ...] | None:
        """The stored curve, or None when it cannot be read."""
        try:
            return parse_curve(self.entry.options.get("charge_curve"))
        except (ChargeCurveError, TypeError, ValueError, OverflowError):
            return None

    def _charge_curve_limit(self, states: Mapping[str, Any]) -> tuple[float, str] | None:
        """Ceiling and readable note of the optional charge curve (#205).

        Reads the held points without changing them. Invalid stored points
        block charging (fail-closed) instead of being ignored; the options
        flow only stores validated curves.
        """
        curve = self._parse_charge_curve()
        if curve is None:
            return 0.0, "Ladekurve ungültig: Laden gesperrt"
        soc = finite(states.get("sensor.opti_soc"))
        limit = curve_limit(curve, soc, self._charge_curve_held)
        if limit is None:
            return None
        power, start = limit
        if start is None:
            return float(power), "Ladekurve: Ladestand unbekannt, kleinste Kurvenleistung"
        # Stable per binding point: the text must not flap with the SoC
        # inside the band, it lands in recorded attributes (#129).
        note = f"Ladekurve ab {start} % SoC: Obergrenze aktiv"
        if start - HYSTERESIS_POINTS >= 0:
            note += f" (frei ab {start - HYSTERESIS_POINTS} %)"
        return float(power), note

    def _update_charge_curve_hold(self, states: Mapping[str, Any]) -> None:
        """Advance the curve hysteresis once per control cycle.

        An unknown SoC or unreadable curve forgets the hold, so the next
        valid SoC starts from the stricter reading again.
        """
        curve = self._parse_charge_curve()
        soc = finite(states.get("sensor.opti_soc"))
        if not curve or soc is None:
            self._charge_curve_held = None
            return
        self._charge_curve_held = active_points(curve, soc, self._charge_curve_held)

    async def _async_update_data(self) -> dict[str, Any]:
        self._schedule_price_fetch()
        async with self._update_lock:
            data = await self._async_update_locked()
        # Alerting and the journal must never fail the control cycle.
        with self._auxiliary_guard("alerts"):
            self.alerts.update(data)
        data["notification_error"] = self.alerts.delivery_error
        # Never await journal I/O here: HA serializes this whole method.
        with self._auxiliary_guard("journal"):
            self._schedule_journal_record(data, dt_util.utcnow())
        with self._auxiliary_guard("journal_status"):
            data.update(self._journal_status())
        return data

    @contextmanager
    def _auxiliary_guard(self, name: str):
        """Log a failing auxiliary step once per incident and continue."""
        try:
            yield
        except Exception:
            if name not in self._auxiliary_failures:
                self._auxiliary_failures.add(name)
                _LOGGER.exception("Opti %s step failed; control continues", name)
        else:
            self._auxiliary_failures.discard(name)

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
        """Retain transiently unavailable cached data while its dated intervals and hard age cap allow."""
        try:
            homes = await async_fetch_prices(self.hass)
            snapshot = homes.get(self.entry.options.get("tibber_home"))
            if snapshot is None:
                raise TibberPriceError("tibber_home_mismatch")
            if snapshot.entry_id != self.entry.options.get("tibber_entry_id"):
                raise TibberPriceError("tibber_entry_mismatch")
        except TibberPriceError as err:
            self._price_provider_error = err.code
            retry = READINESS_RETRY_SECONDS if err.code == "tibber_not_ready" else RETRY_SECONDS
            self._price_next_fetch = dt_util.utcnow() + timedelta(seconds=retry)
            if err.code == "tibber_not_ready":
                # The regular tick checks readiness; do not double device polling.
                return
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
            self._soft_revision += 1
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
        soft_revision = self._soft_revision
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
            self._applied_mode = None
        if (self._pause_pending and not self.write_enabled and not self.shadow_mode and self._online
                and not self._stopping and self._pause_binding == self._writer_binding
                and self.entry.options.get("single_writer_confirmed", False)
                and (self._pause_retry_at is None or time.monotonic() - self._pause_retry_at >= 120)):
            await self._async_finish_huawei_pause()
        now = dt_util.utcnow()
        monotonic_now = time.monotonic()
        connection: dict[str, Any] = {"status": "ready" if self._online else "offline", "write_ready":self._online}
        if self.entry.data.get("backend") != "huawei_solar":
            allowed = getattr(self.device, "allowed_statuses", (235, 2119))
            if not isinstance(allowed, (tuple, list, set, frozenset)):
                allowed = (235, 2119)
            raw_status = finite(self._measurements.get("sensor.opti_inverter_status"))
            # Raw device code explains not_ready incidents, e.g. night standby.
            connection = {**self._recovery.update(self._online, raw_status, monotonic_now, allowed),
                          "inverter_status": int(raw_status) if raw_status is not None else None}
        self._write_monitor_since = (self._write_monitor_since or monotonic_now) if self.write_enabled and not self.shadow_mode else None
        try:
            data = await self._async_cycle_body(
                revision, soft_revision, read_started, battery_power_observed_at, read_errors,
                now, monotonic_now, connection,
            )
        except Exception as err:  # Not BaseException: cancellation stays cancellation.
            data = await self._async_cycle_failed(err, now, monotonic_now, connection, read_errors)
        else:
            self._cycle_succeeded(monotonic_now)
        return data

    async def _async_cycle_body(self, revision, soft_revision, read_started,
                                battery_power_observed_at, read_errors, now, monotonic_now,
                                connection) -> dict[str, Any]:
        """Evaluate, write and report one cycle after the device read."""
        captured_options = dict(self.entry.options)
        score_binding = _score_load_binding(self._load_source_fingerprint, captured_options)
        if score_binding != self._score_load_fingerprint:
            self.engine.reset_score_statistics()
            self._score_load_profile = LoadProfile()
            self._score_load_fingerprint = score_binding
        input_options = dict(captured_options)
        if not self.strategy_enabled:
            input_options["price_provider"] = "entities"
            input_options["sources"] = {k: v for k, v in input_options.get("sources", {}).items()
                                        if not k.startswith(("price_", "forecast_"))}
        states, attributes, source_errors = build_inputs(self._measurements, input_options, self.hass.states, now,
                                                         price_snapshot=self._current_price_snapshot())
        score_opt_in = captured_options.get("demand_forecast", {}).get("house_includes_ev") is True
        score_load_error = None
        states["input_boolean.opti_score_house_includes_ev"] = "on" if score_opt_in else "off"
        if score_opt_in:
            nonlegacy = captured_options.get("plant_mode", "legacy") != "legacy"
            states["sensor.opti_forecast_house_min_load_w"] = (
                min(5000, max(0, finite(attributes.get("sensor.opti_house_raw_w", {}).get("forecast_min_load_w")) or 0))
                if nonlegacy else 0
            )
            value, score_load_error = profile_house_value(
                {"online": self._online, "states": states, "source_errors": source_errors},
                captured_options, self.hass.states, now,
                house_key="sensor.opti_base_load_raw_w" if nonlegacy else "sensor.opti_house_consumption_w",
                excluded_ev_sources=captured_options.get("excluded_load_sources", ()) if nonlegacy else (),
            )
            states["sensor.opti_forecast_house_instant_w"] = value if value is not None else "unavailable"
        if input_options.get("plant_mode", "legacy") != "legacy":
            profile = self._load_profile.observe(finite(states.get("sensor.opti_base_load_raw_w")), now,
                json.dumps(plant_semantic_fingerprint(input_options)),
                min_load_w=min(5000, max(0, finite(attributes.get("sensor.opti_house_raw_w", {}).get("forecast_min_load_w")) or 0)))
            self._load_profile_status = {"coverage_seconds": profile.coverage_seconds, "warming_up": profile.warming_up, "valid": profile.raw_mean_w is not None}
            for key in ("sensor.opti_house_consumption_60min_w", "sensor.haus_stromverbrauch_60_min"):
                states[key] = profile.forecast_w if profile.forecast_w is not None else "unavailable"
                attributes[key] = dict(self._load_profile_status)
            states["sensor.opti_load_profile_mean_w"] = profile.raw_mean_w if profile.raw_mean_w is not None else "unavailable"
            if score_opt_in:
                score_profile = self._score_load_profile.observe(
                    value, now, self._score_load_fingerprint,
                    min_load_w=states["sensor.opti_forecast_house_min_load_w"],
                )
                states["sensor.opti_forecast_house_60min_w"] = (
                    score_profile.forecast_w if score_profile.forecast_w is not None else "unavailable"
                )
        self._add_sun(states, attributes, now)
        states.update({key: ("on" if val else "off") if isinstance(val, bool) else val for key, val in self.settings.items()})
        previous_data = self.data if isinstance(self.data, dict) else {}
        previous_mode = previous_data.get("mode", "Akku Pause")
        if (
            (previous_data.get("decision_id") == "arbitrage_hold"
             and previous_mode == "Akku nur Laden")
            or (previous_data.get("decision_id") == PEAK_RANK_HOLD_DECISION
                and previous_mode in ("Akku nur Laden", "Akku Pause"))
        ):
            # The engine continues from its own L1/L2 mode, not from the hold:
            # the L2 band reads the previous mode.
            previous_mode = previous_data.get(
                "engine_base_mode",
                previous_data.get("engine_requested_mode", previous_mode),
            )
        states["input_select.akkusteuerung_modus"] = self.manual_mode or previous_mode
        # One battery model per cycle, rebuilt from the options (no cache).
        self._battery_model = BatteryModel.from_options(captured_options.get("arbitrage_estimate"))
        states[BATTERY_MODEL_ENTITY], attributes[BATTERY_MODEL_ENTITY] = self._battery_model.engine_state()
        demand_cfg = captured_options.get("demand_forecast", {})
        demand_comparison_enabled = (
            isinstance(demand_cfg, dict) and demand_cfg.get("enabled") is True
        )
        demand_sources = demand_cfg.get("sources", {}) if demand_comparison_enabled else {}
        if not isinstance(demand_sources, dict):
            demand_sources = {}
        comparison_states = {
            entity_id: self.hass.states.get(entity_id)
            for entity_id in demand_sources.values()
            if isinstance(entity_id, str)
        }
        demand_input = {
            "online": self._online,
            "states": states,
            "attributes": attributes,
            "source_errors": source_errors,
            # The report is calculated before this cycle's engine result. Its
            # current-strategy reserve field is therefore deliberately the
            # previous cycle's diagnostic value.
            "reserve_plan": previous_data.get("reserve_plan", {}),
        }
        try:
            demand_report = self._demand_forecast.update(
                now,
                demand_input,
                self.settings,
                captured_options,
                self.hass.states,
                dt_util.DEFAULT_TIME_ZONE,
                self._load_source_fingerprint,
                model=self._battery_model,
            )
        except Exception as err:  # Forecast activation must fail back to the legacy score.
            _LOGGER.debug("Demand observation unavailable: %s", type(err).__name__)
            demand_report = {"status": "error", "observation_only": True}
        demand_input["demand_forecast"] = demand_report
        active_profile_enabled = (
            demand_comparison_enabled
            and demand_cfg.get("use_for_peak_reserve") is True
        )
        remaining_day = None
        if active_profile_enabled:
            try:
                remaining_day = await self.hass.async_add_executor_job(
                    remaining_day_profile,
                    self._demand_forecast,
                    now,
                    demand_input,
                    self.settings,
                    captured_options,
                    comparison_states,
                    dt_util.DEFAULT_TIME_ZONE,
                    self._load_source_fingerprint,
                )
                active_profile, _ = remaining_day
            except Exception as err:  # Never replace the legacy score with an uncertain profile.
                remaining_day = None
                _LOGGER.debug("Active remaining-day profile unavailable: %s", type(err).__name__)
                active_profile = {"status": "error", "reason": "profile_error"}
        else:
            active_profile = {"status": "disabled", "reason": "active_profile_disabled"}
        profile_energy = finite(active_profile.get("profile_energy_kwh"))
        profile_ready = (
            active_profile.get("status") == "ready"
            and profile_energy is not None
            and profile_energy >= 0
        )
        states["sensor.opti_forecast_remaining_load_profile_kwh"] = (
            profile_energy if profile_ready else "unavailable"
        )
        attributes["sensor.opti_forecast_remaining_load_profile_kwh"] = active_profile
        try:
            peak_profile = peak_load_profile(
                self._demand_forecast, now,
                {"online": self._online, "states": states, "source_errors": source_errors,
                 "demand_forecast": demand_report},
                self.settings, captured_options, self.hass.states,
                dt_util.DEFAULT_TIME_ZONE, self._load_source_fingerprint)
        except Exception:
            _LOGGER.exception("Peak load profile unavailable; using configured load")
            peak_profile = {"status": "fallback", "hours": {}, "methods": {}, "reason": "profile_error"}
        states["sensor.opti_peak_load_profile"] = peak_profile["status"]
        attributes["sensor.opti_peak_load_profile"] = peak_profile
        # Observation only: the strategy never reads this estimate. A daily-total
        # forecast source has no intervals; the demand Solcast source is tried next.
        candidates = [entity for entity in (captured_options.get("sources", {}).get("forecast_today"),
                                            demand_sources.get("pv_today")) if entity] or [None]
        forecast_entity = None
        try:
            for forecast_entity in candidates:
                clipping_value, clipping_attributes = clipping_forecast(
                    self.hass.states.get(forecast_entity) if forecast_entity else None,
                    self.settings.get("input_number.akkusteuerung_wr_ac_ueberschuss_grenze"),
                    now, dt_util.DEFAULT_TIME_ZONE,
                    soc=states.get("sensor.opti_soc"),
                    capacity_kwh=states.get("sensor.opti_battery_capacity_kwh"),
                    maxsoc=self.settings.get("input_number.maxsoc"),
                    max_age=captured_options.get("forecast_max_age", DEFAULT_FORECAST_MAX_AGE),
                )
                if clipping_attributes.get("status") not in ("no_forecast", "no_detail"):
                    break
        except Exception as err:  # A diagnostic estimate must never stop the update.
            _LOGGER.debug("Clipping forecast unavailable: %s", type(err).__name__)
            clipping_value, clipping_attributes = "unavailable", {"status": "error", "observation_only": True}
        states["sensor.opti_clipping_forecast_kwh"] = clipping_value
        attributes["sensor.opti_clipping_forecast_kwh"] = {"source": forecast_entity, **clipping_attributes}
        ev_cfg = captured_options.get("ev_preparation", {})
        ev_signals = command_signals(
            ev_cfg, self.hass.states, now, dt_util.DEFAULT_TIME_ZONE
        )
        ev_preparation = self._ev_preparation.update(
            ev_cfg, self.hass.states, now, states, finite(self.settings.get("input_number.maxsoc")),
            self.strategy_enabled and self.manual_mode is None and self._online
            and self.settings.get("input_boolean.akku_opti_automatik") is True,
            can_prepare=not source_errors and states.get("sun.sun") == "above_horizon",
            timezone=dt_util.DEFAULT_TIME_ZONE,
        )
        previous_target_level = finite(
            self._engine_snapshot.get("attributes", {})
            .get("sensor.opti_target_soc", {})
            .get("level")
        )
        previous_latches = {key: self._engine_snapshot.get("states", {}).get(key) for key in PERSISTED_LATCHES}
        previous_latch_attributes = {
            (key, name): self._engine_snapshot.get("attributes", {}).get(key, {}).get(name)
            for key, name in PERSISTED_LATCH_ATTRIBUTES
        }
        previous_manual_cell = (self._manual_cell_latched, self._manual_cell_reserve_v)
        def evaluate():
            result = self.engine.evaluate(states, attributes, dt_util.as_local(now))
            return result, self.engine.snapshot()

        if self.strategy_enabled:
            result, self._engine_snapshot = await self.hass.async_add_executor_job(evaluate)
        else:
            self._add_manual_ev_safety(states, now)
            self._add_manual_cell_safety(states, now)
            result = Evaluation(states, attributes, "Beobachtung", "Strategie deaktiviert", {})
        engine_base_mode = result.mode
        arbitrage_cfg = captured_options.get("arbitrage_estimate", {})
        previous_arbitrage = (
            self.data.get("arbitrage_estimate") if isinstance(self.data, dict) else None
        )
        if (
            self.strategy_enabled
            and self.manual_mode is None
            and self.entry.data.get("backend", "sma") in ("sma", "sma_modbus")
            and isinstance(captured_options.get("demand_forecast"), dict)
            and captured_options["demand_forecast"].get("enabled") is True
        ):
            try:
                result, arbitrage_hold = apply_discharge_hold(
                    result,
                    previous_arbitrage,
                    arbitrage_cfg,
                    finite(result.states.get("sensor.opti_price_current_ct_kwh")),
                    now,
                )
            except Exception as err:
                _LOGGER.debug("Arbitrage hold unavailable: %s", type(err).__name__)
                arbitrage_hold = {
                    "hold_enabled": True,
                    "hold_status": "error",
                    "hold_controls_battery": False,
                }
        else:
            arbitrage_hold = {
                "hold_enabled": isinstance(arbitrage_cfg, dict)
                and arbitrage_cfg.get("hold_enabled") is True,
                "hold_status": (
                    "backend_not_supported"
                    if self.entry.data.get("backend", "sma") not in ("sma", "sma_modbus")
                    else "demand_forecast_required"
                    if not isinstance(captured_options.get("demand_forecast"), dict)
                    or captured_options["demand_forecast"].get("enabled") is not True
                    else "manual_or_strategy_disabled"
                ),
                "hold_controls_battery": False,
            }
        result, ev_report = apply_preparation(result, ev_preparation)
        if (
            arbitrage_hold.get("hold_controls_battery") is True
            and result.decision_id != "arbitrage_hold"
        ):
            arbitrage_hold.update(
                hold_status="higher_priority",
                hold_controls_battery=False,
            )
        peak_rank_cfg = captured_options.get("peak_rank_control")
        peak_rank_control = isinstance(peak_rank_cfg, dict) and peak_rank_cfg.get("enabled") is True
        peak_rank_early: dict[str, Any] | None = None
        if peak_rank_control:
            result, peak_rank_early = self._apply_peak_rank_hold(result, now)
        else:
            self._peak_rank_latch = None
        for key, value in result.helper_updates.items():
            if (key in self.settings and revision == self._revision
                    and soft_revision == self._soft_revision):
                try:
                    self.settings[key] = validate_setting(key, value in (True, "on", "true") if key in SWITCH_DEFINITIONS else value, self.settings)
                except HomeAssistantError:
                    self._last_error = "Ungültige interne Einstellungsänderung"
        requested = self.manual_mode or result.mode
        if not self.strategy_enabled and self.manual_mode is None:
            mode, safety_reason = "Beobachtung", "Strategie deaktiviert; keine weiteren Steuerbefehle"
        else:
            mode, safety_reason = self._safe_mode(requested, result.states, result.attributes)
        if self._failsafe_active and mode != "Beobachtung":
            mode, safety_reason = "Akku Pause", "Interner Fehler: Pause bis 60 s fehlerfrei"
        reason = safety_reason or ("Manuelle Auswahl" if self.manual_mode else result.reason)
        self._update_charge_curve_hold(result.states)
        params = self._parameters(result.states)
        # Text only, no wattage: values here change every update and would
        # flood the Recorder via the reason sensor's attributes (issue #129).
        charge_power_limits = []
        temperature = finite(result.states.get("sensor.opti_battery_temp"))
        if self.manual_mode and temperature is not None and temperature >= 45:
            charge_power_limits.append("Akkutemperatur ab 45 °C: Ladeleistung im manuellen Modus begrenzt")
        if (curve := self._charge_curve_limit(result.states)) is not None:
            charge_power_limits.append(curve[1])
        if arbitrage_hold.get("hold_controls_battery") or result.decision_id == PEAK_RANK_HOLD_DECISION:
            params["min_charge_w"] = 0
            params["min_discharge_w"] = 0
        if pv_peak_release_applies(result, mode, self.manual_mode, self.strategy_enabled):
            # Arbitrage hold and EV preparation switch a 'nur Entladen' preview to a
            # PV charge mode after the engine planned the power; apply the engine's
            # mode-independent PV peak latch so surplus is not throttled there.
            params["charge_power_w"] = max(params["charge_power_w"], params["max_charge_w"])
            charge_power_limits.append("PV-Spitze: Drossel gelöst (nachgelagerter PV-Lademodus)")
        if (self.manual_mode is None and mode == "Akku Dynamisch"
                and result.decision_id in ("peak_l1", "peak_l2", "unknown")):
            # Peak discharge only opens charging for PV surplus; a minimum
            # charge power would pull expensive grid power instead. A template
            # error hides the branch ("unknown"), so treat it the same way.
            params["min_charge_w"] = 0
            charge_power_limits.append("Preisspitze: Laden nur aus PV-Überschuss")
        if self.manual_mode is None and result.decision_id == "balancing_hold":
            params["min_charge_w"] = 0
            charge_power_limits.append("Balancing-Haltefenster: kein Mindestladen aus dem Netz")
        if ev_cfg.get("enabled") is True and self.strategy_enabled and self.manual_mode is None:
            # This feature never imposes minimum export/discharge or minimum
            # charging that could pull power from the grid.
            params["min_discharge_w"] = 0
            if ev_report.get("status") == "preparing" and safety_reason is None:
                params["min_charge_w"] = 0
                params["charge_power_w"] = min(params["charge_power_w"], ev_report.get("surplus_before_battery_w", 0))
                charge_power_limits.append("Auto-Vorbereitung: nur PV-Überschuss vor dem Akku")
        # Rewrite early only for a change of the effective register values,
        # which only the SMA driver knows (model clamp, settling cap). The
        # periodic renewal below is unchanged. Huawei targets also depend on
        # live entity limits, so every parameter stays exact there, as for
        # any device without its own signature.
        device_signature = getattr(type(self.device), "command_signature", None)
        signature = (device_signature(self.device, mode, params)
                     if callable(device_signature)
                     and self.entry.data.get("backend", "sma") in ("sma", "sma_modbus")
                     else (mode, *sorted(params.items())))
        battery = finite(result.states.get("sensor.opti_battery_power_w"))
        # Judge only the confirmed command for this mode, after the device ramp.
        applied = (self._applied_mode == mode and self._applied_mode_since is not None
                   and monotonic_now - self._applied_mode_since >= VIOLATION_SETTLE_SECONDS)
        violation = (not self.shadow_mode and self.write_enabled and battery is not None and applied
                     and ((mode in ("Akku Pause", "Akku nur Laden", "Akku Netzladen") and battery < -200)
                          or (mode in ("Akku Pause", "Akku nur Entladen") and battery > 200)))
        if mode != self._violation_mode:
            self._violation_since = None
        self._violation_mode = mode
        self._violation_since = (self._violation_since or monotonic_now) if violation else None
        if self._violation_since is None:
            self._violation_renewals = 0
        persistent_violation = self._violation_since is not None and monotonic_now - self._violation_since >= 30
        renew = (persistent_violation and self._violation_renewals < VIOLATION_MAX_RENEWALS
                 and (self._last_apply is None or monotonic_now - self._last_apply >= VIOLATION_RENEW_SECONDS))
        command_result = "not_attempted"
        write_failed = False
        last_write_before_command = getattr(self.device, "last_write", None)
        due = self._last_apply is None or monotonic_now - self._last_apply >= RECONCILE_SECONDS
        # Holding-phase pause writes share the incident throttle with the error path.
        failsafe_hold = self._failsafe_active and mode == "Akku Pause"
        if (not self.shadow_mode and self.write_enabled and self._online and connection["write_ready"] and self._identity.get("serial_number") and not self._stopping
                and mode in self.supported_modes
                and not (failsafe_hold and self._failsafe_throttled(monotonic_now))
                and (due or signature != self._last_signature or renew)):
            # Continuing a phased command is not another renewal.
            if renew and not self._command_pending:
                self._violation_renewals += 1
            # A renewal of the confirmed mode may finish despite a pure
            # information update; phased devices keep the strict rule.
            renewal = self._applied_mode == mode and not getattr(type(self.device), "PHASED", False)
            try:
                def current() -> bool:
                    if not self.write_enabled or self._stopping or self._revision != revision:
                        return False
                    if self._soft_revision != soft_revision and not renewal:
                        return False
                    if dict(self.entry.options) != captured_options:
                        return False
                    # Do not extend the validity of the captured measurements
                    # while waiting for a shared bus or a delayed write.
                    if time.monotonic() - read_started > 30:
                        return False
                    checked_at = dt_util.utcnow()
                    if ev_cfg.get("enabled") is True and command_signals(
                        ev_cfg,
                        self.hass.states,
                        checked_at,
                        dt_util.DEFAULT_TIME_ZONE,
                    ) != ev_signals:
                        return False
                    fresh_states, fresh_attributes, fresh_errors = build_inputs(
                        self._measurements,
                        input_options,
                        self.hass.states,
                        checked_at,
                        price_snapshot=self._current_price_snapshot(),
                    )
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
                    if (
                        (peak_rank_control or (isinstance(arbitrage_cfg, dict)
                                               and arbitrage_cfg.get("hold_enabled") is True))
                        # Numeric comparison: the engine returns states as
                        # text, the fresh inputs are numbers. Compared raw,
                        # every write looked superseded (write stall).
                        and (
                            finite(result.states.get("sensor.opti_price_current_ct_kwh"))
                            != finite(fresh_states.get("sensor.opti_price_current_ct_kwh"))
                        )
                    ):
                        return False
                    previously_valid = {key for key in source_keys if key not in source_errors}
                    return not (previously_valid & fresh_errors.keys())

                await self.device.async_apply(mode, params, current)
                command_result = "confirmed"
                self._command_pending = False
                self._last_signature = signature
                self._last_apply = time.monotonic()
                if self._applied_mode != mode:
                    self._applied_mode, self._applied_mode_since = mode, self._last_apply
                if persistent_violation:
                    self._last_error = "Sperrverletzung beobachtet; Sollwerte erneuert"
                else:
                    self._clear_write_error()
            except PendingCommandError as err:
                command_result = "safe_phase_confirmed" if err.confirmed else "pending"
                if err.confirmed:
                    self._last_apply = time.monotonic()
                if self._applied_mode != mode:
                    self._applied_mode = None
                self._command_pending = True
                self._last_signature = None
            except StaleCommandError as err:
                command_result = "failed" if err.cleanup_failed else "superseded"
                write_failed = bool(err.cleanup_failed)
                self._applied_mode = None
                self._command_pending = False
                self._last_signature = None
                # Superseded decisions are expected when sources change during I/O.
                # Keep the original write age; repeated supersession still trips the watchdog.
                # Nothing was confirmed: keep any earlier error visible.
                if err.cleanup_failed:
                    self._last_error = "Schreibvorgang nicht bestätigt: Sicherheits-Pause fehlgeschlagen"
            except Exception as err:
                command_result = "failed"
                write_failed = True
                self._applied_mode = None
                self._command_pending = False
                self._last_signature = None
                self._last_error = f"Schreibvorgang nicht bestätigt: {type(err).__name__}"
            if failsafe_hold:
                self._record_failsafe_attempt(monotonic_now, command_result == "confirmed", write_failed)
        if persistent_violation and not renew and not write_failed and self._violation_renewals >= VIOLATION_MAX_RENEWALS:
            self._last_error = f"Sperrverletzung hält trotz {VIOLATION_MAX_RENEWALS} Erneuerungen an"
        result.states["binary_sensor.opti_connection"] = "on" if self._online else "off"
        result.states["binary_sensor.opti_block_violation"] = "on" if persistent_violation else "off"
        # A documented night standby (#103) cannot be written to; its time does
        # not count as "without a successful write". The watchdog resumes
        # counting from the end of the standby.
        if night_standby({"connection_status": connection, "online": self._online, "states": result.states, "mode": mode}):
            self._write_stall_paused_at = monotonic_now
        result.states["binary_sensor.opti_write_stalled"] = "on" if self._write_stalled(monotonic_now) else "off"
        metadata = {key: dict(value) for key, value in self._metadata.items()}
        for identity in result.states:
            if identity.startswith("sensor.opti_") and identity.endswith("_w"):
                metadata.setdefault(identity, {}).update(unit_of_measurement="W", device_class="power", state_class="measurement")
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
                "mode": mode, "reason": reason, "decision_id": result.decision_id,
                "reason_details": {"decision_id": result.decision_id,
                                   "engine_mode": result.mode,
                                   "charge_power_limits": charge_power_limits,
                                   **({"manual_limits": self._manual_limits()} if self.manual_mode else {})},
                "engine_requested_mode": result.mode,
                "engine_base_mode": engine_base_mode,
                "command_result_this_update": command_result, "write_enabled": self.write_enabled and not self.shadow_mode,
                "pause_pending": self._pause_pending and not self.write_enabled,
                "command_confirmation": "waiting_ready" if self.write_enabled and not connection["write_ready"] else "pending" if self._command_pending else "idle_or_confirmed",
                "command_evidence": command_evidence,
                "last_write_values": getattr(self.device, "last_write_values", None),
                "control_release": ("not_confirmed" if getattr(self.device, "supports_control_release", False) is True else "not_supported"),
                "connection_status": connection,
                "online": self._online, "last_write": getattr(self.device, "last_write", None),
                "last_error": self._last_error, "source_errors": source_errors,
                "score_load_error": score_load_error,
                "device_errors": dict(read_errors) if isinstance(read_errors, dict) else {}, "load_profile": self._load_profile_status,
                "price_provider_error": self._price_provider_error,
                "price_last_success": self._price_last_success,
                "notification_error": self.alerts.delivery_error,
                "identity": self._identity, "manual_mode": self.manual_mode, "strategy_enabled": self.strategy_enabled,
                "control_inactive": bool(self.strategy_enabled and not self.shadow_mode and not self.write_enabled
                                         and self.supported_modes
                                         and self.entry.options.get("single_writer_confirmed", False)),
                "write_restore_blocked": self._write_restore_blocked,
                "internal_error": self._internal_error_report(self._failsafe_pause_state(command_result))}
        self._update_operating_report(data, now, ev_report, safety_reason, write_failed)
        data["demand_forecast"] = demand_report
        if demand_comparison_enabled:
            try:
                def compare():
                    return build_strategy_comparison(
                        self._demand_forecast, now, data, self.settings, captured_options,
                        comparison_states, dt_util.DEFAULT_TIME_ZONE,
                        self._load_source_fingerprint, previous_target_level,
                        remaining_day=remaining_day,
                    )

                comparison = await self.hass.async_add_executor_job(compare)
            except Exception as err:  # Comparison must remain isolated from control and reports.
                _LOGGER.debug("Demand strategy comparison unavailable: %s", type(err).__name__)
                comparison = {"status": "error", "observation_only": True}
            data["demand_forecast"] = {
                **data["demand_forecast"], "strategy_comparison": comparison
            }
            self._update_refill_comparison(data, now)
        else:
            # Disabled time must not count under the last held verdict after re-enabling.
            self._refill_comparison.interrupt()
        self._update_negative_price_hold(
            data, now, (captured_options.get("sources", {}).get("forecast_today"), demand_sources.get("pv_today")),
            captured_options)
        await self._async_update_planner(data, now, captured_options, demand_sources)
        self._update_evcc_comparison(data, now, captured_options)
        self._update_peak_rank(data, now, peak_rank_early, safety_reason)
        self._update_arbitrage_report(data, now, captured_options, arbitrage_hold, safety_reason)
        await self._async_update_observations(data, now, captured_options)
        # Only a real on<->off transition or a changed latch origin forces a
        # write; a latch missing on one side (disabled strategy, first cycle)
        # waits for the routine interval.
        latch_changed = any(
            previous_latches[key] != result.states.get(key)
            and previous_latches[key] in ("on", "off") and result.states.get(key) in ("on", "off")
            for key in PERSISTED_LATCHES
        ) or any(
            previous is not None and self._engine_snapshot.get("attributes", {}).get(key, {}).get(name)
            not in (None, previous)
            for (key, name), previous in previous_latch_attributes.items()
        ) or previous_manual_cell != (self._manual_cell_latched, self._manual_cell_reserve_v)
        self._schedule_routine_save(monotonic_now, force=latch_changed)
        return data

    def _write_stalled(self, monotonic_now: float) -> bool:
        """True after 240 s without a confirmed write while writes are enabled."""
        # Count from the later of the last confirmed write and the start of
        # this write session: an old write must not trip the watchdog at once
        # after writes were re-enabled.
        stall_reference = max((value for value in (self._last_apply, self._write_monitor_since)
                               if value is not None), default=None)
        if stall_reference is not None and self._write_stall_paused_at is not None:
            stall_reference = max(stall_reference, self._write_stall_paused_at)
        return (not self.shadow_mode and self.write_enabled and self._write_monitor_since is not None
                and stall_reference is not None and monotonic_now - stall_reference > 240)

    def _cycle_succeeded(self, monotonic_now: float) -> None:
        """End an internal-error incident after enough error-free time."""
        self._cycle_finished_at = time.monotonic()
        if (self._cycle_fail_last is not None
                and monotonic_now - self._cycle_fail_last >= CYCLE_FAILURE_CLEAR_SECONDS):
            self._cycle_fail_first = self._cycle_fail_last = None
            self._cycle_fail_since = self._cycle_fail_type = None
            self._failsafe_active = False
            self._failsafe_attempt_at = None
            self._failsafe_failed_attempts = 0
            if self._last_error and self._last_error.startswith("Interner Fehler"):
                self._last_error = None

    def _internal_error_report(self, pause: str) -> dict[str, Any] | None:
        if self._cycle_fail_first is None:
            return None
        return {"active": self._failsafe_active, "error_type": self._cycle_fail_type,
                "since": self._cycle_fail_since.isoformat() if self._cycle_fail_since else None,
                "pause": pause}

    def _failsafe_pause_possible(self, connection: Mapping[str, Any]) -> bool:
        return bool(self._online and connection.get("write_ready") and self._identity.get("serial_number")
                    and "Akku Pause" in self.supported_modes and not self._stopping)

    def _failsafe_pause_state(self, command_result: str) -> str:
        """Pause status of a running incident as seen by a successful cycle."""
        if self.shadow_mode or not self.write_enabled:
            return "not_required"
        if not self._failsafe_active:
            return "waiting"
        if self._applied_mode == "Akku Pause":
            return "confirmed"
        return {"pending": "pending", "safe_phase_confirmed": "pending",
                "failed": "failed"}.get(command_result, "waiting")

    def _failsafe_throttled(self, monotonic_now: float) -> bool:
        """Spacing of safety-pause attempts after failures, for the whole incident."""
        if self._command_pending:
            # A running phase continues in the next cycle.
            return False
        if not self._failsafe_failed_attempts or self._failsafe_attempt_at is None:
            return False
        interval = (FAILSAFE_RETRY_SECONDS if self._failsafe_failed_attempts <= FAILSAFE_FAST_RETRIES
                    else RECONCILE_SECONDS)
        return monotonic_now - self._failsafe_attempt_at < interval

    def _record_failsafe_attempt(self, monotonic_now: float, confirmed: bool, failed: bool) -> None:
        self._failsafe_attempt_at = monotonic_now
        if confirmed:
            self._failsafe_failed_attempts = 0
        elif failed:
            self._failsafe_failed_attempts += 1

    async def _async_failsafe_pause(self, monotonic_now: float, connection: Mapping[str, Any]) -> tuple[str, str]:
        """Write the safety pause after an internal error; return (pause, command_result)."""
        if (self.shadow_mode or not self.write_enabled or self._stopping
                or (self.manual_mode is None and not self.strategy_enabled)):
            return "not_required", "not_attempted"
        if not self._failsafe_pause_possible(connection):
            return "not_possible", "not_attempted"
        if (self._applied_mode == "Akku Pause" and self._last_apply is not None
                and monotonic_now - self._last_apply < RECONCILE_SECONDS):
            return "confirmed", "not_attempted"
        if self._failsafe_throttled(monotonic_now):
            return "waiting", "not_attempted"
        try:
            try:
                params = self._parameters((self.data or {}).get("states", {}))
            except Exception:
                params = self._parameters({})
            await self.device.async_apply(
                "Akku Pause", params, lambda: self.write_enabled and not self._stopping)
        except PendingCommandError as err:
            # Huawei continues its phases in the next failed cycle; no throttle.
            self._record_failsafe_attempt(monotonic_now, False, False)
            if err.confirmed:
                self._last_apply = time.monotonic()
            if self._applied_mode != "Akku Pause":
                self._applied_mode = None
            self._command_pending = True
            self._last_signature = None
            return "pending", "pending"
        except StaleCommandError:
            # Write permission was withdrawn; the disable path writes its own pause.
            self._record_failsafe_attempt(monotonic_now, False, False)
            self._applied_mode = None
            self._command_pending = False
            self._last_signature = None
            return ("not_required" if not self.write_enabled else "waiting"), "superseded"
        except Exception as err:
            self._record_failsafe_attempt(monotonic_now, False, True)
            self._applied_mode = None
            self._command_pending = False
            self._last_signature = None
            self._last_error = ("Schreibvorgang nicht bestätigt: Sicherheits-Pause nach internem Fehler "
                                f"fehlgeschlagen ({type(err).__name__})")
            return "failed", "failed"
        self._last_apply = time.monotonic()
        if self._applied_mode != "Akku Pause":
            self._applied_mode, self._applied_mode_since = "Akku Pause", self._last_apply
        # The holding phase renews this pause with the regular interval
        # instead of rewriting it in the next successful cycle.
        self._command_pending = False
        self._record_failsafe_attempt(monotonic_now, True, False)
        self._clear_write_error()
        device_signature = getattr(type(self.device), "command_signature", None)
        try:
            self._last_signature = (device_signature(self.device, "Akku Pause", params)
                                    if callable(device_signature)
                                    and self.entry.data.get("backend", "sma") in ("sma", "sma_modbus")
                                    else ("Akku Pause", *sorted(params.items())))
        except Exception:
            # The confirmed pause stands; only an earlier renewal may follow.
            self._last_signature = None
        return "confirmed", "confirmed"

    async def _async_cycle_failed(self, err: Exception, now: datetime, monotonic_now: float,
                                  connection: dict[str, Any], read_errors: Any) -> dict[str, Any]:
        """Handle an unexpected exception in the cycle body with a degraded data set."""
        # The cycle did not advance the curve hold; the next good cycle
        # starts from the stricter reading, as after an unknown SoC.
        self._charge_curve_held = None
        try:
            error_type = type(err).__name__
            if self._cycle_fail_first is None or error_type != self._cycle_fail_type:
                _LOGGER.error("Opti control cycle failed (%s); evaluation suspended",
                              error_type, exc_info=err)
            if self._cycle_fail_first is None:
                self._cycle_fail_first, self._cycle_fail_since = monotonic_now, now
            self._cycle_fail_last = monotonic_now
            self._cycle_fail_type = error_type
            if monotonic_now - self._cycle_fail_first >= CYCLE_FAILURE_GRACE_SECONDS:
                self._failsafe_active = True
            previous = self.data if isinstance(self.data, dict) else {}
            # Current control state, never the previously displayed mode.
            observation = not self.strategy_enabled and self.manual_mode is None
            if not self._failsafe_active:
                pause = ("not_required" if self.shadow_mode or not self.write_enabled or observation
                         else "waiting")
                command_result = "not_attempted"
            elif observation:
                pause, command_result = "not_required", "not_attempted"
            else:
                pause, command_result = await self._async_failsafe_pause(monotonic_now, connection)
            if not self._last_error or self._last_error.startswith("Interner Fehler"):
                self._last_error = f"Interner Fehler: {error_type}"
            if not self._failsafe_active:
                reason = f"Interner Fehler ({error_type}): Bewertung ausgefallen, letzter Befehl gilt weiter"
            elif pause == "confirmed":
                reason = f"Interner Fehler ({error_type}): Sicherheits-Pause geschrieben"
            elif pause == "not_required":
                reason = f"Interner Fehler ({error_type}): keine Schreibfreigabe, kein Eingriff"
            else:
                reason = (f"Interner Fehler ({error_type}): Pause nicht bestätigt, "
                          "Gerät führt den letzten Befehl weiter")
            previous_states = previous.get("states")
            states = {key: "unavailable" for key in previous_states} if isinstance(previous_states, dict) else {}
            states["binary_sensor.opti_connection"] = "on" if self._online else "off"
            states["binary_sensor.opti_write_stalled"] = "on" if self._write_stalled(monotonic_now) else "off"
            states["binary_sensor.opti_block_violation"] = "unavailable"
            data = dict(previous)
            data.update({
                "states": states, "attributes": {}, "metadata": previous.get("metadata", {}),
                "mode": "Beobachtung" if observation else "Akku Pause",
                "reason": reason, "decision_id": "internal_error",
                "reason_details": {"decision_id": "internal_error", "engine_mode": None,
                                   "charge_power_limits": []},
                "engine_requested_mode": "Akku Pause", "engine_base_mode": "Akku Pause",
                "command_result_this_update": command_result,
                "write_enabled": self.write_enabled and not self.shadow_mode,
                "pause_pending": self._pause_pending and not self.write_enabled,
                "command_confirmation": "waiting_ready" if self.write_enabled and not connection["write_ready"] else "pending" if self._command_pending else "idle_or_confirmed",
                "last_write_values": getattr(self.device, "last_write_values", None),
                "control_release": ("not_confirmed" if getattr(self.device, "supports_control_release", False) is True else "not_supported"),
                "connection_status": connection,
                "online": self._online, "last_write": getattr(self.device, "last_write", None),
                "last_error": self._last_error,
                "device_errors": dict(read_errors) if isinstance(read_errors, dict) else {},
                "price_provider_error": self._price_provider_error,
                "price_last_success": self._price_last_success,
                "identity": self._identity, "manual_mode": self.manual_mode, "strategy_enabled": self.strategy_enabled,
                "control_inactive": bool(self.strategy_enabled and not self.shadow_mode and not self.write_enabled
                                         and self.supported_modes
                                         and self.entry.options.get("single_writer_confirmed", False)),
                "write_restore_blocked": self._write_restore_blocked,
                "reserve_plan": {"status": "no_valid_plan"},
                "ev_preparation": {"status": "data_missing", "controls_battery": False, "observation_only": True},
                "arbitrage_estimate": None,
                "internal_error": self._internal_error_report(pause),
            })
        except Exception as build_err:
            raise UpdateFailed(f"Opti control cycle failed: {type(err).__name__}") from build_err
        finally:
            self._cycle_finished_at = time.monotonic()
        return data

    @callback
    def async_start_watchdog(self) -> None:
        """Report a cycle that never finishes; the timer never writes or locks."""
        if self._unsubscribe_watchdog is not None:
            return
        self._watchdog_started_at = time.monotonic()
        self._unsubscribe_watchdog = async_track_time_interval(
            self.hass, self._async_cycle_watchdog, timedelta(seconds=CYCLE_WATCHDOG_INTERVAL_SECONDS))
        self.entry.async_on_unload(self._stop_watchdog)

    @callback
    def _stop_watchdog(self) -> None:
        if self._unsubscribe_watchdog is not None:
            unsubscribe, self._unsubscribe_watchdog = self._unsubscribe_watchdog, None
            unsubscribe()

    @callback
    def _async_cycle_watchdog(self, _now: datetime | None = None) -> None:
        if self._stopping:
            return
        reference = self._cycle_finished_at if self._cycle_finished_at is not None else self._watchdog_started_at
        if reference is None:
            return
        monotonic_now = time.monotonic()
        stalled_for = monotonic_now - reference
        if stalled_for <= CYCLE_STALL_SECONDS:
            return
        data = dict(self.data) if isinstance(self.data, dict) else {}
        states = dict(data.get("states", {})) if isinstance(data.get("states"), dict) else {}
        states["binary_sensor.opti_write_stalled"] = "on" if self._write_stalled(monotonic_now) else "off"
        data.update({
            "states": states,
            "internal_error": {"active": True, "error_type": "cycle_stalled",
                               "since": (dt_util.utcnow() - timedelta(seconds=stalled_for)).isoformat(),
                               "pause": "not_possible"},
            "last_error": "Interner Fehler: Steuerzyklus ohne Abschluss",
            "reason": "Interner Fehler (Steuerzyklus hängt): Gerät führt den letzten Befehl weiter",
        })
        with self._auxiliary_guard("alerts"):
            self.alerts.update(data)
        self.data = data
        self.async_update_listeners()

    @callback
    def _schedule_routine_save(self, monotonic_now: float, *, force: bool = False) -> None:
        """Request the routine store write at most once per interval, or now for a latch change."""
        if (not force and self._routine_save_at is not None
                and monotonic_now - self._routine_save_at < STORE_SAVE_INTERVAL):
            return
        self._routine_save_at = monotonic_now
        self._store.async_delay_save(self._stored_data, 0)

    def _update_refill_comparison(self, data, now):
        """Attach the peak-versus-refill shadow comparison; it never changes control."""
        try:
            check = refill_reserve_check(
                data.get("demand_forecast"), data.get("reserve_plan"), data.get("states")
            )
            comparison = {**check, "totals": self._refill_comparison.observe(now, check)}
        except Exception as err:  # Observation only; never affects the demand report.
            _LOGGER.debug("Refill reserve comparison unavailable: %s", type(err).__name__)
            self._refill_comparison.interrupt()
            comparison = {"status": "error", "informational_only": True,
                          "controls_battery": False}
        data["demand_forecast"] = {
            **data["demand_forecast"], "refill_reserve_comparison": comparison
        }

    def _update_operating_report(self, data, now, ev_report, safety_reason, write_failed):
        """Reporting failures must not interrupt an already evaluated command."""
        try:
            data["ev_preparation"] = {**ev_report, "controls_battery": bool(ev_report.get("controls_battery") and self.write_enabled and not self.shadow_mode and safety_reason is None), "observation_only": self.shadow_mode or not self.write_enabled}
            data["reserve_plan"] = reserve_plan(data, self.settings, now, shadow=self.shadow_mode,
                                                model=self._battery_model)
            data["operating_report"] = self._operating_report.observe(now, data, data["reserve_plan"], write_failed=write_failed)
        except Exception as err:  # Reporting must never interrupt battery control.
            _LOGGER.debug("Operating report unavailable: %s", type(err).__name__)
            data["reserve_plan"] = {"status": "no_valid_plan"}
            data["operating_report"] = {"status": "error", "error_type": type(err).__name__}

    def _update_negative_price_hold(self, data, now, forecast_entities, options):
        """Shadow check for #136; runs after the decision and never changes it."""
        key = "sensor.opti_negative_price_hold"
        states, attributes = data["states"], data["attributes"]
        try:
            # A daily-total forecast source has no intervals; try the demand Solcast source next.
            forecast_entity, pv_slots = None, []
            max_age = options.get("forecast_max_age", DEFAULT_FORECAST_MAX_AGE)
            for candidate in forecast_entities:
                if candidate and (pv_slots := pv_intervals(self.hass.states, {"pv_today": candidate}, now,
                                                           max_age=max_age)):
                    forecast_entity = candidate
                    break
            load = states.get("sensor.opti_house_consumption_60min_w")
            if finite(load) is None:
                load = states.get("sensor.opti_house_consumption_w")
            check = negative_price_hold_check(
                now=now, timezone=dt_util.DEFAULT_TIME_ZONE,
                price_series=attributes.get("sensor.opti_price_series"),
                feed_in_ct=self.settings.get("input_number.opti_einspeiseverguetung_ct"),
                pv_slots=pv_slots,
                load_w=load, score=states.get("sensor.opti_forecast_score"),
                soc=states.get("sensor.opti_soc"), capacity_kwh=states.get("sensor.opti_battery_capacity_kwh"),
                minsoc=self.settings.get("input_number.minsoc"), maxsoc=self.settings.get("input_number.maxsoc"),
                peak_reserve_active=states.get("binary_sensor.opti_peak_reserve_aktiv") == "on",
                peak_reserve_soc=states.get("sensor.opti_peak_reserve_soc"),
                charge_efficiency=self._battery_model.charge_efficiency,
            )
            assumptions = [self._battery_model.charge_efficiency,
                           self.settings.get("input_number.opti_einspeiseverguetung_ct"),
                           self.settings.get("input_number.minsoc"), self.settings.get("input_number.maxsoc")]
            check["source"] = forecast_entity
            check["totals"] = self._negative_price_hold.observe(
                now, check, assumptions, states.get("sensor.opti_battery_power_w"),
                states.get("sensor.opti_grid_export_w"))
        except Exception as err:  # Observation only; never affects the decision.
            _LOGGER.debug("Negative price hold check unavailable: %s", type(err).__name__)
            self._negative_price_hold.interrupt()
            check = {"status": "error", "observation_only": True, "controls_battery": False}
        states[key] = check.get("verdict", "unavailable") if check.get("status") == "ready" else "unavailable"
        attributes[key] = check

    def _peak_rank_check(self, states, attributes, now, decision_id, strategy_mode):
        settings = self.settings
        peak = attributes.get("sensor.opti_peak_reserve_soc") or {}
        profile = attributes.get("sensor.opti_peak_load_profile") or {}
        return peak_rank_check(
            now=now, timezone=dt_util.DEFAULT_TIME_ZONE,
            price_series=attributes.get("sensor.opti_price_series"),
            horizon_end=peak.get("horizont_ende"), load_hours=profile.get("hours"),
            fallback_load_kw=settings.get("input_number.opti_peak_verbrauch_kw"),
            surcharge_ct=settings.get("input_number.opti_peak_min_aufschlag_ct"),
            soc=states.get("sensor.opti_soc"), capacity_kwh=states.get("sensor.opti_battery_capacity_kwh"),
            minsoc=settings.get("input_number.minsoc"), maxsoc=settings.get("input_number.maxsoc"),
            decision_id=decision_id,
            strategy_mode=strategy_mode,
            peak_reserve_active=states.get("binary_sensor.opti_peak_reserve_aktiv") == "on",
            grid_charging=settings.get("input_boolean.opti_prognose_netzladen") is True,
            spread_ct=settings.get("input_number.opti_netzlade_spread_ct"),
            feed_in_ct=settings.get("input_number.opti_einspeiseverguetung_ct"),
            hold_spread_ct=settings.get("input_number.opti_halte_spread_ct"),
            score=states.get("sensor.opti_forecast_score"),
            discharge_efficiency=self._battery_model.discharge_efficiency,
            battery_model=self._battery_model,
        )

    def _apply_peak_rank_hold(self, result, now):
        """Optional hold before the write; any failure keeps the strategy decision."""
        try:
            check = self._peak_rank_check(result.states, result.attributes, now,
                                          result.decision_id, result.mode)
        except Exception as err:  # Never disturbs the control cycle.
            _LOGGER.debug("Peak rank check unavailable: %s", type(err).__name__)
            check = {"status": "error"}
        try:
            held, status, self._peak_rank_latch = apply_peak_rank_hold(
                result, check, self._peak_rank_latch,
                eligible=self.strategy_enabled and self.manual_mode is None)
        except Exception as err:
            _LOGGER.debug("Peak rank hold unavailable: %s", type(err).__name__)
            held, status, self._peak_rank_latch = result, "error", None
        check = {**check, "observation_only": False, "control_enabled": True,
                 "control_status": status, "controls_battery": held is not result}
        return held, check

    def _update_peak_rank(self, data, now, early=None, safety_reason=None):
        """Price-rank allocation for L1/L2.

        Without the option it runs after the decision and never changes it.
        With the option ``early`` is the check from before the write; this
        only records it.
        """
        states, attributes = data["states"], data["attributes"]
        key = "sensor.opti_peak_rank"
        try:
            if early is not None:
                if early.get("status") == "error":
                    raise RuntimeError("early peak rank check failed")
                check = dict(early)
                if check["controls_battery"] and (data.get("decision_id") != PEAK_RANK_HOLD_DECISION
                                                  or data.get("mode") != data.get("engine_requested_mode")
                                                  or safety_reason is not None):
                    # A safety pause or the failsafe replaced the hold, even
                    # when it writes the same mode.
                    check.update(controls_battery=False, control_status="higher_priority")
                elif check["controls_battery"] and (data.get("write_enabled") is not True
                                                    or self._applied_mode != data.get("mode")):
                    # Only a confirmed hold mode on the device blocks anything;
                    # a failed or superseded write leaves the previous mode.
                    check["controls_battery"] = False
            else:
                check = self._peak_rank_check(states, attributes, now, data.get("decision_id"),
                                              data.get("engine_requested_mode"))
            settings = self.settings
            # Changed model values restart the totals.
            assumptions = [*peak_rank_model_assumptions(self._battery_model), *(settings.get(name) for name in (
                "input_number.minsoc", "input_number.opti_netzlade_spread_ct",
                "input_number.opti_peak_min_aufschlag_ct", "input_number.opti_einspeiseverguetung_ct",
                "input_boolean.opti_prognose_netzladen", "input_number.opti_peak_verbrauch_kw"))]
            check["totals"] = self._peak_rank.observe(
                now, check, assumptions, states.get("sensor.opti_battery_power_w"))
        except Exception as err:  # Reporting only; never affects the decision.
            _LOGGER.debug("Peak rank check unavailable: %s", type(err).__name__)
            self._peak_rank.interrupt()
            check = {"status": "error", "observation_only": early is None, "controls_battery": False,
                     **({"control_enabled": True, "control_status": "error"} if early is not None else {})}
        states[key] = (check.get("verdict", "unavailable")
                       if check.get("status") in ("ready", "uncertain", "incomplete") else "unavailable")
        attributes[key] = check

    def _planner_entity_enabled(self) -> bool:
        return self._shadow_entity_enabled("sensor.opti_plan_shadow")

    def _shadow_entity_enabled(self, key: str) -> bool:
        """Shadow sensors are disabled by default; a disabled entity costs no CPU or I/O."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{self.entry.entry_id}_{key}")
        entry = registry.async_get(entity_id) if entity_id else None
        return entry is not None and entry.disabled_by is None

    def _update_evcc_comparison(self, data, now, options):
        """Compare the ladder with the evcc optimizer; it never changes control."""
        key = "sensor.opti_evcc_comparison"
        states, attributes = data["states"], data["attributes"]
        cfg = options.get("evcc_comparison") if isinstance(options.get("evcc_comparison"), dict) else {}
        entity, url = cfg.get("suggestion_entity"), cfg.get("url")
        try:
            if not entity and not url:
                check = {"status": "not_configured"}
            elif not self._shadow_entity_enabled(key):
                check = {"status": "disabled", "detail": "entity_disabled"}
            else:
                check = None
            # Options change without a reload: a plan from another address, or from
            # before the comparison was switched off, never counts.
            active_url = url if check is None else None
            if active_url != self._evcc_url:
                self._reset_evcc_plan(active_url)
            if check is not None:
                self._evcc_comparison.interrupt()
                check.update(informational_only=True, controls_battery=False)
            else:
                if url:
                    self._schedule_evcc_fetch(url)
                if entity:
                    state = self.hass.states.get(entity)
                    suggestion = suggestion_status(
                        state.attributes if state else None, state.state if state else None,
                        state.last_reported if state else None, now)
                else:
                    suggestion = {"status": "not_configured"}
                if not url:
                    plan = {"status": "not_configured"}
                elif self._evcc_fetch_error is not None:
                    plan = {"status": "fetch_error", "error": self._evcc_fetch_error}
                else:
                    plan = derive_plan(self._evcc_evopt, now)
                check = evcc_check(suggestion, plan, data.get("mode"))
                check["totals"] = self._evcc_comparison.observe(now, check)
        except Exception as err:  # Observation only; never affects the decision.
            _LOGGER.debug("evcc comparison unavailable: %s", type(err).__name__)
            self._evcc_comparison.interrupt()
            check = {"status": "error", "informational_only": True, "controls_battery": False}
        states[key] = check.get("verdict", "unknown") if check.get("status") == "ready" else "unknown"
        attributes[key] = check

    @callback
    def _reset_evcc_plan(self, url: str | None) -> None:
        if self._evcc_task is not None and not self._evcc_task.done():
            # Cancellation completes later; the new address must not wait for it.
            self._evcc_task.cancel()
        self._evcc_task = None
        self._evcc_url = url
        self._evcc_evopt = None
        self._evcc_fetch_error = None
        self._evcc_next_fetch = None

    @callback
    def _schedule_evcc_fetch(self, url: str) -> None:
        """Read evcc's evopt result in the background; never await the network in the update."""
        if (self._stopping or (self._evcc_task is not None and not self._evcc_task.done())
                or (self._evcc_next_fetch is not None and dt_util.utcnow() < self._evcc_next_fetch)):
            return
        self._evcc_next_fetch = dt_util.utcnow() + EVCC_FETCH_INTERVAL
        self._evcc_task = self.entry.async_create_background_task(
            self.hass, self._async_fetch_evcc(url), "Opti evcc optimizer plan", eager_start=False)

    async def _async_fetch_evcc(self, url: str) -> None:
        """A failed read drops the cached plan, so an old plan never looks current."""
        try:
            raw = await fetch_evopt(async_get_clientsession(self.hass), url)
            payload = await self.hass.async_add_executor_job(json.loads, raw)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any read failure stays in the comparison
            payload, error = None, type(err).__name__
        else:
            error = None
        if url == self._evcc_url:
            self._evcc_evopt, self._evcc_fetch_error = payload, error

    async def _async_update_planner(self, data, now, options, demand_sources):
        """Shadow horizon planner; runs after the decision and never changes it.

        The solve is CPU work in an executor thread with a time budget. With
        the entity disabled nothing is computed and nothing is recorded unless
        ``planner.history_always`` asks for the forecast history anyway.
        """
        key = "sensor.opti_plan_shadow"
        states, attributes = data["states"], data["attributes"]
        try:
            settings = self.settings
            cfg = options.get("planner") if isinstance(options.get("planner"), dict) else {}
            enabled = self._planner_entity_enabled()
            if not enabled and cfg.get("history_always") is not True:
                self._planner.interrupt()
                states[key] = "unknown"
                attributes[key] = {"status": "disabled", "detail": "entity_disabled", "observation_only": True,
                                   "controls_battery": False}
                return
            sources = options.get("sources", {})
            pv_status, pv_hourly, periods = "no_forecast", {}, []
            for today, tomorrow in ((sources.get("forecast_today"), sources.get("forecast_tomorrow")),
                                    (demand_sources.get("pv_today"), demand_sources.get("pv_tomorrow"))):
                entities = [self.hass.states.get(entity) for entity in (today, tomorrow) if entity]
                pv_status, pv_hourly = pv_forecast_hourly(
                    entities, now, max_age=options.get("forecast_max_age", DEFAULT_FORECAST_MAX_AGE))
                if pv_status == "ready":
                    break
            profile = attributes.get("sensor.opti_peak_load_profile") or {}
            load_hours = _expected_load_hours(profile)
            if load_hours:
                load_source = "peak_load_profile"
                fallback_load_w = finite(settings.get("input_number.opti_peak_verbrauch_kw")) or 0
                fallback_load_w *= 1000
            elif finite(states.get("sensor.opti_house_consumption_60min_w")) is not None:
                load_source = "house_consumption_60min"
                fallback_load_w = finite(states.get("sensor.opti_house_consumption_60min_w"))
            else:
                load_source = "fixed_setting"
                fallback_load_w = (finite(settings.get("input_number.opti_peak_verbrauch_kw")) or 0) * 1000
            inputs = PlannerInputs(
                price_series=attributes.get("sensor.opti_price_series"),
                pv_status=pv_status, pv_hourly=pv_hourly,
                load_hours=load_hours if load_hours else None, load_source=load_source,
                fallback_load_w=fallback_load_w,
                soc=states.get("sensor.opti_soc"), capacity_kwh=states.get("sensor.opti_battery_capacity_kwh"),
                minsoc=settings.get("input_number.minsoc"), maxsoc=settings.get("input_number.maxsoc"),
                max_charge_w=settings.get("input_number.akkusteuerung_max_ladestaerke"),
                max_discharge_w=settings.get("input_number.akkusteuerung_max_entladestaerke"),
                feed_in_ct=settings.get("input_number.opti_einspeiseverguetung_ct"),
                grid_charging=settings.get("input_boolean.opti_prognose_netzladen") is True,
                ladder_mode=data.get("mode"),
                quantile=cfg.get("quantile", "p10"),
                # An explicit planner option wins over the battery model.
                wear_ct_kwh=_option_number(cfg, "wear_ct_kwh", self._battery_model.wear_ct_per_discharged_ac_kwh),
                eta_charge=self._battery_model.charge_efficiency,
                eta_discharge=self._battery_model.discharge_efficiency,
                terminal_factor=_option_number(cfg, "terminal_factor", 0.8),
                min_benefit_ct=_option_number(cfg, "min_benefit_ct", 0.0),
                grid_check_quantile=cfg.get("grid_check_quantile"),
            )
            if enabled:
                check = await self.hass.async_add_executor_job(
                    self._planner.evaluate, now, dt_util.DEFAULT_TIME_ZONE, inputs)
                check["totals"] = self._planner.observe(now, dt_util.DEFAULT_TIME_ZONE, check)
            else:
                self._planner.interrupt()
                check = {"status": "disabled", "detail": "entity_disabled", "observation_only": True,
                         "controls_battery": False}
            try:
                periods = price_periods(inputs.price_series, now, dt_util.DEFAULT_TIME_ZONE)
                if self._planner_history.record(now, pv_hourly, periods):
                    self._history_store.async_delay_save(self._planner_history.snapshot, HISTORY_SAVE_DELAY)
            except Exception as err:  # noqa: BLE001 - history is diagnosis only
                _LOGGER.debug("Planner history not recorded: %s", type(err).__name__)
                self._planner_history.error = type(err).__name__
            check["forecast_history"] = self._planner_history.summary()
        except Exception as err:  # Observation only; never affects the decision.
            _LOGGER.debug("Planner shadow unavailable: %s", type(err).__name__)
            self._planner.interrupt()
            check = {"status": "error", "observation_only": True, "controls_battery": False}
        states[key] = check.get("verdict", "unknown") if check.get("status") == "ready" else "unknown"
        attributes[key] = check

    def _update_arbitrage_report(self, data, now, options, arbitrage_hold, safety_reason):
        """Describe the hold decision without changing it."""
        demand_cfg = options.get("demand_forecast", {})
        demand_comparison_enabled = isinstance(demand_cfg, dict) and demand_cfg.get("enabled") is True
        arbitrage_cfg = options.get("arbitrage_estimate", {})
        try:
            terminal_context = None
            if demand_comparison_enabled:
                demand_report = data.get("demand_forecast", {})
                terminal_context = {
                    "forecast_slots": demand_report.get("forecast_slots"),
                    "price_series": data["attributes"].get("sensor.opti_price_series"),
                    "now": now,
                    "timezone": dt_util.DEFAULT_TIME_ZONE,
                    "refill_from": demand_report.get("pv_cover_from"),
                    "battery_capacity_kwh": finite(
                        data["states"].get("sensor.opti_battery_capacity_kwh")
                    ),
                    "current_soc": finite(data["states"].get("sensor.opti_soc")),
                    "minimum_soc": finite(self.settings.get("input_number.minsoc")),
                    "maximum_soc": finite(self.settings.get("input_number.maxsoc")),
                    "profile_ready": (
                        demand_report.get("profile_ready") is True
                        and demand_report.get("status") == "ready"
                    ),
                }
            data["arbitrage_estimate"] = build_arbitrage_estimate(
                arbitrage_cfg,
                finite(data["states"].get("sensor.opti_price_current_ct_kwh")),
                strategy_enabled=self.strategy_enabled,
                terminal_context=terminal_context,
            )
            would_control = (
                arbitrage_hold.get("hold_controls_battery") is True
                and data["decision_id"] == "arbitrage_hold"
            )
            if data["arbitrage_estimate"].get("status") not in (
                "not_configured", "invalid", "strategy_disabled"
            ):
                # Own error boundary: the hold logic reads this report back next cycle.
                try:
                    check = precharge_spread_check(
                        data["arbitrage_estimate"],
                        data["states"],
                        data["attributes"],
                        self.settings,
                        data["decision_id"],
                    )
                    comparison = {
                        **check,
                        "totals": self._precharge_comparison.observe(
                            now, check, precharge_assumptions(arbitrage_cfg, self.settings)
                        ),
                    }
                except Exception as err:  # Observation only; never affects the hold report.
                    _LOGGER.debug("Precharge comparison unavailable: %s", type(err).__name__)
                    self._precharge_comparison.interrupt()
                    comparison = {"status": "error", "informational_only": True,
                                  "controls_battery": False}
                data["arbitrage_estimate"]["precharge_comparison"] = comparison
            data["arbitrage_estimate"].update(arbitrage_hold)
            data["arbitrage_estimate"].update(
                hold_status=(
                    "higher_priority"
                    if would_control and safety_reason is not None
                    else arbitrage_hold.get("hold_status")
                ),
                hold_would_control_battery=would_control,
                hold_controls_battery=bool(
                    would_control
                    and self.write_enabled
                    and not self.shadow_mode
                    and safety_reason is None
                ),
                controls_battery=bool(
                    would_control
                    and self.write_enabled
                    and not self.shadow_mode
                    and safety_reason is None
                ),
                informational_only=not bool(arbitrage_hold.get("hold_enabled")),
            )
        except Exception as err:  # A display-only estimate must never interrupt control.
            _LOGGER.debug("Arbitrage estimate unavailable: %s", type(err).__name__)
            data["arbitrage_estimate"] = {
                "status": "error", "informational_only": True, "controls_battery": False
            }

    async def _async_update_observations(self, data, now, options):
        """Keep source comparison and the optional shadow journal off the command path."""
        try:
            probe = getattr(self.device, "last_probe_registers", {})
            data["source_observation"] = self._source_observation.update(
                now, self._measurements, self.hass.states, options, self._online,
                data["source_errors"], data["connection_status"], probe if isinstance(probe, dict) else {})
        except Exception as err:
            _LOGGER.debug("Source observation unavailable: %s", type(err).__name__)
            data["source_observation"] = {"status":"error", "controls_battery":False}
        data.update(self._journal_status())

    def _journal_status(self) -> dict:
        summary = {key: value for key, value in self._shadow_snapshot.items()
                   if key not in ("settings", "reference_entity")}
        if self.shadow_mode:
            attempts = getattr(self.device, "blocked_write_attempts", 0)
            summary["blocked_write_attempts"] = attempts if isinstance(attempts, int) else 0
        return {"shadow_status": self._shadow_snapshot["status"], "shadow_summary": summary}

    def _journal_sample(self, data: dict) -> MappingProxyType:
        """Freeze exactly what the recorder reads; later result changes cannot leak in."""
        sample = {key: deepcopy(data[key]) for key in _JOURNAL_SAMPLE_KEYS if key in data}
        states = data.get("states")
        states = states if isinstance(states, dict) else {}
        sample["states"] = {f"sensor.opti_{key}": deepcopy(states.get(f"sensor.opti_{key}"))
                            for key in MEASUREMENTS}
        report = data.get("demand_forecast")
        if isinstance(report, dict):
            sample["demand_forecast"] = {
                key: deepcopy(report[key])
                for key in (*DEMAND_FIELDS, "strategy_comparison") if key in report
            }
        sample["entry_shadow_mode"] = self.shadow_mode
        return MappingProxyType(sample)

    @callback
    def _schedule_journal_record(self, data: dict, now: datetime) -> asyncio.Task | None:
        """Hand one frozen sample to the single journal writer; skip it while that is busy.

        A skipped sample is simply absent: the recorder derives gaps from real
        sample times, so a slow disk shows up as a gap instead of queued rows.
        """
        if self._stopping or self._journal_jobs or self._shadow_snapshot.get("status") != "running":
            return None
        reference_id = self._shadow_snapshot.get("reference_entity", "")
        reference = self.hass.states.get(reference_id) if reference_id else None
        return self._start_journal_job(
            self._async_journal_record_job(self._journal_sample(data), dict(self.settings),
                                           reference.state if reference else None, now),
            "Opti journal sample")

    @callback
    def _start_journal_job(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Run journal work in a task nobody cancels, so its lock outlives any caller.

        An HA background task owns it for unload/stop tracking; cancelling that
        owner only defers until the executor work has really finished.
        """
        job = self.hass.loop.create_task(coro, name=name)
        self._journal_jobs.add(job)
        job.add_done_callback(self._journal_jobs.discard)
        owner = self.entry.async_create_background_task(
            self.hass, self._async_own_journal_job(job), name, eager_start=False)
        self._journal_owners.add(owner)
        owner.add_done_callback(self._journal_owners.discard)
        return job

    async def _async_own_journal_job(self, job: asyncio.Task) -> None:
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                # Defer HA's cancellation: releasing now would abandon a writing thread.
                if (task := asyncio.current_task()) is not None:
                    task.uncancel()
            except Exception:  # Reported to the awaiting caller; only mark it retrieved.
                pass
        if not job.cancelled():
            job.exception()

    async def _async_journal_idle(self) -> None:
        """Wait until every owned journal job and its HA owner have finished."""
        while pending := {*self._journal_jobs, *self._journal_owners}:
            await asyncio.wait(pending)

    async def _async_journal_record_job(self, sample, settings, reference, now) -> None:
        """Append passive diagnostics after the control result and price status are final."""
        async with self._journal_lock:
            if self._stopping or self._shadow.state.get("status") != "running":
                return
            try:
                self._shadow_snapshot = await self.hass.async_add_executor_job(
                    self._shadow.record, now, sample, settings, reference, MODES)
                if self._shadow_snapshot.get("status") == "completed":
                    # A crash within the delayed save would resume the run.
                    await self._store.async_save(self._stored_data())
            except Exception:  # Journal diagnostics must not interrupt active control.
                self._shadow.state["status"] = "error"
                self._shadow.state["error"] = "journal_write_failed"
                self._shadow_snapshot = self._shadow.snapshot()
        self._publish_journal_status()

    async def async_import_demand_history(self):
        """Manual import into observer only, never hold the writer lock over I/O."""
        from datetime import datetime
        from functools import partial
        from homeassistant.components.recorder import get_instance
        from .demand_history import read_recorder
        cfg = self.entry.options.get("demand_forecast", {})
        if self._stopping:
            raise translated(HomeAssistantError, "integration_stopping")
        if cfg.get("enabled") is not True:
            raise translated(ServiceValidationError, "history_import_unavailable")
        if self._history_import_running:
            raise translated(HomeAssistantError, "history_import_running")
        if cfg.get("sources", {}).get("heat_power"):
            raise translated(ServiceValidationError, "history_import_whole_house")
        house = cfg.get("history_house")
        if not house and self.entry.options.get("plant_mode", "legacy") == "legacy":
            house = self.entry.options.get("sources", {}).get("house_consumption")
        if not house:
            raise translated(ServiceValidationError, "history_import_sensor_scope")
        now = dt_util.utcnow()
        timezone = dt_util.DEFAULT_TIME_ZONE
        today = now.astimezone(timezone).date()
        first_online = min((key[:10] for key in self._demand_forecast.cells), default=today.isoformat())
        end_day = min(today, datetime.fromisoformat(first_online).date())
        start = datetime.combine(today - timedelta(days=42), datetime.min.time(), timezone).astimezone(dt_util.UTC)
        end = datetime.combine(end_day, datetime.min.time(), timezone).astimezone(dt_util.UTC)
        if start >= end:
            raise translated(ServiceValidationError, "history_import_no_interval")
        binding = self._demand_forecast.history_binding(self._load_source_fingerprint, self.entry.options, timezone)
        sources = {**cfg.get("sources", {}), "house": house}
        if cfg.get("house_includes_ev") is True:
            ev_sources = ev_profile_sources(self.entry.options)
            if not ev_sources or house in ev_sources or cfg.get("sources", {}).get("heat_power") in ev_sources:
                raise translated(ServiceValidationError, "history_import_ev_sources")
            sources.update({f"ev{index}": entity for index, entity in enumerate(ev_sources, 1)})
        self._history_import_running = True
        commit = None
        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                partial(read_recorder, self.hass, sources, start, end))
            # Once accepted, the transaction outlives its caller. Reuse the
            # owned persistence-job lifetime, without taking the journal lock.
            commit = self._start_journal_job(
                self._async_commit_demand_history(rows, binding, now), "Opti history import")
            await asyncio.shield(commit)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise translated(HomeAssistantError, "history_import_failed") from err
        finally:
            if commit is None:
                self._history_import_running = False

    async def _async_commit_demand_history(self, rows, binding, now) -> None:
        """Own stage/save/publish until the real write ends, even if the caller leaves."""
        from .demand_history import HistoricalPrior

        try:
            async with self._update_lock:
                # Entry unload/options changes while Recorder runs invalidate the result.
                if self._stopping or binding != self._demand_forecast.history_binding(
                        self._load_source_fingerprint, self.entry.options, dt_util.DEFAULT_TIME_ZONE):
                    raise translated(HomeAssistantError, "history_import_config_changed")
                candidate = HistoricalPrior()
                candidate.replace(rows, binding, now)
                self._pending_history = candidate
            # Keep the previous profile active while the disk is busy. Neither
            # the device lock nor a coordinator refresh waits for this save.
            await self._store.async_save(self._stored_data())
            async with self._update_lock:
                if self._stopping or binding != self._demand_forecast.history_binding(
                        self._load_source_fingerprint, self.entry.options, dt_util.DEFAULT_TIME_ZONE):
                    raise translated(HomeAssistantError, "history_import_config_changed")
                self._demand_forecast.history = candidate
        finally:
            if self._pending_history is not None:
                self._pending_history = None
                # Commit the current snapshot, or repair an invalidated import.
                # Store may coalesce this with a scheduled delayed save.
                self._store.async_delay_save(self._stored_data, 0)
            self._history_import_running = False
        # Publish diagnostics without scheduling an extra actuator update.
        if self.data is not None:
            self.async_set_updated_data({**self.data, "demand_forecast": {
                **self.data.get("demand_forecast", {}),
                "historical_prior": self._demand_forecast.history.summary()}})

    async def async_start_shadow(self) -> None:
        if not self.shadow_mode or not self._online or self._stopping:
            raise translated(HomeAssistantError, "shadow_requires_connection")
        await self._async_start_journal(refresh=True)

    async def async_start_comparison(self) -> None:
        if self.shadow_mode or not self._online or self._stopping:
            raise translated(HomeAssistantError, "comparison_requires_standard")
        await self._async_start_journal(refresh=False)

    async def _async_run_journal_lifecycle(
        self, job: Callable[[str], Coroutine[Any, Any, None]], label: str, name: str
    ) -> None:
        """Accept at most one start/stop at a time; once accepted it runs exactly once.

        Neither the device lock nor the caller's task is involved: a cancelled
        caller leaves the owned job to finish (or be rejected) on its own.
        """
        if self._stopping:
            raise translated(HomeAssistantError, f"{label}_stopping")
        if self._journal_lifecycle is not None and not self._journal_lifecycle.done():
            raise translated(HomeAssistantError, f"{label}_busy")
        accepted = self._journal_lifecycle = self._start_journal_job(job(label), name)
        await asyncio.shield(accepted)

    async def _async_start_journal(self, *, refresh: bool) -> None:
        label = "shadow_start" if self.shadow_mode else "comparison_start"
        await self._async_run_journal_lifecycle(
            self._async_journal_start_job, label, "Opti journal start")
        if refresh:
            await self.async_refresh()

    async def _async_journal_start_job(self, label: str) -> None:
        async with self._journal_lock:
            if self._stopping:  # Also rejects requests queued before the unload.
                raise translated(HomeAssistantError, f"{label}_stopping")
            now = dt_util.utcnow()
            settings = dict(self.settings)
            reference = self.entry.options.get("shadow_reference_mode", "")
            def start():
                version = json.loads(Path(__file__).with_name("manifest.json").read_text())["version"]
                return self._shadow.start(now, settings, reference, version)
            try:
                self._shadow_snapshot = await self.hass.async_add_executor_job(start)
            except (ValueError, OSError) as err:
                raise translated(HomeAssistantError, f"{label}_failed") from err
            await self._store.async_save(self._stored_data())
        self._publish_journal_status()

    async def async_stop_shadow(self) -> None:
        if self._stopping:
            raise translated(HomeAssistantError, "integration_stopping")
        if not self.shadow_mode:
            raise translated(ServiceValidationError, "no_shadow_entry")
        await self._async_stop_journal(refresh=True)

    async def async_stop_comparison(self) -> None:
        if self._stopping:
            raise translated(HomeAssistantError, "integration_stopping")
        if self.shadow_mode:
            raise translated(ServiceValidationError, "no_standard_entry")
        await self._async_stop_journal(refresh=False)

    async def _async_stop_journal(self, *, refresh: bool) -> None:
        label = "shadow_stop" if self.shadow_mode else "comparison_stop"
        await self._async_run_journal_lifecycle(
            self._async_journal_stop_job, label, "Opti journal stop")
        if refresh:
            await self.async_refresh()

    async def _async_journal_stop_job(self, label: str) -> None:
        async with self._journal_lock:
            if self._stopping:  # Also rejects requests queued before the unload.
                raise translated(HomeAssistantError, f"{label}_stopping")
            try:
                self._shadow_snapshot = await self.hass.async_add_executor_job(
                    self._shadow.stop, dt_util.utcnow())
            except OSError as err:
                raise translated(HomeAssistantError, f"{label}_failed") from err
            await self._store.async_save(self._stored_data())
        self._publish_journal_status()

    async def _async_journal_final_save_job(self) -> None:
        # Queued behind every accepted journal job, so it saves their final state.
        async with self._journal_lock:
            await self._store.async_save(self._stored_data())

    @callback
    def _publish_journal_status(self) -> None:
        if self.data is None:
            return
        self.data = {**self.data, **self._journal_status()}
        self.async_update_listeners()

    def _validate_ev_enable(self, settings: dict) -> None:
        if not settings.get("input_boolean.opti_ev_akku_pause"):
            return
        sources = self.entry.options.get("sources", {})
        if not any(sources.get(f"ev{n}_mode") and sources.get(f"ev{n}_charging") for n in (1, 2)):
            raise translated(ServiceValidationError, "ev_charger_required")

    async def async_set_setting(self, key: str, value: Any) -> None:
        async with self._update_lock:
            value = validate_setting(key, value, self.settings)
            if key == "input_boolean.opti_ev_akku_pause":
                self._validate_ev_enable({key: value})
            if key in ("input_number.minsoc", "input_number.maxsoc") and value != self.settings.get(key):
                self._reset_manual_soc_blocks()
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
            if any(validated.get(key) != self.settings.get(key)
                   for key in ("input_number.minsoc", "input_number.maxsoc")):
                self._reset_manual_soc_blocks()
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
            if self.manual_mode is not None:
                self._reset_manual_soc_blocks()
            self.manual_mode = None
            self._revision += 1
            await self.async_refresh()
            return
        if mode == "Strategie" and not self.strategy_enabled:
            raise translated(ServiceValidationError, "strategy_disabled")
        if mode != "Strategie" and mode not in self.supported_modes:
            raise translated(ServiceValidationError, "mode_unknown")
        new_manual = None if mode == "Strategie" else mode
        # A different selection starts a new manual session. Repeating the
        # same selection keeps the running hysteresis.
        if new_manual != self.manual_mode:
            self._reset_manual_soc_blocks()
        self.manual_mode = new_manual
        self._revision += 1
        await self.async_refresh()

    async def async_set_write_enabled(self, enabled: bool) -> None:
        if enabled and self._pause_pending and self._pause_binding != self._writer_binding:
            raise translated(HomeAssistantError, "pause_other_binding")
        if enabled and not self.strategy_enabled and self.manual_mode is None:
            raise translated(ServiceValidationError, "manual_mode_required")
        if enabled and not self.supported_modes:
            raise translated(ServiceValidationError, "backend_read_only")
        if enabled and self.shadow_mode:
            raise translated(ServiceValidationError, "shadow_write_forbidden")
        if enabled and not self.entry.options.get("single_writer_confirmed", False):
            raise translated(ServiceValidationError, "single_writer_unconfirmed")
        if enabled and (not self._online or self._stopping):
            raise translated(HomeAssistantError, "inverter_unreachable")
        if enabled and not self._identity.get("serial_number"):
            raise translated(HomeAssistantError, "identity_unconfirmed")
        self._revision += 1
        self._write_restore_blocked = None
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
            if not self.write_enabled:
                # Newly granted permission during an incident: the next failed
                # cycle tries the safety pause at once.
                self._failsafe_attempt_at = None
                self._failsafe_failed_attempts = 0
            self.write_enabled = True
            if getattr(type(self.device), "PHASED", False):
                # Saved before the first control call. Survives a crash or an
                # offline disable; only the original binding may finish it.
                self._pause_pending = True
                self._pause_binding = self._writer_binding
            self._last_signature = None
            self._applied_mode = None
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
        # This can run inside HA's serialized refresh or the shutdown device
        # lock. A stale saved debt only repeats a safety pause after restart;
        # saving its clearance must never stall control. Store may coalesce it.
        # Write activation and final shutdown still await their own saves
        # outside the device lock.
        self._store.async_delay_save(self._stored_data, 0)

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
        self._stop_watchdog()
        await self.alerts.async_stop()
        for task in (self._price_task, self._evcc_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
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
                    # The Huawei pause may defer itself; only a cleared debt confirms it.
                    self._unload_pause_confirmed = not self._pause_pending
                except Exception as err:
                    self._last_error = f"Pause beim Entladen der Integration nicht bestätigt: {type(err).__name__}"
            self._release_device()
        # Only now wait for journal writes: a slow disk never delays the pause above.
        await asyncio.shield(self._start_journal_job(
            self._async_journal_final_save_job(), "Opti final save"))
        await self._async_journal_idle()
