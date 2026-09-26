"""Bounded, independent health notifications; never control the inverter."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)
PRICE_STARTUP_GRACE_SECONDS = 360
PRICE_SOURCE_KEYS = {"price_current", "price_series"}
# Long enough for a deliberate short toggle, short enough to catch a night.
CONTROL_INACTIVE_SECONDS = 900
STALE_SOURCE_ALERT_SECONDS = 180
MAX_SOURCE_DETAILS = 5
MESSAGES = {
    "pause": ("Huawei-Pause nicht bestätigt. Geräteeinstellungen können weiterwirken; automatische Nachholung nur bei unveränderter Bindung und Freigabe.", "Huawei pause is unconfirmed. Device settings may remain active; automatic retry requires unchanged binding and permission."),
    "connection": ("Wechselrichter nicht erreichbar oder noch nicht betriebsbereit. Gerätebereitschaft und Verbindung prüfen: bei SMA die Modbus-Einstellungen, bei Huawei die Huawei-Solar-Integration.", "Inverter offline or not yet ready. Check device readiness and connection: Modbus settings for SMA, or the Huawei Solar integration for Huawei."),
    "block": ("Lade-/Entladesperre wird verletzt", "Charge/discharge restriction violated"),
    "write": ("Schreibvorgang fehlgeschlagen oder seit über 240 Sekunden nicht bestätigt", "Write failed or not confirmed for more than 240 seconds"),
    "sources": ("Benötigte Eingangsdaten fehlen oder sind ungültig. Am Opti-Akku-Gerät Quellenfehler prüfen, dann den betroffenen Sensor und seine Zuordnung unter Konfigurieren kontrollieren.", "Input data is missing or invalid. Check Source errors on the Opti Akku device, then the affected sensor and its mapping under Configure."),
    "control": ("Strategie aktiv, aber Schreibfreigabe aus: Opti Akku steuert den Akku nicht. Bei einem absichtlich lesenden 24-Stunden-Vergleich ist das erwartet; die Strategie dafür eingeschaltet lassen. Andernfalls die Schreibfreigabe prüfen.", "Strategy active but writes disabled: Opti Akku is not controlling the battery. This is expected during an intentional read-only 24-hour comparison; keep the strategy enabled for that comparison. Otherwise, check write permission."),
    "prices": ("Tibber-Preisabruf fehlgeschlagen; Cache gilt nur bis zu seiner ursprünglichen Ablaufzeit", "Tibber price fetch failed; cached prices keep their original expiry"),
}


class HealthAlerts:
    """One notification per incident, recovery debounce and bounded push delivery."""

    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self._since = {}
        self._clear_since = {}
        self._active = set()
        self._last_push = {}
        self._task = None
        self._stopped = False
        self._pending = None
        self.delivery_error = None
        self._initial_sync = True
        self._started_at = None
        self._prices_ready = False
        self._service = entry.options.get("notification_service", "")
        self._active_source_details = ""
        self._source_details_updated_at = None
        self._source_stale_only = None

    def _text(self, key):
        return MESSAGES[key][0 if self.hass.config.language == "de" else 1]

    def _source_details(self, errors):
        """Identify failing configured entities without adding measurements to alerts."""
        if not isinstance(errors, Mapping):
            return ""
        sources = self.entry.options.get("sources", {})
        if not isinstance(sources, Mapping):
            sources = {}
        details = []
        for raw_key in sorted(key for key in errors if isinstance(key, str)):
            key = raw_key.removeprefix("plant:")
            entity_id = sources.get(key)
            label = f"{key}: {entity_id}" if isinstance(entity_id, str) and entity_id else key
            reason = errors[raw_key]
            if isinstance(reason, str) and reason:
                label += f" ({reason})"
            details.append(label.replace("\n", " ").replace("\r", " "))
        visible = ", ".join(details[:MAX_SOURCE_DETAILS])
        remaining = len(details) - MAX_SOURCE_DETAILS
        if remaining > 0:
            visible += f" (+{remaining} {'weitere' if self.hass.config.language == 'de' else 'more'})"
        return visible

    def _message(self, key):
        message = self._text(key)
        if key == "sources" and self._active_source_details:
            label = "Betroffen" if self.hass.config.language == "de" else "Affected"
            message += f" {label}: {self._active_source_details}."
        return message

    @callback
    def update(self, data):
        if self._stopped:
            return
        service = self.entry.options.get("notification_service", "")
        if service != self._service:
            self.delivery_error = None
            self._service = service
        now = dt_util.utcnow()
        if self._started_at is None:
            self._started_at = now
        if data.get("price_last_success") is not None:
            self._prices_ready = True
        raw_source_errors = data.get("source_errors", {})
        source_errors = raw_source_errors if isinstance(raw_source_errors, Mapping) else {}
        startup_grace = (self.entry.options.get("price_provider") == "tibber"
            and not self._prices_ready
            and (now - self._started_at).total_seconds() < PRICE_STARTUP_GRACE_SECONDS)
        data["price_status"] = ("loading" if startup_grace else "error") if (
            data.get("price_provider_error") or PRICE_SOURCE_KEYS.intersection(source_errors)
            or (self.entry.options.get("price_provider") == "tibber" and not self._prices_ready)
        ) else "ready"
        if data.get("strategy_enabled") is False:
            data["price_status"] = "not_used"
        if self.entry.data.get("shadow_mode"):
            return
        states = data.get("states", {})
        writing = data.get("write_enabled", False)
        recovering = data.get("connection_status", {}).get("status") in ("offline", "recovering", "not_ready")
        problems = {
            "pause": data.get("pause_pending") is True,
            "connection": not data.get("online", False) or recovering,
            "block": writing and states.get("binary_sensor.opti_block_violation") == "on",
            "write": writing and (
                states.get("binary_sensor.opti_write_stalled") == "on"
                or (str(data.get("last_error", "")).startswith("Schreibvorgang nicht bestätigt")
                    and not (recovering and "InverterNotReadyError" in str(data.get("last_error", "")) ))
            ),
            "sources": bool(source_errors),
            "prices": bool(data.get("price_provider_error")),
            "control": data.get("control_inactive") is True,
        }
        changed = False
        new = []
        for key, problem in problems.items():
            if problem:
                self._clear_since.pop(key, None)
                if key == "sources" and key not in self._active:
                    stale_only = all(reason == "missing_or_stale" for reason in source_errors.values())
                    if stale_only != self._source_stale_only:
                        self._since[key] = now
                    self._source_stale_only = stale_only
                self._since.setdefault(key, now)
                # Keep the incident onset: at six minutes the existing 60s
                # debounce is already satisfied, not started afresh.
                price_only = key == "prices" or (key == "sources"
                    and set(source_errors) <= PRICE_SOURCE_KEYS)
                if startup_grace and price_only:
                    continue
                delay = 0 if key in ("block", "write", "pause") else CONTROL_INACTIVE_SECONDS if key == "control" else 60
                # A rounded sensor may briefly exceed its age limit while
                # upstream data is healthy. The source error sensor stays live.
                if key == "sources" and self._source_stale_only:
                    delay = STALE_SOURCE_ALERT_SECONDS
                if key not in self._active and (now - self._since[key]).total_seconds() >= delay:
                    self._active.add(key)
                    new.append(key)
                    changed = True
            else:
                self._since.pop(key, None)
                if key == "sources":
                    self._source_stale_only = None
                if key in self._active:
                    self._clear_since.setdefault(key, now)
                    if (now - self._clear_since[key]).total_seconds() >= 60:
                        self._active.remove(key)
                        changed = True
        if self._initial_sync and not any(problems.values()):
            changed = True
            self._initial_sync = False
        if "sources" in self._active:
            visible_errors = source_errors
            if startup_grace and isinstance(visible_errors, Mapping):
                visible_errors = {
                    key: value for key, value in visible_errors.items() if key not in PRICE_SOURCE_KEYS
                }
            details = self._source_details(visible_errors)
            if (details and details != self._active_source_details
                    and (self._source_details_updated_at is None
                         or (now - self._source_details_updated_at).total_seconds() >= 60)):
                self._active_source_details = details
                self._source_details_updated_at = now
                changed = True
        else:
            self._active_source_details = ""
            self._source_details_updated_at = None
        if not changed:
            return
        self._initial_sync = False
        notification_id = f"opti_akku_health_{self.entry.entry_id}"
        if self._active:
            message = "\n".join(f"- {self._message(key)}" for key in sorted(self._active))
            persistent_notification.async_create(self.hass, message, title=self.entry.title,
                                                 notification_id=notification_id)
        else:
            persistent_notification.async_dismiss(self.hass, notification_id)
        due = [key for key in new if key not in self._last_push or now - self._last_push[key] >= timedelta(minutes=5)]
        if due:
            for key in due:
                self._last_push[key] = now
            self._queue("\n".join(self._message(key) for key in sorted(self._active)))

    @callback
    def _queue(self, message):
        if not self.entry.options.get("notification_service"):
            return
        self._pending = message
        if self._task is None or self._task.done():
            self._task = self.entry.async_create_background_task(self.hass, self._deliver_pending(),
                                                                 "Opti health notification", eager_start=False)

    async def _deliver_pending(self):
        while self._pending is not None and not self._stopped:
            message, self._pending = self._pending, None
            await self._deliver(message)

    async def _deliver(self, message):
        service = self.entry.options.get("notification_service", "")
        if not service:
            self.delivery_error = None
            return
        # Only explicit notify services, never arbitrary HA actions.
        domain, _, name = service.partition(".")
        if domain != "notify" or not name or name in ("send_message", "persistent_notification"):
            self.delivery_error = "invalid_notification_service"
            return
        try:
            async with asyncio.timeout(10):
                await self.hass.services.async_call("notify", name,
                    {"title": self.entry.title, "message": message}, blocking=True)
        except Exception as err:
            if service == self.entry.options.get("notification_service", ""):
                self.delivery_error = "notification_delivery_failed"
            _LOGGER.warning("Opti notification delivery failed: %s", type(err).__name__)
        else:
            if service == self.entry.options.get("notification_service", ""):
                self.delivery_error = None

    async def async_test(self):
        message = ("Testmeldung: Die Opti-Akku-Alarmierung ist erreichbar. Dies ist kein Gerätefehler."
                   if self.hass.config.language == "de" else
                   "Test: Opti Akku notifications are reachable. This is not a device fault.")
        persistent_notification.async_create(self.hass, message, title=self.entry.title,
                                             notification_id=f"opti_akku_test_{self.entry.entry_id}")
        await self._deliver(message)
        if self.delivery_error:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(self.delivery_error)

    async def async_stop(self):
        self._stopped = True
        persistent_notification.async_dismiss(self.hass, f"opti_akku_health_{self.entry.entry_id}")
        persistent_notification.async_dismiss(self.hass, f"opti_akku_test_{self.entry.entry_id}")
        self._pending = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
