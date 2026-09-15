"""Recorder priors, separate from measured coverage and forward accuracy."""

from datetime import UTC, datetime, timedelta
from math import isfinite
from statistics import median

CONTEXT_KEYS = ("outdoor_temperature", "context_water_temperature", "context_water_target")
FLAGS = ("summer_mode", "heating_active", "dhw_active")


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) else None
    except TypeError, ValueError, OverflowError:
        return None


def stamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.utcoffset() is None:
        raise ValueError("Timezone required")
    return value.astimezone(UTC)


def flag_for_hour(changes, start):
    """Only a known state throughout the hour qualifies, no back projection."""
    state = None
    end = start + timedelta(hours=1)
    for at, value in changes:
        if at <= start:
            state = value if value in ("on", "off") else None
        elif at < end and value != state:
            return None
        elif at >= end:
            break
    return state


def build_rows(statistics, flags, start, end):
    """Inputs normalized to W/C and UTC seconds by the Recorder reader."""
    maps = {}
    for key, rows in statistics.items():
        values = {}
        for row in rows:
            at = stamp(row["start"])
            value = numeric(row.get("mean"))
            if at.minute or at.second or at.microsecond or not start <= at < end:
                continue
            if at in values:
                raise ValueError("Duplicate statistics hour")
            values[at] = value
        maps[key] = values
    result = {}
    for at, power in maps.get("house", {}).items():
        if power is None or not 0 <= power <= 50000:
            continue
        row = {"house_w": power}
        for key in CONTEXT_KEYS:
            value = maps.get(key, {}).get(at)
            row[key] = value if value is not None and -60 <= value <= 120 else None
        for key in FLAGS:
            row[key] = flag_for_hour(flags.get(key, []), at)
        result[at.isoformat()] = row
    if len(result) > 1010:
        raise ValueError("History window exceeded")
    return result


class HistoricalPrior:
    """Idempotent hourly estimates with unknown measured coverage."""

    def __init__(self):
        self.rows = {}
        self.binding = None
        self.imported_at = None

    def snapshot(self):
        return {
            "version": 1,
            "rows": self.rows,
            "binding": self.binding,
            "imported_at": self.imported_at,
        }

    def restore(self, value):
        self.__init__()
        if not isinstance(value, dict) or value.get("version") != 1:
            return
        rows = value.get("rows")
        if not isinstance(rows, dict) or len(rows) > 1010:
            return
        try:
            for key, row in rows.items():
                at = stamp(key)
                if at.minute or at.second or at.microsecond:
                    return
                power = numeric(row.get("house_w"))
                if power is None or not 0 <= power <= 50000:
                    return
                for name in CONTEXT_KEYS:
                    if row.get(name) is not None and (
                        numeric(row[name]) is None or not -60 <= float(row[name]) <= 120
                    ):
                        return
                if any(row.get(name) not in (None, "on", "off") for name in FLAGS):
                    return
        except TypeError, ValueError, AttributeError, OverflowError:
            return
        if not isinstance(value.get("binding"), str):
            return
        self.rows = {k: dict(v) for k, v in rows.items()}
        self.binding = value["binding"]
        self.imported_at = value.get("imported_at")

    def replace(self, rows, binding, now):
        candidate = HistoricalPrior()
        candidate.restore(
            {"version": 1, "rows": rows, "binding": binding, "imported_at": now.isoformat()}
        )
        if not candidate.rows:
            raise ValueError("No usable history")
        self.rows, self.binding = candidate.rows, candidate.binding
        self.imported_at = candidate.imported_at

    def expected(self, at, now, timezone, context, online_cells, outdoor=None):
        """Distinct earlier days only; current temperature is a near-term proxy."""
        target = at.astimezone(timezone)
        today = now.astimezone(timezone).date()
        cutoff = today - timedelta(days=42)
        candidates = {}
        online_days = {key[:10] for key in online_cells}
        for key, row in self.rows.items():
            local = stamp(key).astimezone(timezone)
            if not cutoff <= local.date() < today or local.hour != target.hour:
                continue
            if local.date().isoformat() in online_days:
                continue
            mode = {"on": "summer", "off": "winter"}.get(row.get("summer_mode"), "unknown")
            if mode != "unknown" and mode != context:
                continue
            if (local.weekday() < 5) != (target.weekday() < 5):
                continue
            candidates.setdefault(local.date(), []).append(row)
        # The DST fold is one day/example, not two independent observations.
        days = []
        for day, rows in candidates.items():
            temps = [
                r["outdoor_temperature"] for r in rows if r.get("outdoor_temperature") is not None
            ]
            days.append(
                (
                    day,
                    median(r["house_w"] for r in rows),
                    median(temps) if temps else None,
                    any(r.get("summer_mode") is None for r in rows),
                )
            )
        match = "weekday"
        if outdoor is not None and (at - now).total_seconds() < 4 * 3600:
            near = [r for r in days if r[2] is not None and abs(r[2] - outdoor) <= 3]
            if len(near) >= 3:
                days, match = near, "weekday_and_current_outdoor_within_3C"
        exact = [r for r in days if r[0].weekday() == target.weekday()]
        chosen = exact if len(exact) >= 2 else days
        if len(chosen) < (2 if len(exact) >= 2 else 3):
            return None
        return {
            "house_w": median(r[1] for r in chosen),
            "days": len(chosen),
            "match": match,
            "unknown_season_days": sum(r[3] for r in chosen),
        }

    def summary(self):
        return {
            "status": "imported" if self.rows else "not_imported",
            "hours": len(self.rows),
            "measured_coverage": "unknown",
            "imported_at": self.imported_at,
            "first": min(self.rows) if self.rows else None,
            "last": max(self.rows) if self.rows else None,
            "known_season_hours": sum(r.get("summer_mode") is not None for r in self.rows.values()),
            "outdoor_hours": sum(
                r.get("outdoor_temperature") is not None for r in self.rows.values()
            ),
        }


def read_recorder(hass, sources, start, end):
    """Run in Recorder's executor. Read only, selected entities, bounded window."""
    from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
    from homeassistant.components.recorder.history import get_significant_states

    ids = {sources[k] for k in ("house", *CONTEXT_KEYS) if sources.get(k)}
    metadata = get_metadata(hass, statistic_ids=ids)
    house_meta = metadata.get(sources["house"])
    if not house_meta or house_meta[1].get("unit_of_measurement") not in ("W", "kW"):
        raise ValueError("House statistics require W or kW")
    valid = {
        key: entity
        for key, entity in sources.items()
        if key in ("house", *CONTEXT_KEYS)
        and entity in metadata
        and metadata[entity][1].get("unit_of_measurement")
        in (("W", "kW") if key == "house" else ("°C",))
    }
    statistics = statistics_during_period(
        hass, start, end, set(valid.values()), "hour", {"power": "W", "temperature": "°C"}, {"mean"}
    )
    flag_ids = [sources[k] for k in FLAGS if sources.get(k)]
    raw = get_significant_states(hass, start, end, flag_ids, no_attributes=True) if flag_ids else {}
    flags = {
        key: sorted((s.last_updated, s.state) for s in raw.get(entity, []))
        for key, entity in sources.items()
        if key in FLAGS
    }
    return build_rows(
        {key: statistics.get(entity, []) for key, entity in valid.items()}, flags, start, end
    )
