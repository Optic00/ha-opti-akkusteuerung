"""Bounded demand/PV comparison. This module never supplies controller inputs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from math import isfinite
from statistics import median

from .load_profile import LoadProfile
from .demand_accuracy import DemandAccuracy
from .demand_history import HistoricalPrior, CONTEXT_KEYS


SOURCE_KEYS = (
    "heat_power",
    "summer_mode",
    "heating_active",
    "dhw_active",
    "dhw_due",
    "water_temperature",
    "water_target",
    "pv_today",
    "pv_tomorrow",
    *CONTEXT_KEYS,
)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except TypeError, ValueError, OverflowError:
        return None


def instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(UTC)


def source_value(states, entity_id, now, *, kind):
    """Read selected signals; unknown never means off or zero."""
    s = states.get(entity_id) if entity_id else None
    if s is None or s.state in ("unknown", "unavailable"):
        return None
    # State flags may legitimately remain unchanged for months. Unlike power,
    # their HA state is authoritative until their integration marks unavailable.
    if kind == "flag":
        return {"on": True, "off": False}.get(s.state)
    try:
        age = (
            0
            if kind == "target"
            else (now - instant(s.last_reported or s.last_updated)).total_seconds()
        )
    except TypeError, ValueError, AttributeError:
        return None
    if not -60 <= age <= (21600 if kind == "temperature" else 900):
        return None
    v = number(s.state)
    unit = s.attributes.get("unit_of_measurement")
    factor = ({"W": 1, "kW": 1000} if kind == "power" else {"°C": 1}).get(unit)
    if v is None or factor is None:
        return None
    return v * factor


def pv_intervals(states, sources, now):
    """Solcast detailedForecast: dated half-hour mean power in kW (P10)."""
    rows = {}
    for key in ("pv_today", "pv_tomorrow"):
        s = states.get(sources.get(key, ""))
        if s is None or s.state in ("unknown", "unavailable"):
            continue
        try:
            age = (now - instant(s.last_reported or s.last_updated)).total_seconds()
            detail = s.attributes.get("detailedForecast")
            if not -60 <= age <= 21600 or not isinstance(detail, list) or len(detail) > 100:
                continue
            for row in detail:
                start = instant(row["period_start"])
                p10 = number(row.get("pv_estimate10"))
                if p10 is None or not 0 <= p10 <= 1000:
                    raise ValueError("Missing P10 power")
                end = start + timedelta(minutes=30)
                if end <= now or start > now + timedelta(hours=25):
                    continue
                if start in rows and rows[start] != p10 * 1000:
                    raise ValueError("Conflicting PV intervals")
                rows[start] = p10 * 1000
        except KeyError, ValueError, TypeError, AttributeError:
            return []
    ordered = sorted(rows.items())
    if any(b[0] - a[0] < timedelta(minutes=30) for a, b in zip(ordered, ordered[1:])):
        return []
    return ordered


class DemandForecast:
    """Separate local history, with no synthetic observations across downtime."""

    def __init__(self):
        self.cells = {}
        self.history = HistoricalPrior()
        self.accuracy = DemandAccuracy()
        self.fingerprint = None
        self.previous = None
        self.recent = LoadProfile(window_seconds=1800, max_gap_seconds=90)

    def snapshot(self):
        return {
            "version": 1,
            "history": self.history.snapshot(),
            "accuracy": self.accuracy.snapshot(),
            "fingerprint": self.fingerprint,
            "cells": {k: list(v) for k, v in self.cells.items()},
        }

    def restore(self, value):
        self.previous = None
        self.cells = {}
        self.history = HistoricalPrior()
        if not isinstance(value, dict) or value.get("version") != 1:
            return
        cells = value.get("cells")
        if (
            not isinstance(cells, dict)
            or len(cells) > 4032
            or not isinstance(value.get("fingerprint"), str)
        ):
            return
        try:
            for key, row in cells.items():
                date, hour, context = key.split("|")
                datetime.fromisoformat(date)
                if not 0 <= int(hour) <= 23 or context not in ("summer", "winter", "unknown"):
                    return
                if not isinstance(row, list) or len(row) != 4:
                    return
                base, heat, dhw, seconds = map(number, row)
                if None in (base, heat, dhw, seconds) or not 0 < seconds <= 7200:
                    return
                if (
                    not 0 <= base <= 50000 * seconds
                    or not 0 <= heat <= 50000 * seconds
                    or not 0 <= dhw <= heat
                ):
                    return
        except ValueError, TypeError:
            return
        self.cells = {key: [float(v) for v in row] for key, row in cells.items()}
        self.fingerprint = value["fingerprint"]
        self.accuracy.restore(value.get("accuracy"))
        self.history.restore(value.get("history"))

    @staticmethod
    def history_binding(load_fingerprint, options, timezone):
        cfg = options.get("demand_forecast", {})
        return json.dumps([load_fingerprint, cfg.get("history_house"), cfg.get("sources", {}),
                           str(timezone)], sort_keys=True)

    def _learn(self, now, base, heat, dhw, context, timezone):
        if self.previous:
            old, old_base, old_heat, old_dhw, old_context = self.previous
            gap = (now - old).total_seconds()
            if gap < 0:
                self.cells.clear()
            if 0 < gap <= 90 and old_base is not None and old_context == context:
                # Split at UTC minute boundaries: local hours and DST folds remain exact.
                cursor = old
                while cursor < now:
                    end = min(now, cursor.replace(second=0, microsecond=0) + timedelta(minutes=1))
                    local = cursor.astimezone(timezone)
                    key = f"{local.date().isoformat()}|{local.hour}|{context}"
                    row = self.cells.setdefault(key, [0.0, 0.0, 0.0, 0.0])
                    seconds = (end - cursor).total_seconds()
                    row[0] += old_base * seconds
                    row[1] += old_heat * seconds
                    row[2] += old_dhw * seconds
                    row[3] += seconds
                    cursor = end
        self.previous = (now, base, heat, dhw, context)
        cutoff = (now.astimezone(timezone) - timedelta(days=42)).date().isoformat()
        today = now.astimezone(timezone).date().isoformat()
        self.cells = {k: v for k, v in self.cells.items() if cutoff <= k[:10] <= today}

    def _expected(self, at, context, timezone, *, before=None):
        local = at.astimezone(timezone)
        exact, similar = [], []
        for key, (base, heat, dhw, seconds) in self.cells.items():
            day, hour, mode = key.split("|")
            if mode != context or int(hour) != local.hour or seconds < 1800:
                continue
            date = datetime.fromisoformat(day).date()
            reference = before.astimezone(timezone).date() if before is not None else local.date()
            if not reference - timedelta(days=42) <= date < reference:
                continue
            value = (base / seconds, heat / seconds, dhw / seconds)
            if date.weekday() == local.weekday():
                exact.append(value)
            if (date.weekday() < 5) == (local.weekday() < 5):
                similar.append(value)
        selected = exact if len(exact) >= 2 else similar
        if len(selected) < 3 and len(exact) < 2:
            return None
        return (
            median(v[0] for v in selected),
            median(v[1] for v in selected),
            median(v[2] for v in selected),
        )

    def update(self, now, data, settings, options, states, timezone, fingerprint):
        result = self._forecast(now, data, settings, options, states, timezone, fingerprint)
        if result["status"] != "disabled":
            house = number(data.get("states", {}).get("sensor.opti_house_consumption_w"))
            if (
                not data.get("online")
                or house is None
                or not 0 <= house <= 50000
                or any(
                    key == "house_consumption" or key.startswith("plant:")
                    for key in data.get("source_errors", {})
                )
            ):
                house = None
            result["accuracy"] = self.accuracy.observe(instant(now), house, result)
        return result

    def _forecast(self, now, data, settings, options, states, timezone, fingerprint):
        cfg = options.get("demand_forecast", {})
        out = {"status": "disabled", "observation_only": True}
        if cfg.get("enabled") is not True:
            return out
        now = instant(now)
        sources = cfg.get("sources", {})
        load_fingerprint = fingerprint
        fingerprint = json.dumps([fingerprint, {k: v for k, v in sources.items() if k not in CONTEXT_KEYS}, str(timezone)], sort_keys=True)
        if fingerprint != self.fingerprint:
            self.cells.clear()
            self.previous = None
            self.fingerprint = fingerprint
            self.accuracy = DemandAccuracy()
            self.history = HistoricalPrior()
        binding = self.history_binding(load_fingerprint, options, timezone)
        if self.history.binding and self.history.binding != binding:
            self.history = HistoricalPrior()

        def get(key, kind):
            return source_value(states, sources.get(key), now, kind=kind)

        summer, heating, dhw = (
            get(key, "flag") for key in ("summer_mode", "heating_active", "dhw_active")
        )
        context = "unknown" if summer is None else "summer" if summer else "winter"
        heat = get("heat_power", "power") if sources.get("heat_power") else 0.0
        house = number(data.get("states", {}).get("sensor.opti_house_consumption_w"))
        valid = data.get("online") and house is not None and 0 <= house <= 50000
        valid = valid and not any(
            k == "house_consumption" or k.startswith("plant:")
            for k in data.get("source_errors", {})
        )
        valid = valid and heat is not None and 0 <= heat <= (house if house is not None else 0)
        context_valid = not any(
            sources.get(k) and get(k, "flag") is None
            for k in ("summer_mode", "heating_active", "dhw_active", "dhw_due")
        )
        context_valid = context_valid and not (heating is True and dhw is True)
        valid = valid and context_valid
        base = house - heat if valid else None
        self._learn(now, base, heat, heat if dhw is True else 0.0, context, timezone)
        recent = self.recent.observe(base, now, fingerprint)
        out.update(
            historical_prior=self.history.summary(),
            temperature_context={key: get(key, "target" if key == "context_water_target" else "temperature") for key in CONTEXT_KEYS},
            status="learning",
            context=context,
            heating_active=heating,
            dhw_active=dhw,
            heat_meter_configured=bool(sources.get("heat_power")),
            learned_hours=len(self.cells),
            thermal_model="separate_meter"
            if sources.get("heat_power")
            else "included_in_house_profile",
            recent_coverage_seconds=round(recent.coverage_seconds),
            current_strategy_reserve_soc=data.get("reserve_plan", {}).get("planned_reserve_soc"),
            controls_battery=False,
            pv_method="Solcast P10 half-hour mean power",
        )
        if not valid:
            return {**out, "status": "data_missing", "detail": "house_or_heat_power"}
        if any(
            sources.get(k) and get(k, "flag") is None
            for k in ("summer_mode", "heating_active", "dhw_active", "dhw_due")
        ):
            return {**out, "status": "data_missing", "detail": "heat_context_unknown"}
        outdoor = get("outdoor_temperature", "temperature") if cfg.get("temperature_matching") is True else None
        expected_now = self._expected(now, context, timezone)
        if expected_now is None and not sources.get("heat_power"):
            current_prior = self.history.expected(now, now, timezone, context, self.cells, outdoor)
            if current_prior:
                expected_now = (current_prior["house_w"], 0.0, 0.0)
        uplift = (
            max(0.0, recent.raw_mean_w - expected_now[0])
            if expected_now and recent.coverage_seconds >= 1200
            else 0.0
        )
        if not sources.get("heat_power") and (heating is True or dhw is True):
            uplift = 0.0
        out["extra_base_load_w"] = round(uplift, 1)
        slots = pv_intervals(states, sources, now)
        if not slots:
            return {**out, "status": "no_pv_timing"}
        fallback = number(settings.get("input_number.opti_peak_verbrauch_kw"))
        if fallback is None or fallback <= 0:
            return {**out, "status": "data_missing", "detail": "fallback_load"}
        cursor, cover_since, boundary = now, None, None
        load_kwh = heat_kwh = dhw_kwh = deficit = required = 0.0
        learned = True
        projected = []
        historical_slots = 0
        cover_totals = None
        for start, pv_w in slots:
            end = start + timedelta(minutes=30)
            if start > cursor or end <= cursor or (end - now).total_seconds() > 24 * 3600:
                break
            hours = (end - cursor).total_seconds() / 3600
            expected = self._expected(cursor, context, timezone)
            provenance = {"source": "online" if expected else "fixed_or_recent_fallback"}
            if expected is None:
                learned = False
                prior = self.history.expected(cursor, now, timezone, context, self.cells,
                    outdoor) if not sources.get("heat_power") else None
                if prior:
                    expected = (prior["house_w"], 0.0, 0.0)
                    historical_slots += 1
                    provenance = {"source": "recorder_statistics", **{k: v for k, v in prior.items() if k != "house_w"}, "measured_coverage": "unknown"}
            if expected is None:
                first_hour = cursor < now + timedelta(hours=1)
                # Whole-house fixed fallback does not perpetuate a current DHW spike.
                expected = (
                    max(fallback * 1000 - (heat if first_hour else 0.0), recent.raw_mean_w or base),
                    heat if first_hour else 0.0,
                    heat if first_hour and dhw is True else 0.0,
                )
            extra = (uplift * max(0.0, 1 - (cursor - now).total_seconds() / 14400)
                     if provenance["source"] != "fixed_or_recent_fallback" else 0.0)
            thermal = expected[1]
            water_power = expected[2]
            # Active heat/DHW is reflected once, for at most the first hour.
            if (
                sources.get("heat_power")
                and (heating is True or dhw is True)
                and cursor < now + timedelta(hours=1)
            ):
                if dhw is True:
                    water_power += max(0.0, heat - thermal)
                thermal = max(thermal, heat)
            load = expected[0] + thermal + extra
            before = (load_kwh, heat_kwh, dhw_kwh, required)
            load_kwh += load * hours / 1000
            heat_kwh += thermal * hours / 1000
            dhw_kwh += water_power * hours / 1000
            deficit += (load - pv_w) * hours / 1000
            required = max(required, deficit)
            projected.append(
                {
                    "start": cursor.isoformat(),
                    "end": end.isoformat(),
                    "load_w": round(load),
                    "pv_p10_w": round(pv_w),
                    "load_provenance": provenance,
                }
            )
            if pv_w >= load * 1.2:
                if cover_since is None:
                    cover_since = cursor
                    cover_totals = before
                if (end - cover_since).total_seconds() >= 3600:
                    boundary = cover_since
                    load_kwh, heat_kwh, dhw_kwh, required = cover_totals
                    break
            else:
                cover_since = None
            cursor = end
        out.update(
            historical_forecast_slots=historical_slots,
            profile_ready=learned,
            forecast_slots=projected,
            expected_load_kwh=round(load_kwh, 3),
            heat_load_kwh=round(heat_kwh, 3),
            dhw_load_kwh=round(dhw_kwh, 3),
        )
        if boundary is None:
            return {**out, "status": "no_pv_timing", "detail": "no_contiguous_sustained_pv_cover"}
        due = get("dhw_due", "flag") is True
        if sources.get("water_temperature") or sources.get("water_target"):
            actual, target = get("water_temperature", "temperature"), get("water_target", "target")
            if actual is None or target is None:
                return {**out, "status": "data_missing", "detail": "water_temperature"}
            due = due or actual < target - 2
        cycle = number(cfg.get("dhw_cycle_kwh", 0))
        if due and (
            not sources.get("heat_power")
            or not sources.get("dhw_active")
            or cycle is None
            or cycle <= 0
        ):
            return {
                **out,
                "status": "data_missing",
                "detail": "dhw_due_requires_meter_and_cycle_energy",
            }
        # Floor, not an addition of a whole cycle to already forecast heat energy.
        dhw_extra = max(0.0, cycle - dhw_kwh) if due else 0.0
        required += dhw_extra
        capacity = number(data.get("states", {}).get("sensor.opti_battery_capacity_kwh"))
        soc = number(data.get("states", {}).get("sensor.opti_soc"))
        low, high = (number(settings.get("input_number." + key)) for key in ("minsoc", "maxsoc"))
        if (
            capacity is None
            or capacity <= 0
            or soc is None
            or not 0 <= soc <= 100
            or low is None
            or high is None
            or not 0 <= low < high <= 100
        ):
            return {**out, "status": "data_missing", "detail": "battery_limits"}
        # Explicit conservative assumptions, not a guaranteed physical optimum.
        battery_need = (required * 1.2 + 0.2) / 0.9
        uncapped = low + battery_need / capacity * 100
        out.update(
            status="ready" if learned and recent.coverage_seconds >= 1200 else "learning",
            pv_cover_from=boundary.isoformat(),
            expected_load_kwh=round(load_kwh + dhw_extra, 3),
            heat_load_kwh=round(heat_kwh + dhw_extra, 3),
            dhw_load_kwh=round(dhw_kwh + dhw_extra, 3),
            dhw_due=due,
            dhw_extra_kwh=round(dhw_extra, 3),
            expected_deficit_kwh=round(required, 3),
            safety_margin_percent=20,
            safety_margin_kwh=0.2,
            discharge_efficiency=0.9,
            required_battery_kwh=round(battery_need, 3),
            suggested_reserve_soc=round(min(high, uncapped), 1),
            capacity_shortfall_kwh=round(max(0.0, battery_need - capacity * (high - low) / 100), 3),
            available_above_suggestion_kwh=round(
                max(0.0, (soc - min(high, uncapped)) * capacity / 100), 3
            ),
            comparison_note="Different horizon: all demand until sustained PV, not only expensive slots",
        )
        return out
