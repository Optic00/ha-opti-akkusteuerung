"""Bounded history of control decisions for support diagnostics.

Records one segment per stretch of unchanged decision (effective mode,
requested mode, manual mode, decision id, connection and source health,
strategy and write release). The reason text of a segment is kept from its
start and from its last cycle, so a reason that carries changing numbers does
not create a new segment every cycle. A segment never spans more than one
hour, so pruning by its start keeps every stored value inside 24 hours.

Nothing in this module is a control input. Stored and exported segments are
always rebuilt from typed, size-limited fields.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

VERSION = 2
MAX_AGE = timedelta(hours=24)
MAX_SEGMENT = timedelta(hours=1)
MAX_SEGMENTS = 200
MAX_REASON = 200
MAX_LABEL = 64
MAX_CYCLES = 10_000

# Entity IDs may name private devices or people; IP addresses and host names
# may name hosts. The domain of an entity ID needs a letter, so decimals such
# as 3.5 stay; the object ID may be digits.
_ENTITY = re.compile(r"(?<![\w.-])(?![a-z]\.[a-z]\.?(?![\w-]|\.\w))(?=[a-z0-9_]*[a-z])[a-z0-9_]+\.[a-z0-9_]+(?![\w-]|\.\w)")
# Any run of hex digits, dots and colons with "::" or at least three colons is
# an IPv6 address in some notation (also with an embedded IPv4 part).
# A trailing colon does not count, so "um 14:35:15: ..." stays a time.
_IPV6 = re.compile(r"(?<![\w.])(?=[0-9a-f.:]*::|(?:[0-9a-f.]*:){3}[0-9a-f])[0-9a-f.:]+(?![\w:.])",
                   re.IGNORECASE)
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Dotted host names; the last label starts with a letter, so text such as 3.5kW
# stays, and abbreviations of two single letters (z.B., d.h., u.a.) are skipped.
_HOST = re.compile(r"(?<![\w.-])(?![a-z]\.[a-z]\.?(?![\w-]|\.\w))"
                   r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z][a-z0-9-]*(?![\w-]|\.\w)",
                   re.IGNORECASE)
# Plain text a mode name or decision id may hold after masking.
_LABEL = re.compile(r"[\w <>().,%+-]+")


def _mask(text: str) -> str:
    text = _ENTITY.sub("<entity>", text)
    text = _IPV4.sub("<host>", _IPV6.sub("<host>", text))
    return _HOST.sub("<host>", text)


_TEXT_FIELDS = ("mode", "requested_mode", "manual_mode", "decision_id")
_BOOL_FIELDS = ("online", "source_errors", "strategy_enabled", "write_enabled")
_SOC_FIELDS = ("soc", "soc_last")
_PRICE_FIELDS = ("price_ct", "price_ct_last")


def _number(value: Any, digits: int, low: float, high: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(result, digits) if isfinite(result) and low <= result <= high else None


def _label(value: Any) -> str | None:
    """A mode or decision id: short plain text, never an entity ID or host."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _mask(value.strip())[:MAX_LABEL]
    return text if _LABEL.fullmatch(text) else "<invalid>"


def _reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _mask(value.strip())
    return text[:MAX_REASON] if text else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _clean(segment: Any) -> dict[str, Any] | None:
    """Rebuild one segment from allowed, typed fields; None if malformed."""
    if not isinstance(segment, Mapping):
        return None
    start, until = _parse(segment.get("from")), _parse(segment.get("until"))
    cycles, failures = segment.get("cycles"), segment.get("write_failures")
    if (start is None or until is None or until < start or until - start > MAX_SEGMENT
            or type(cycles) is not int or not 1 <= cycles <= MAX_CYCLES
            or type(failures) is not int or not 0 <= failures <= cycles):
        return None
    clean: dict[str, Any] = {"from": start.isoformat(), "until": until.isoformat(),
                             "cycles": cycles, "write_failures": failures}
    for name in _TEXT_FIELDS:
        clean[name] = _label(segment.get(name))
    for name in _BOOL_FIELDS:
        clean[name] = segment.get(name) is True
    for name in _SOC_FIELDS:
        clean[name] = _number(segment.get(name), 1, 0, 100)
    for name in _PRICE_FIELDS:
        clean[name] = _number(segment.get(name), 2, -1000, 1000)
    clean["reason"] = _reason(segment.get("reason"))
    if (last := _reason(segment.get("reason_last"))) is not None and last != clean["reason"]:
        clean["reason_last"] = last
    for name in ("after_restart", "continued"):
        if segment.get(name) is True:
            clean[name] = True
    return clean


class DecisionLog:
    """Decision segments of the last 24 hours, oldest first."""

    def __init__(self) -> None:
        self._segments: list[dict[str, Any]] = []
        # After a restart the next cycle always opens a new segment.
        self._split_next = False

    @staticmethod
    def _fields(data: Mapping[str, Any]) -> dict[str, Any]:
        manual = data.get("manual_mode")
        return {
            "mode": _label(data.get("mode")),
            "requested_mode": _label(data.get("engine_requested_mode")),
            "manual_mode": _label(manual) if isinstance(manual, str) else None,
            "decision_id": _label(data.get("decision_id")),
            "online": data.get("online") is True,
            "source_errors": bool(data.get("source_errors")),
            "strategy_enabled": data.get("strategy_enabled") is True,
            "write_enabled": data.get("write_enabled") is True,
        }

    def record(self, now: datetime, data: Mapping[str, Any]) -> None:
        """Extend the current segment or start a new one; then prune."""
        if not isinstance(data, Mapping):
            return
        states = data.get("states") if isinstance(data.get("states"), Mapping) else {}
        soc = _number(states.get("sensor.opti_soc"), 1, 0, 100)
        price = _number(states.get("sensor.opti_price_current_ct_kwh"), 2, -1000, 1000)
        reason = _reason(data.get("reason"))
        # Every failed write counts; a superseded command is expected when
        # sources change during I/O and does not.
        failed = data.get("command_result_this_update") == "failed"
        fields = self._fields(data)
        clock_back = False
        if self._segments and (until := _parse(self._segments[-1]["until"])) is not None and now < until:
            # The clock moved backwards: history after "now" can no longer be
            # placed, so it is dropped and the list stays in chronological order.
            self._segments = [segment for segment in self._segments
                              if (start := _parse(segment["from"])) is not None and start <= now]
            for segment in self._segments:
                if (end := _parse(segment["until"])) is not None and end > now:
                    segment["until"] = now.isoformat()
            clock_back = True
        last = self._segments[-1] if self._segments else None
        same = last is not None and all(last[name] == value for name, value in fields.items())
        start = _parse(last["from"]) if last is not None else None
        too_long = start is not None and now - start >= MAX_SEGMENT
        if (same and not self._split_next and not too_long and not clock_back
                and last["cycles"] < MAX_CYCLES):
            last["until"] = now.isoformat()
            last["cycles"] += 1
            last["soc_last"] = soc
            last["price_ct_last"] = price
            if reason != last["reason"]:
                last["reason_last"] = reason
            else:
                last.pop("reason_last", None)
        else:
            last = {
                **fields, "from": now.isoformat(), "until": now.isoformat(), "cycles": 1,
                "reason": reason, "soc": soc, "soc_last": soc,
                "price_ct": price, "price_ct_last": price, "write_failures": 0,
            }
            if self._split_next:
                last["after_restart"] = True
            elif same:
                last["continued"] = True
            self._segments.append(last)
        self._split_next = False
        if failed:
            last["write_failures"] += 1
        self._prune(now)

    def _prune(self, now: datetime) -> None:
        cutoff = now - MAX_AGE
        self._segments = [
            segment for segment in self._segments
            if (start := _parse(segment.get("from"))) is not None and start >= cutoff
            # After the clock moved back by more than an hour, segments from
            # the "future" are dropped; small corrections keep them.
            and (until := _parse(segment.get("until"))) is not None
            and until <= now + MAX_SEGMENT
        ][-MAX_SEGMENTS:]

    def report(self, now: datetime) -> list[dict[str, Any]]:
        """Segments for diagnostics, rebuilt from allowed fields only."""
        self._prune(now)
        return [clean for segment in self._segments if (clean := _clean(segment)) is not None]

    def snapshot(self, now: datetime) -> dict[str, Any]:
        self._prune(now)
        return {"version": VERSION,
                "segments": [clean for segment in self._segments
                             if (clean := _clean(segment)) is not None]}

    def restore(self, saved: Any, now: datetime) -> None:
        """Rebuild saved segments field by field; malformed history starts empty."""
        self._segments = []
        if saved is None:
            return
        self._split_next = True
        if not isinstance(saved, Mapping) or saved.get("version") != VERSION:
            return
        segments = saved.get("segments")
        if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
            return
        restored = [_clean(segment) for segment in segments]
        if any(segment is None for segment in restored):
            return
        self._segments = [segment for segment in restored if segment is not None]
        self._prune(now)
