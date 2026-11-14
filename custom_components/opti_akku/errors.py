"""Translated exceptions; texts live under "exceptions" in strings.json."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


def translated[E: HomeAssistantError](cls: type[E], key: str, **placeholders: object) -> E:
    """Build ``cls`` with a translation key of this integration.

    ServiceValidationError marks wrong user input (no stack trace in the log),
    HomeAssistantError a runtime state, ConfigEntryError a broken entry.
    """
    error: E = cls(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={name: str(value) for name, value in placeholders.items()} or None,
    )
    return error
