"""Small structural contract between device access and battery strategy.

Canonical sensor values are a compatibility boundary, not hardware register
names. Each backend owns command semantics, serialization and freshness checks.
A successfully sent command is not evidence of its physical effect.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol


class DeviceError(RuntimeError):
    """A device transaction cannot safely be completed."""


class PendingCommandError(DeviceError):
    """A phased command awaits readback or a newer target reconciliation."""

    confirmed = False


class StaleCommandError(DeviceError):
    """A new decision invalidated this transaction; cleanup may also have failed."""

    cleanup_failed = False


class DeviceAdapter(Protocol):
    """Device API used by the coordinator, with explicit command capabilities."""

    supported_modes: tuple[str, ...]
    supports_control_release: bool
    command_execution_basis: str
    setpoint_readback_capability: str
    setpoint_readback_limitation: str
    last_read_errors: dict[str, str]

    async def async_probe(self) -> dict[str, Any]:
        """Validate identity and return backend-specific device information."""
        ...

    async def async_read(self) -> dict[str, Any]:
        """Return canonical current measurements; missing values remain missing."""
        ...

    async def async_apply(
        self, mode: str, parameters: Mapping[str, float], is_current: Callable[[], bool]
    ) -> None:
        """Apply a supported mode while repeatedly respecting is_current."""
        ...
