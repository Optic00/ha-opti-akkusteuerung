"""Bounded source evidence and a parallel plant balance; no controller inputs."""

from datetime import UTC, datetime, timedelta
import json
from math import isfinite

from .load_profile import LoadProfile
from .plant import evaluate_plant


def finite(value):
    try:
        v = float(value)
        return v if isfinite(v) else None
    except TypeError, ValueError, OverflowError:
        return None


def evidence(state, now):
    if state is None:
        return {"value": None, "missing": True}
    result = {
        "value": finite(state.state),
        "available": state.state not in ("unknown", "unavailable"),
        "unit": state.attributes.get("unit_of_measurement"),
    }
    for key in ("last_reported", "last_updated", "last_changed"):
        at = getattr(state, key, None)
        if isinstance(at, datetime) and at.utcoffset() is not None:
            result[key] = at.isoformat()
            result[key + "_age_s"] = round((now - at).total_seconds(), 1)
    return result


def watts(detail):
    value = detail.get("value")
    factor = {"W": 1, "kW": 1000}.get(detail.get("unit"))
    return value * factor if value is not None and value >= 0 and factor is not None else None


class SourceObservation:
    """48h five-minute aggregates and transition evidence, bounded in Store."""

    def __init__(self):
        self.binding = None
        self.bins = {}
        self.events = []
        self.profile = LoadProfile(max_gap_seconds=90)
        self.previous = None
        self.signature = None
        self.last_evidence_at = None

    def snapshot(self):
        return {"version": 1, "binding": self.binding, "bins": self.bins, "events": self.events}

    def restore(self, value):
        # Diagnostic-only payload: no arbitrary entities or input evaluation on restore.
        if not isinstance(value, dict) or value.get("version") != 1:
            return
        try:
            if len(json.dumps(value)) > 2000000:
                return
            bins, events = value.get("bins"), value.get("events")
            if (
                not isinstance(bins, dict)
                or len(bins) > 577
                or not isinstance(events, list)
                or len(events) > 256
            ):
                return
            if not isinstance(value.get("binding"), str):
                return
            for key, row in bins.items():
                if not isinstance(row, dict) or not key.isdigit():
                    return
                if any(
                    finite(row.get(k)) is None or row[k] < 0
                    for k in ("seconds", "native_ws", "legacy_ws", "absolute_difference_ws")
                ):
                    return
                for field in ("gross_seconds", "gross_absolute_difference_ws"):
                    item = row.get(field, 0)
                    if not isinstance(item, (int, float)) or finite(item) is None or item < 0:
                        return
                if row["seconds"] > 300 or row.get("gross_seconds", 0) > 300:
                    return
            self.binding = value["binding"]
            self.bins = {k: dict(v) for k, v in bins.items()}
            self.events = [
                e for e in events if isinstance(e, dict) and isinstance(e.get("at"), str)
            ]
        except ValueError, TypeError, OverflowError:
            return

    def update(
        self, now, measurements, states, options, online, source_errors, connection, identity
    ):
        cfg = options.get("source_observation", {})
        if cfg.get("enabled") is not True:
            self.__init__()
            return {"status": "disabled", "controls_battery": False}
        binding = json.dumps(
            [
                cfg,
                options.get("sources", {}).get("house_consumption"),
                options.get("source_max_age", 900),
            ],
            sort_keys=True,
        )
        if binding != self.binding or (self.previous and now < self.previous[0]):
            self.__init__()
            self.binding = binding
        legacy_id = options.get("sources", {}).get("house_consumption")
        ids = list(
            dict.fromkeys(
                [
                    legacy_id,
                    cfg.get("instant_house"),
                    cfg.get("gross_house"),
                    *cfg.get("watch_sources", []),
                    *cfg.get("additional_ac_sources", []),
                    *cfg.get("excluded_load_sources", []),
                ]
            )
        )
        details = {key: evidence(states.get(key), now) for key in ids if key}
        candidate = {
            "plant_mode": "balance",
            "plant_meter_confirmed": cfg.get("meter_confirmed", False),
            "forecast_min_load_w": 0,
            "source_max_age": options.get("source_max_age", 900),
            "additional_ac_sources": cfg.get("additional_ac_sources", []),
            "excluded_load_sources": cfg.get("excluded_load_sources", []),
            "sources": {},
        }
        plant = evaluate_plant(measurements if online else {}, candidate, states, now)
        gross = evaluate_plant(
            measurements if online else {}, {**candidate, "excluded_load_sources": []}, states, now
        )
        gross_state = details.get(cfg.get("gross_house"), {})
        gross_age = gross_state.get("last_reported_age_s", float("inf"))
        limit = finite(options.get("source_max_age", 900)) or 900
        gross_difference = (
            gross.house_w - watts(gross_state)
            if online
            and gross.house_w is not None
            and gross_state.get("available")
            and watts(gross_state) is not None
            and -60 <= gross_age <= limit
            else None
        )
        native = plant.base_load_w if online else None
        profile = self.profile.observe(native, now, binding)
        floor = finite(cfg.get("legacy_floor_w", 0)) or 0
        comparison = (
            max(floor, profile.raw_mean_w)
            if profile.raw_mean_w is not None and not profile.warming_up
            else None
        )
        legacy = details.get(legacy_id, {})
        limit = finite(options.get("source_max_age", 900)) or 900
        legacy_valid = (
            legacy.get("available")
            and watts(legacy) is not None
            and -60 <= legacy.get("last_reported_age_s", float("inf")) <= limit
        )
        difference = comparison - watts(legacy) if comparison is not None and legacy_valid else None
        reason = (
            "ready"
            if difference is not None
            else "warming_up"
            if native is not None and legacy_valid
            else "data_missing"
        )
        signature = json.dumps(
            [
                online,
                source_errors,
                connection.get("status"),
                plant.errors,
                {
                    k: (v.get("available"), not -60 <= v.get("last_reported_age_s", 1e9) <= limit)
                    for k, v in details.items()
                },
            ],
            sort_keys=True,
        )
        if (
            signature != self.signature
            or self.last_evidence_at is None
            or (now - self.last_evidence_at).total_seconds() >= 900
        ):
            self.last_evidence_at = now
            self.events.append(
                {
                    "at": now.isoformat(),
                    "online": online,
                    "connection": connection.get("status"),
                    "source_errors": dict(source_errors),
                    "balance_errors": plant.errors,
                    "sources": details,
                    "identity_registers": identity,
                }
            )
            self.signature = signature
        if self.previous:
            at, old_native, old_legacy, old_diff, old_gross_diff = self.previous
            seconds = (now - at).total_seconds()
            if 0 < seconds <= 90 and (old_diff is not None or old_gross_diff is not None):
                cursor = at
                while cursor < now:
                    bucket = int(cursor.timestamp()) // 300 * 300
                    end = min(now, datetime.fromtimestamp(bucket + 300, UTC))
                    duration = (end - cursor).total_seconds()
                    row = self.bins.setdefault(
                        str(bucket),
                        {
                            "seconds": 0.0,
                            "native_ws": 0.0,
                            "legacy_ws": 0.0,
                            "absolute_difference_ws": 0.0,
                            "gross_seconds": 0.0,
                            "gross_absolute_difference_ws": 0.0,
                        },
                    )
                    if old_diff is not None:
                        row["seconds"] += duration
                        row["native_ws"] += old_native * duration
                        row["legacy_ws"] += old_legacy * duration
                        row["absolute_difference_ws"] += abs(old_diff) * duration
                    if old_gross_diff is not None:
                        row["gross_seconds"] = row.get("gross_seconds", 0) + duration
                        row["gross_absolute_difference_ws"] = (
                            row.get("gross_absolute_difference_ws", 0)
                            + abs(old_gross_diff) * duration
                        )
                    cursor = end
        self.previous = (now, comparison, watts(legacy), difference, gross_difference)
        cutoff = now - timedelta(hours=48)
        self.bins = {
            k: v
            for k, v in self.bins.items()
            if int(cutoff.timestamp()) // 300 * 300 <= int(k) <= int(now.timestamp())
        }
        self.events = [e for e in self.events if cutoff.isoformat() <= e["at"] <= now.isoformat()][
            -256:
        ]
        seconds = sum(v["seconds"] for v in self.bins.values())
        gross_seconds = sum(v.get("gross_seconds", 0) for v in self.bins.values())
        return {
            "status": reason,
            "controls_battery": False,
            "sources": details,
            "balance_errors": plant.errors,
            "gross_native_w": gross.house_w if online else None,
            "gross_difference_w": gross_difference,
            "gross_comparison_covered_seconds": gross_seconds,
            "gross_mean_absolute_difference_w": sum(
                v.get("gross_absolute_difference_ws", 0) for v in self.bins.values()
            )
            / gross_seconds
            if gross_seconds
            else None,
            "native_instant_w": native,
            "legacy_instant": details.get(cfg.get("instant_house")),
            "native_60min_w": profile.raw_mean_w,
            "comparison_native_w": comparison,
            "legacy_smoothed_w": watts(legacy),
            "difference_w": difference,
            "profile_coverage_seconds": profile.coverage_seconds,
            "comparison_covered_seconds": seconds,
            "mean_absolute_difference_w": sum(
                v["absolute_difference_ws"] for v in self.bins.values()
            )
            / seconds
            if seconds
            else None,
            "aggregation": "time_weighted_5min_48h; gaps_over_90s_excluded",
            "comparison_note": "Native zero-order hold vs legacy smoothing; identical topology must be configured",
            "recent_events": self.events[-6:],
            "aggregate_bins": len(self.bins),
        }
