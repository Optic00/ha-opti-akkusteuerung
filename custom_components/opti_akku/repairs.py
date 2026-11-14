"""Repair issues for states that need a user action and do not clear by themselves.

Operating incidents (connection, sources, writes, Huawei pause) stay
persistent notifications in alerts.py: they recover on their own. A repair
issue is only raised where the user has to change something:

- the device answers with a different serial number than the configured one,
- the Tibber selection no longer matches what Home Assistant provides,
- a saved settings change was rejected and discarded.

Issues are derived from the coordinator data on every cycle, so they open and
close with the state and need no stored bookkeeping. They are not persistent:
after a restart the next cycle raises them again if the state remains.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

DOCS = "https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/"
TIBBER_CONFIG_ERRORS = frozenset(
    {"tibber_home_mismatch", "tibber_entry_mismatch", "tibber_home_count", "tibber_config_entries"}
)


def _identity_changed(data: Mapping[str, Any]) -> bool:
    return data.get("identity_mismatch") is True


def _tibber_config(data: Mapping[str, Any]) -> bool:
    return data.get("price_provider_error") in TIBBER_CONFIG_ERRORS


def _settings_rejected(data: Mapping[str, Any]) -> bool:
    return data.get("settings_rejected") is True


ISSUES: dict[str, tuple[Callable[[Mapping[str, Any]], bool], ir.IssueSeverity, str]] = {
    "identity_changed": (_identity_changed, ir.IssueSeverity.ERROR, DOCS + "operation.md"),
    "tibber_config": (_tibber_config, ir.IssueSeverity.ERROR, "https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/PRICE_SOURCES.md"),
    "settings_rejected": (_settings_rejected, ir.IssueSeverity.WARNING, DOCS + "configuration.md"),
}


def issue_id(key: str, entry_id: str) -> str:
    return f"{key}_{entry_id}"


class RepairIssues:
    """Keep the repair issues of one entry in line with its coordinator data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self._open: set[str] = set()

    @callback
    def sync(self, data: Mapping[str, Any]) -> None:
        for key, (active, severity, url) in ISSUES.items():
            if active(data):
                if key not in self._open:
                    ir.async_create_issue(
                        self.hass, DOMAIN, issue_id(key, self.entry.entry_id),
                        is_fixable=False, is_persistent=False, severity=severity,
                        translation_key=key, learn_more_url=url,
                        translation_placeholders={"entry": self.entry.title},
                    )
                    self._open.add(key)
            else:
                # Also after a restart, when HA restored the issue inactive and
                # _open is still empty; deleting a missing issue is a no-op.
                ir.async_delete_issue(self.hass, DOMAIN, issue_id(key, self.entry.entry_id))
                self._open.discard(key)


@callback
def async_clear_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Drop every repair issue of an unloaded or removed entry."""
    for key in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(key, entry_id))
