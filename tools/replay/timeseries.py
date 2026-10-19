"""Normalised hourly plant series shared by importer, simulator and metrics."""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo

PRICE_MEASURED = "measured"
PRICE_FALLBACK = "fallback"
PRICE_INTERPOLATED = "interpolated"
PRICE_MISSING = "missing"


@dataclass
class Hour:
    """One local hour of recorded plant energy. ``None`` marks a gap, never zero."""

    ts_local: dt.datetime
    pv_kwh: float | None = None
    load_kwh: float | None = None
    export_kwh: float | None = None
    import_kwh: float | None = None
    charge_kwh: float | None = None
    discharge_kwh: float | None = None
    soc: float | None = None
    soc_min: float | None = None
    soc_max: float | None = None
    price_ct: float | None = None
    price_src: str = PRICE_MISSING
    forecast_today_kwh: float | None = None
    forecast_tomorrow_kwh: float | None = None
    house_meter_kwh: float | None = None
    gaps: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when the simulator can run this hour (energy balance known)."""
        return self.pv_kwh is not None and self.load_kwh is not None

    @property
    def day(self) -> dt.date:
        return self.ts_local.date()


_FLOATS = tuple(
    f.name for f in fields(Hour) if f.name not in {"ts_local", "price_src", "gaps"}
)


def write_csv(hours: list[Hour], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts_local", *_FLOATS, "price_src", "gaps"])
        for hour in hours:
            writer.writerow([
                hour.ts_local.isoformat(),
                *("" if getattr(hour, name) is None else repr(getattr(hour, name)) for name in _FLOATS),
                hour.price_src,
                ";".join(hour.gaps),
            ])


def read_csv(path: Path, tz: str = "Europe/Berlin") -> list[Hour]:
    """Read a series; timestamps get a real zone so DST days keep 23/25 hours."""
    zone = ZoneInfo(tz)
    hours = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            values = {name: (float(row[name]) if row.get(name) else None) for name in _FLOATS}
            hours.append(Hour(
                ts_local=dt.datetime.fromisoformat(row["ts_local"]).astimezone(zone),
                price_src=row.get("price_src") or PRICE_MISSING,
                gaps=[item for item in (row.get("gaps") or "").split(";") if item],
                **values,
            ))
    return hours
