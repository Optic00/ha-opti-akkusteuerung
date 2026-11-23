"""Bounded history of control decisions for support diagnostics.

Records one segment per stretch of unchanged decision (effective mode,
requested mode, decision id, connection and source health). The reason text of
a segment is kept from its start and from its last cycle, so a reason that
carries changing numbers does not create a new segment every cycle.

Nothing in this module is a control input. Storage is limited to the last
24 hours and a fixed number of segments.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

VERSION = 1
MAX_AGE = timedelta(hours=24)
MAX_SEGMENTS = 200
MAX_REASON = 200
FAILED_RESULTS = ("failed", "superseded")

# Entity IDs outside this integration may name private devices or people.
_FOREIGN_ENTITY = re.compile(r"\b[a-z_]+\.(?!opti_)[a-z0-9_]+\b")


def _number(value: Any, digits: int) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(result, digits) if isfinite(result) else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _FOREIGN_ENTITY.sub("<entity>", value.strip())
    return text[:MAX_REASON] if text else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class DecisionLog:
    """Decision segments of the last 24 hours, oldest first."""

    def __init__(self) -> None:
        self._segments: list[dict[str, Any]] = []
        # After a restart the next cycle always opens a new segment.
        self._split_next = False

    @staticmethod
    def _key(data: Mapping[str, Any]) -> tuple:
        return (
            data.get("mode"),
            data.get("engine_requested_mode"),
            data.get("decision_id"),
            data.get("online") is True,
            bool(data.get("source_errors")),
            data.get("strategy_enabled") is True,
            data.get("write_enabled") is True,
        )

    def record(self, now: datetime, data: Mapping[str, Any]) -> None:
        """Extend the current segment or start a new one; then prune."""
        if not isinstance(data, Mapping):
            return
        states = data.get("states") if isinstance(data.get("states"), Mapping) else {}
        soc = _number(states.get("sensor.opti_soc"), 1)
        price = _number(states.get("sensor.opti_price_current_ct_kwh"), 2)
        reason = _text(data.get("reason"))
        result = data.get("command_result_this_update")
        stamp = now.isoformat()
        key = self._key(data)
        last = self._segments[-1] if self._segments else None
        if last is not None and not self._split_next and tuple(last["key"]) == key:
            last["until"] = stamp
            last["cycles"] += 1
            last["soc_last"] = soc
            last["price_ct_last"] = price
            if reason != last["reason"]:
                last["reason_last"] = reason
            else:
                last.pop("reason_last", None)
        else:
            last = {
                "key": list(key),
                "from": stamp,
                "until": stamp,
                "cycles": 1,
                "mode": key[0] if isinstance(key[0], str) else None,
                "requested_mode": key[1] if isinstance(key[1], str) else None,
                "decision_id": key[2] if isinstance(key[2], str) else None,
                "online": key[3],
                "source_errors": key[4],
                "strategy_enabled": key[5],
                "write_enabled": key[6],
                "reason": reason,
                "soc": soc,
                "soc_last": soc,
                "price_ct": price,
                "price_ct_last": price,
                "write_failures": 0,
            }
            if self._split_next:
                last["after_restart"] = True
            self._segments.append(last)
        self._split_next = False
        if result in FAILED_RESULTS:
            last["write_failures"] += 1
        self._prune(now)

    def _prune(self, now: datetime) -> None:
        cutoff = now - MAX_AGE
        self._segments = [
            segment for segment in self._segments
            if (until := _parse(segment.get("until"))) is not None and until >= cutoff
        ][-MAX_SEGMENTS:]

    def report(self, now: datetime) -> list[dict[str, Any]]:
        """Segments for diagnostics, without the internal comparison key."""
        self._prune(now)
        return [
            {name: value for name, value in segment.items() if name != "key"}
            for segment in deepcopy(self._segments)
        ]

    def snapshot(self) -> dict[str, Any]:
        return {"version": VERSION, "segments": deepcopy(self._segments)}

    def restore(self, saved: Any) -> None:
        """Accept only well-formed saved data; anything else starts empty."""
        self._segments = []
        self._split_next = True
        if not isinstance(saved, Mapping) or saved.get("version") != VERSION:
            return
        segments = saved.get("segments")
        if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
            return
        restored = []
        for segment in segments:
            if (not isinstance(segment, Mapping) or not isinstance(segment.get("key"), list)
                    or len(segment["key"]) != 7 or _parse(segment.get("from")) is None
                    or _parse(segment.get("until")) is None
                    or type(segment.get("cycles")) is not int or segment["cycles"] < 1
                    or type(segment.get("write_failures")) is not int
                    or segment["write_failures"] < 0):
                return
            restored.append(deepcopy(dict(segment)))
        self._segments = restored
