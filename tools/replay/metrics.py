"""Period metrics per policy, end-of-day PV potential and raw export patterns."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from .battery import BatteryParams
from .simulate import StepRecord

SEASONS = {9: "uebergang", 10: "uebergang", 3: "uebergang", 4: "uebergang",
           11: "winter", 12: "winter", 1: "winter", 2: "winter"}
SOC_BANDS = ((0, 50, "<50"), (50, 70, "50-70"), (70, 85, "70-85"), (85, 101, ">=85"))
FREE_BATTERY_MARGIN = 1.0
HIGH_SOC = 85.0
CONTINUITY_GAP = dt.timedelta(hours=6)


def period_keys(ts: dt.datetime) -> list[str]:
    keys = ["gesamt", ts.strftime("%Y-%m")]
    if ts.month in SEASONS:
        keys.append(SEASONS[ts.month])
    return keys


def _soc_band(soc: float) -> str:
    for low, high, label in SOC_BANDS:
        if low <= soc < high:
            return label
    return SOC_BANDS[-1][2]


def export_free_battery_raw(record: StepRecord, maxsoc: float, charge_cap_kwh: float) -> float:
    """Raw export while the battery had room and unused charge power.

    Counts every step independently: it ignores whether the battery filled up
    later in the day, so it overstates what could have been stored. The
    end-of-day potential below is the usable figure.
    """
    if record.soc_before >= maxsoc - FREE_BATTERY_MARGIN:
        return 0.0
    return min(record.export_kwh, max(0.0, charge_cap_kwh - record.charge_kwh))


def end_of_day_potential(
    records: list[StepRecord], params: BatteryParams, step_hours: float
) -> dict[dt.date, dict[str, float]]:
    """PV that a virtual extra store could still hold after the day's last PV step.

    Per step the extra charge is bounded by the exported energy, the unused
    charge power and the capacity left beside the real trajectory, with the
    charge efficiency applied. Later regular charging displaces the virtual
    content; a full battery wipes earlier chances. The content left after the
    last PV step is the day's potential (as AC-side PV energy). Until the next
    PV production it may replace grid import with the discharge efficiency and
    power limit, valued at each step's price.
    """
    cap_max = params.maxsoc / 100 * params.capacity_kwh
    charge_cap = params.max_charge_w * step_hours / 1000
    discharge_cap = params.max_discharge_w * step_hours / 1000
    result: dict[dt.date, dict[str, float]] = {}
    virtual = 0.0
    daytime = False
    day: dt.date | None = None
    previous_ts: dt.datetime | None = None

    def entry(key: dt.date) -> dict[str, float]:
        return result.setdefault(key, {"potential_kwh": 0.0, "replaced_import_kwh": 0.0,
                                       "replaced_import_eur": 0.0, "replaced_unpriced_kwh": 0.0})

    for record in records:
        if previous_ts is not None and record.ts_local - previous_ts > CONTINUITY_GAP:
            if daytime and day is not None:
                entry(day)["potential_kwh"] += virtual / params.eta_charge
            virtual, daytime = 0.0, False
        previous_ts = record.ts_local
        stored_after = record.soc_after / 100 * params.capacity_kwh
        virtual = min(virtual, max(0.0, cap_max - stored_after))  # displaced by real charging
        if record.pv_kwh > 0:
            if not daytime:
                virtual, daytime, day = 0.0, True, record.ts_local.date()
            free_power = max(0.0, charge_cap - record.charge_kwh)
            free_capacity = max(0.0, cap_max - stored_after - virtual)
            extra = min(record.export_kwh, free_power, free_capacity / params.eta_charge)
            virtual += extra * params.eta_charge
            day = record.ts_local.date()
            continue
        if daytime:
            daytime = False
            entry(day)["potential_kwh"] += virtual / params.eta_charge
        if virtual > 0 and day is not None:
            replaced = min(record.import_kwh, max(0.0, discharge_cap - record.discharge_kwh), virtual * params.eta_discharge)
            virtual -= replaced / params.eta_discharge
            target = entry(day)
            target["replaced_import_kwh"] += replaced
            if record.price_ct is not None:
                target["replaced_import_eur"] += replaced * record.price_ct / 100
            else:
                target["replaced_unpriced_kwh"] += replaced
    if daytime and day is not None:
        entry(day)["potential_kwh"] += virtual / params.eta_charge
    return result


class _Accumulator:
    def __init__(self) -> None:
        self.sums: dict[str, float] = defaultdict(float)
        self.mode_changes = 0
        self.error_steps = 0
        self.steps = 0
        self.by_decision: dict[str, float] = defaultdict(float)
        self.by_mode: dict[str, float] = defaultdict(float)
        self.by_hour: dict[int, float] = defaultdict(float)
        self.by_soc: dict[str, float] = defaultdict(float)
        self.by_day: dict[dt.date, float] = defaultdict(float)
        self.day_decisions: dict[dt.date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.day_modes: dict[dt.date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.potential_days: dict[dt.date, dict[str, float]] = {}

    def add(self, record: StepRecord, raw_free: float, grid_charge: float, mode_changed: bool, step_hours: float) -> None:
        s = self.sums
        self.steps += 1
        s["pv_kwh"] += record.pv_kwh
        s["load_kwh"] += record.load_kwh
        s["export_kwh"] += record.export_kwh
        s["import_kwh"] += record.import_kwh
        s["charge_kwh"] += record.charge_kwh
        s["discharge_kwh"] += record.discharge_kwh
        s["grid_charge_kwh"] += grid_charge
        s["export_free_battery_raw_kwh"] += raw_free
        if record.soc_before > HIGH_SOC:
            s["hours_soc_above_85"] += step_hours
        if record.price_ct is not None:
            s["priced_import_kwh"] += record.import_kwh
            s["priced_export_kwh"] += record.export_kwh
            s["import_cost_eur"] += record.import_kwh * record.price_ct / 100
        self.mode_changes += mode_changed
        self.error_steps += not record.engine_ok
        if raw_free > 0:
            self.by_decision[record.decision_id] += raw_free
            self.by_mode[record.mode] += raw_free
            self.by_hour[record.ts_local.hour] += raw_free
            self.by_soc[_soc_band(record.soc_before)] += raw_free
            day = record.ts_local.date()
            self.by_day[day] += raw_free
            self.day_decisions[day][record.decision_id] += raw_free
            self.day_modes[day][record.mode] += raw_free

    def result(self, capacity_kwh: float, feed_in_ct: float, top_n: int) -> dict[str, Any]:
        s = self.sums
        pv = s["pv_kwh"]
        revenue = s["priced_export_kwh"] * feed_in_ct / 100
        potential = {key: sum(values[key] for values in self.potential_days.values())
                     for key in ("potential_kwh", "replaced_import_kwh", "replaced_import_eur", "replaced_unpriced_kwh")}
        top_days = sorted(self.by_day.items(), key=lambda item: item[1], reverse=True)[:top_n]
        top_potential = sorted(self.potential_days.items(), key=lambda item: item[1]["potential_kwh"], reverse=True)[:top_n]
        return {
            "steps": self.steps,
            "pv_kwh": round(pv, 2),
            "load_kwh": round(s["load_kwh"], 2),
            "export_kwh": round(s["export_kwh"], 2),
            "pv_potential_end_of_day_kwh": round(potential["potential_kwh"], 2),
            "replaceable_import_kwh": round(potential["replaced_import_kwh"], 2),
            "replaceable_import_eur": round(potential["replaced_import_eur"], 2),
            "replaceable_import_unpriced_kwh": round(potential["replaced_unpriced_kwh"], 2),
            "export_free_battery_raw_kwh": round(s["export_free_battery_raw_kwh"], 2),
            "import_kwh": round(s["import_kwh"], 2),
            "charge_kwh": round(s["charge_kwh"], 2),
            "discharge_kwh": round(s["discharge_kwh"], 2),
            "grid_charge_kwh": round(s["grid_charge_kwh"], 2),
            "cycles": round((s["charge_kwh"] + s["discharge_kwh"]) / 2 / capacity_kwh, 2) if capacity_kwh else None,
            "self_consumption_pct": round((pv - s["export_kwh"]) / pv * 100, 1) if pv > 0 else None,
            "hours_soc_above_85": round(s["hours_soc_above_85"], 2),
            "priced_import_kwh": round(s["priced_import_kwh"], 2),
            "import_cost_eur": round(s["import_cost_eur"], 2),
            "feed_in_revenue_eur": round(revenue, 2),
            "net_cost_eur": round(s["import_cost_eur"] - revenue, 2),
            "mode_changes": self.mode_changes,
            "engine_error_steps": self.error_steps,
            "raw_by_decision": _rounded(self.by_decision),
            "raw_by_mode": _rounded(self.by_mode),
            "raw_by_hour": {str(k): round(v, 2) for k, v in sorted(self.by_hour.items())},
            "raw_by_soc_band": _rounded(self.by_soc),
            "top_days_raw": [
                {
                    "day": day.isoformat(),
                    "export_free_battery_raw_kwh": round(value, 2),
                    "decisions": _rounded(self.day_decisions[day]),
                    "modes": _rounded(self.day_modes[day]),
                }
                for day, value in top_days
            ],
            "top_days_potential": [
                {"day": day.isoformat(), **{key: round(value, 2) for key, value in values.items()}}
                for day, values in top_potential if values["potential_kwh"] > 0
            ],
        }


def _rounded(values: dict) -> dict:
    return {key: round(value, 2) for key, value in sorted(values.items(), key=lambda item: -item[1])}


def summarize(
    records: list[StepRecord], *, params: BatteryParams, feed_in_ct: float,
    step_hours: float = 0.25, top_n: int = 10,
) -> dict[str, dict[str, Any]]:
    """Metrics keyed by period: ``gesamt``, each month, ``uebergang`` and ``winter``.

    Warm-up records after a measurement block restart are excluded from every
    period and reported under ``warmup_steps`` of ``gesamt``.
    """
    counted = [record for record in records if not record.warmup]
    accumulators: dict[str, _Accumulator] = defaultdict(_Accumulator)
    previous_mode = None
    charge_cap_kwh = params.max_charge_w * step_hours / 1000
    for record in counted:
        raw_free = export_free_battery_raw(record, params.maxsoc, charge_cap_kwh)
        grid_charge = max(0.0, record.charge_kwh - max(0.0, record.pv_kwh - record.load_kwh))
        changed = previous_mode is not None and record.mode != previous_mode
        previous_mode = record.mode
        for key in period_keys(record.ts_local):
            accumulators[key].add(record, raw_free, grid_charge, changed, step_hours)
    for day, values in end_of_day_potential(counted, params, step_hours).items():
        for key in period_keys(dt.datetime.combine(day, dt.time())):
            accumulators[key].potential_days[day] = values
    result = {
        key: accumulator.result(params.capacity_kwh, feed_in_ct, top_n)
        for key, accumulator in sorted(accumulators.items())
    }
    if "gesamt" in result:
        result["gesamt"]["warmup_steps"] = len(records) - len(counted)
    return result
