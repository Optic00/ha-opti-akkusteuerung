"""Optional charge curve: SoC-dependent ceiling for the charge power (#205).

Up to five points of state of charge (%) and charge power (W). From a
point's SoC upwards, its power is the ceiling until the next point; below
the first point the curve does not limit. The curve is only an additional
ceiling: the coordinator takes the minimum with every other limit, in every
mode, after all strategy and downstream adjustments. It never raises a
value.

Powers must not rise with the SoC. A rising ceiling towards full charge
contradicts the purpose of a taper and is almost always a typo (2500 for
250 W). Equal powers are allowed.

There is no minimum power: a point with 0 W blocks charging from that SoC.
Small values are written as whole watts; whether the inverter and BMS hold
such a small power is device-specific and not verified here.
"""
from __future__ import annotations

from itertools import pairwise
import math
from typing import Any

MAX_POINTS = 5
# Same range as the maximum charge power setting.
MAX_POWER_W = 10000


class ChargeCurveError(ValueError):
    """A data-free error code for config flow and diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _whole(value: Any) -> int | None:
    """A finite whole number (selectors deliver 85.0), never a bool or text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value != int(value):
        return None
    return int(value)


def parse_curve(raw: Any) -> tuple[tuple[int, int], ...]:
    """Validate configured points; return (soc, power_w) sorted by SoC.

    An absent or empty curve is valid and means: no additional limit.
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ChargeCurveError("charge_curve_invalid")
    if len(raw) > MAX_POINTS:
        raise ChargeCurveError("charge_curve_too_many")
    points = []
    for item in raw:
        if not isinstance(item, dict):
            raise ChargeCurveError("charge_curve_invalid")
        soc = _whole(item.get("soc"))
        power = _whole(item.get("power_w"))
        if soc is None or not 0 <= soc <= 100:
            raise ChargeCurveError("charge_curve_soc")
        if power is None or not 0 <= power <= MAX_POWER_W:
            raise ChargeCurveError("charge_curve_power")
        points.append((soc, power))
    points.sort()
    if len({soc for soc, _ in points}) != len(points):
        raise ChargeCurveError("charge_curve_duplicate")
    if any(later > earlier for (_, earlier), (_, later) in pairwise(points)):
        raise ChargeCurveError("charge_curve_rising")
    return tuple(points)


def normalized_curve(raw: Any) -> list[dict[str, int]]:
    """The stored form: sorted points with whole numbers."""
    return [{"soc": soc, "power_w": power} for soc, power in parse_curve(raw)]


def curve_limit(curve: tuple[tuple[int, int], ...], soc: float | None) -> tuple[int, int | None] | None:
    """Ceiling for this SoC as (power_w, point SoC), or None without a limit.

    An unknown SoC takes the smallest power of the curve (fail-closed); the
    point SoC is then None.
    """
    if not curve:
        return None
    if soc is None or not math.isfinite(soc):
        return min(power for _, power in curve), None
    applicable = [(power, start) for start, power in curve if soc >= start]
    return applicable[-1] if applicable else None


def format_curve(raw: Any) -> str:
    """Short summary for diagnostics; never raises."""
    try:
        points = parse_curve(raw)
    except ChargeCurveError:
        return "ungültig"
    if not points:
        return "—"
    return ", ".join(f"ab {soc} % {power} W" for soc, power in points)
