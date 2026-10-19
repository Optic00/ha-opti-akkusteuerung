"""Sun state for the engine: astral when available, otherwise a coarse table.

The engine reads ``sun.sun`` (above/below_horizon) and the ``next_rising`` /
``next_setting`` attributes. Without astral a fixed sunrise/sunset table by
month approximates central Germany; the summary of a run records which path
was used.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

try:  # astral ships with Home Assistant; the harness also runs without it.
    from astral import LocationInfo
    from astral.sun import sun as _astral_sun
except ImportError:  # pragma: no cover - depends on the environment
    LocationInfo = None  # type: ignore[assignment]
    _astral_sun = None  # type: ignore[assignment]

DEFAULT_LATITUDE = 51.0
DEFAULT_LONGITUDE = 10.0
# Approximate local sunrise/sunset (hour, minute) by month for ~51 N, 10 E.
_TABLE = {
    1: ((8, 15), (16, 45)), 2: ((7, 30), (17, 40)), 3: ((6, 30), (18, 30)),
    4: ((6, 20), (20, 15)), 5: ((5, 25), (21, 0)), 6: ((5, 0), (21, 30)),
    7: ((5, 15), (21, 20)), 8: ((6, 0), (20, 30)), 9: ((6, 50), (19, 25)),
    10: ((7, 40), (18, 20)), 11: ((7, 35), (16, 30)), 12: ((8, 10), (16, 10)),
}


@lru_cache(maxsize=4096)
def _events(latitude: float, longitude: float, day: dt.date, tzinfo: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    if _astral_sun is not None:
        location = LocationInfo("replay", "", "UTC", latitude, longitude)
        result = _astral_sun(location.observer, date=day, tzinfo=tzinfo)
        return result["sunrise"], result["sunset"]
    rise, set_ = _TABLE[day.month]
    base = dt.datetime.combine(day, dt.time(), tzinfo=tzinfo)
    return base.replace(hour=rise[0], minute=rise[1]), base.replace(hour=set_[0], minute=set_[1])


class SunModel:
    def __init__(self, latitude: float = DEFAULT_LATITUDE, longitude: float = DEFAULT_LONGITUDE) -> None:
        self.latitude, self.longitude = latitude, longitude
        self.source = "astral" if _astral_sun is not None else "table"

    def events(self, day: dt.date, tzinfo: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
        """Local sunrise and sunset for ``day``."""
        return _events(self.latitude, self.longitude, day, tzinfo)

    def state(self, now: dt.datetime) -> tuple[str, dict[str, str]]:
        """``sun.sun`` state and attributes as the coordinator would supply them."""
        day = now.date()
        sunrise, sunset = self.events(day, now.tzinfo)
        above = sunrise <= now <= sunset
        if now < sunrise:
            next_rising, next_setting = sunrise, sunset
        elif now <= sunset:
            next_rising = self.events(day + dt.timedelta(days=1), now.tzinfo)[0]
            next_setting = sunset
        else:
            next_rising, next_setting = self.events(day + dt.timedelta(days=1), now.tzinfo)
        return ("above_horizon" if above else "below_horizon"), {
            "next_rising": next_rising.isoformat(),
            "next_setting": next_setting.isoformat(),
        }
