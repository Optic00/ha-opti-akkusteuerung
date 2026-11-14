"""Shared entities for Opti Akku."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME


class OptiAkkuEntity(CoordinatorEntity):
    """Base class for coordinator-backed entities."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        identity = (entry.runtime_data.data or {}).get("identity", {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{NAME} Shadow" if entry.runtime_data.shadow_mode else NAME,
            manufacturer=identity.get("manufacturer", "SMA"),
            model=identity.get("model") or entry.data.get("profile", "STP SE"),
            serial_number=identity.get("serial_number") or entry.data.get("serial_number"),
        )

    @property
    def available(self) -> bool:
        return super().available


# Strategy, source and diagnostic states with a translated name under
# entity.<platform>.state_<object id>. Other keys (new strategy entities
# before their translation lands) fall back to the metadata name.
TRANSLATED_STATE_KEYS = frozenset({
    "binary_sensor.opti_balancing_haltefenster_aktiv",
    "binary_sensor.opti_block_violation",
    "binary_sensor.opti_ev_lp1_schnell",
    "binary_sensor.opti_ev_lp2_schnell",
    "binary_sensor.opti_ev_schnellladung",
    "binary_sensor.opti_extreme_price_hold",
    "binary_sensor.opti_ladedeckel_aktiv",
    "binary_sensor.opti_minsoc_schutz_aktiv",
    "binary_sensor.opti_peak_horizont_lang",
    "binary_sensor.opti_peak_reserve_aktiv",
    "binary_sensor.opti_pv_reichtag",
    "binary_sensor.opti_pv_spitze",
    "binary_sensor.opti_ueberschuss_70_aktiv",
    "binary_sensor.opti_ueberschuss_ac_aktiv",
    "binary_sensor.opti_ueberschuss_veto_aktiv",
    "binary_sensor.opti_vorladen_pv_ueberschuss",
    "binary_sensor.opti_winter_charging_allowed",
    "binary_sensor.opti_write_stalled",
    "binary_sensor.opti_zellspannung_unter_reserve",
    "counter.opti_balancing_done_minuten",
    "counter.tage_seit_akku100",
    "input_datetime.akkusteuerung_modbus_letzter_schreibzeitpunkt",
    "input_datetime.opti_balancing_letzter_abschluss",
    "sensor.byd_zellspreizung_ruhe",
    "sensor.haus_stromverbrauch_60_min",
    "sensor.house_battery_load_30_mins",
    "sensor.opti_balancing_watchdog",
    "sensor.opti_battery_model",
    "sensor.opti_cell_voltage_min_v",
    "sensor.opti_clipping_forecast_kwh",
    "sensor.opti_engine_diagnostics",
    "sensor.opti_forecast_effective_remaining_kwh",
    "sensor.opti_forecast_house_24h_w",
    "sensor.opti_forecast_house_60min_w",
    "sensor.opti_forecast_remaining_load_profile_kwh",
    "sensor.opti_forecast_remaining_today_kwh",
    "sensor.opti_forecast_score",
    "sensor.opti_forecast_score_sonnentag",
    "sensor.opti_forecast_score_tomorrow",
    "sensor.opti_forecast_today_kwh",
    "sensor.opti_house_balance_w",
    "sensor.opti_house_consumption_24h_w",
    "sensor.opti_house_consumption_60min_w",
    "sensor.opti_inverter_status",
    "sensor.opti_mindestentladepreis_ct_kwh",
    "sensor.opti_negative_price_hold",
    "sensor.opti_peak_load_profile",
    "sensor.opti_peak_rank",
    "sensor.opti_peak_reserve_soc",
    "sensor.opti_price_level",
    "sensor.opti_price_series",
    "sensor.opti_pv_forecast_hours",
    "sensor.opti_runtime_h",
    "sensor.opti_strategie_vorschau",
})


def state_translation_key(key: str) -> str | None:
    """Translation key of a state entity, or None if it has no translated name."""
    if key not in TRANSLATED_STATE_KEYS:
        return None
    return "state_" + key.split(".", 1)[1].removeprefix("opti_")


def readable_name(key: str) -> str:
    """Turn a virtual entity id into a readable fallback name."""
    value = key.split(".", 1)[-1].replace("_", " ").strip()
    return value[:1].upper() + value[1:]


def coordinator_data(entity: OptiAkkuEntity) -> dict[str, Any]:
    return entity.coordinator.data or {}
