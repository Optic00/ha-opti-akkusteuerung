"""Period metrics per policy, plus the avoidable-export pattern breakdown."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from .simulate import StepRecord

SEASONS = {9: "uebergang", 10: "uebergang", 3: "uebergang", 4: "uebergang",
           11: "winter", 12: "winter", 1: "winter", 2: "winter"}
SOC_BANDS = ((0, 50, "<50"), (50, 70, "50-70"), (70, 85, "70-85"), (85, 101, ">=85"))
AVOIDABLE_MARGIN = 1.0


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

    def add(self, record: StepRecord, avoidable: float, grid_charge: float, mode_changed: bool) -> None:
        s = self.sums
        self.steps += 1
        s["pv_kwh"] += record.pv_kwh
        s["load_kwh"] += record.load_kwh
        s["export_kwh"] += record.export_kwh
        s["import_kwh"] += record.import_kwh
        s["charge_kwh"] += record.charge_kwh
        s["discharge_kwh"] += record.discharge_kwh
        s["grid_charge_kwh"] += grid_charge
        s["avoidable_export_kwh"] += avoidable
        if record.price_ct is not None:
            s["priced_import_kwh"] += record.import_kwh
            s["priced_export_kwh"] += record.export_kwh
            s["import_cost_eur"] += record.import_kwh * record.price_ct / 100
        self.mode_changes += mode_changed
        self.error_steps += not record.engine_ok
        if avoidable > 0:
            self.by_decision[record.decision_id] += avoidable
            self.by_mode[record.mode] += avoidable
            self.by_hour[record.ts_local.hour] += avoidable
            self.by_soc[_soc_band(record.soc_before)] += avoidable
            day = record.ts_local.date()
            self.by_day[day] += avoidable
            self.day_decisions[day][record.decision_id] += avoidable
            self.day_modes[day][record.mode] += avoidable

    def result(self, capacity_kwh: float, feed_in_ct: float, top_n: int) -> dict[str, Any]:
        s = self.sums
        pv = s["pv_kwh"]
        revenue = s["priced_export_kwh"] * feed_in_ct / 100
        top_days = sorted(self.by_day.items(), key=lambda item: item[1], reverse=True)[:top_n]
        return {
            "steps": self.steps,
            "pv_kwh": round(pv, 2),
            "load_kwh": round(s["load_kwh"], 2),
            "export_kwh": round(s["export_kwh"], 2),
            "avoidable_export_kwh": round(s["avoidable_export_kwh"], 2),
            "import_kwh": round(s["import_kwh"], 2),
            "charge_kwh": round(s["charge_kwh"], 2),
            "discharge_kwh": round(s["discharge_kwh"], 2),
            "grid_charge_kwh": round(s["grid_charge_kwh"], 2),
            "cycles": round((s["charge_kwh"] + s["discharge_kwh"]) / 2 / capacity_kwh, 2) if capacity_kwh else None,
            "self_consumption_pct": round((pv - s["export_kwh"]) / pv * 100, 1) if pv > 0 else None,
            "priced_import_kwh": round(s["priced_import_kwh"], 2),
            "import_cost_eur": round(s["import_cost_eur"], 2),
            "feed_in_revenue_eur": round(revenue, 2),
            "net_cost_eur": round(s["import_cost_eur"] - revenue, 2),
            "mode_changes": self.mode_changes,
            "engine_error_steps": self.error_steps,
            "avoidable_by_decision": _rounded(self.by_decision),
            "avoidable_by_mode": _rounded(self.by_mode),
            "avoidable_by_hour": {str(k): round(v, 2) for k, v in sorted(self.by_hour.items())},
            "avoidable_by_soc_band": _rounded(self.by_soc),
            "top_days": [
                {
                    "day": day.isoformat(),
                    "avoidable_export_kwh": round(value, 2),
                    "decisions": _rounded(self.day_decisions[day]),
                    "modes": _rounded(self.day_modes[day]),
                }
                for day, value in top_days
            ],
        }


def _rounded(values: dict) -> dict:
    return {key: round(value, 2) for key, value in sorted(values.items(), key=lambda item: -item[1])}


def avoidable_export(record: StepRecord, maxsoc: float, charge_cap_kwh: float) -> float:
    """Export while the battery had room and unused charge power.

    Export beyond the plant's maximum charge power is physically unavoidable;
    export below it while SoC < maxsoc - 1 is a decision (mode, strategy's
    charge limit, target-SoC cap) and therefore counted.
    """
    if record.soc_before >= maxsoc - AVOIDABLE_MARGIN:
        return 0.0
    return min(record.export_kwh, max(0.0, charge_cap_kwh - record.charge_kwh))


def summarize(
    records: list[StepRecord], *, capacity_kwh: float, maxsoc: float, feed_in_ct: float,
    max_charge_w: float, step_hours: float = 0.25, top_n: int = 10,
) -> dict[str, dict[str, Any]]:
    """Metrics keyed by period: ``gesamt``, each month, ``uebergang`` and ``winter``."""
    accumulators: dict[str, _Accumulator] = defaultdict(_Accumulator)
    previous_mode = None
    charge_cap_kwh = max_charge_w * step_hours / 1000
    for record in records:
        avoidable = avoidable_export(record, maxsoc, charge_cap_kwh)
        grid_charge = max(0.0, record.charge_kwh - max(0.0, record.pv_kwh - record.load_kwh))
        changed = previous_mode is not None and record.mode != previous_mode
        previous_mode = record.mode
        for key in period_keys(record.ts_local):
            accumulators[key].add(record, avoidable, grid_charge, changed)
    return {
        key: accumulator.result(capacity_kwh, feed_in_ct, top_n)
        for key, accumulator in sorted(accumulators.items())
    }
