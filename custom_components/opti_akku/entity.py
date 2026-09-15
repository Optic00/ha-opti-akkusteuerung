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


def readable_name(key: str) -> str:
    """Turn a virtual entity id into a readable fallback name."""
    value = key.split(".", 1)[-1].replace("_", " ").strip()
    return value[:1].upper() + value[1:]


def coordinator_data(entity: OptiAkkuEntity) -> dict[str, Any]:
    return entity.coordinator.data or {}
