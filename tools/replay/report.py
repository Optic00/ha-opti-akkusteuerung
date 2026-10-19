"""Markdown report comparing policies per period."""
from __future__ import annotations

from typing import Any

TABLE_COLUMNS = [
    ("export_kwh", "Einspeisung kWh"),
    ("avoidable_export_kwh", "davon vermeidbar kWh"),
    ("import_kwh", "Netzbezug kWh"),
    ("import_cost_eur", "Bezugskosten EUR"),
    ("feed_in_revenue_eur", "Einspeiseerlös EUR"),
    ("net_cost_eur", "Nettokosten EUR"),
    ("self_consumption_pct", "EV-Quote %"),
    ("cycles", "Zyklen"),
    ("grid_charge_kwh", "Netzladung kWh"),
    ("mode_changes", "Moduswechsel"),
    ("engine_error_steps", "Engine-Fehler"),
]
SEASON_ORDER = ["gesamt", "uebergang", "winter"]


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}" if abs(value) >= 100 else f"{value:.2f}"
    return str(value)


def _table(period: str, metrics: dict[str, dict[str, dict]]) -> list[str]:
    lines = [f"### {period}", "", "| Policy | " + " | ".join(label for _, label in TABLE_COLUMNS) + " |",
             "|" + "---|" * (len(TABLE_COLUMNS) + 1)]
    for policy, periods in metrics.items():
        row = periods.get(period)
        if row is None:
            continue
        lines.append(f"| {policy} | " + " | ".join(_fmt(row.get(key)) for key, _ in TABLE_COLUMNS) + " |")
    return lines + [""]


def _patterns(policy: str, period: str, row: dict) -> list[str]:
    lines = [f"#### {policy} – {period}: vermeidbare Einspeisung {row['avoidable_export_kwh']} kWh", ""]
    for key, label in (("avoidable_by_decision", "nach decision_id"), ("avoidable_by_mode", "nach Modus"),
                       ("avoidable_by_soc_band", "nach SoC-Band"), ("avoidable_by_hour", "nach Stunde")):
        items = row.get(key, {})
        if items:
            lines.append(f"- {label}: " + ", ".join(f"{k} {v}" for k, v in list(items.items())[:12]))
    if row.get("top_days"):
        lines += ["", "| Tag | vermeidbar kWh | decision_ids (kWh) | Modi (kWh) |", "|---|---|---|---|"]
        for day in row["top_days"]:
            decisions = ", ".join(f"{k} {v}" for k, v in day["decisions"].items())
            modes = ", ".join(f"{k} {v}" for k, v in day["modes"].items())
            lines.append(f"| {day['day']} | {day['avoidable_export_kwh']} | {decisions} | {modes} |")
    return lines + [""]


def render(metrics: dict[str, dict[str, dict]], meta: dict[str, Any]) -> str:
    """``metrics`` maps policy name -> period -> metric dict."""
    periods: list[str] = []
    for policy_periods in metrics.values():
        periods.extend(key for key in policy_periods if key not in periods)
    seasons = [key for key in SEASON_ORDER if key in periods]
    months = sorted(key for key in periods if key not in SEASON_ORDER)
    lines = ["# Replay-Vergleich", ""]
    for key, value in meta.items():
        lines.append(f"- {key}: {value}")
    lines += ["", "Kosten und Erlöse zählen nur Schritte mit bekanntem Preis (priced_import_kwh in den JSON-Dateien).",
              "Vermeidbare Einspeisung: Export bei SoC < maxsoc − 1 % zu Schrittbeginn, begrenzt auf die im Schritt",
              "ungenutzte maximale Ladeleistung der Anlage.", "", "## Zeiträume", ""]
    for period in seasons + months:
        lines += _table(period, metrics)
    lines += ["## Muster vermeidbarer Einspeisung", ""]
    for policy, policy_periods in metrics.items():
        for period in seasons:
            if period in policy_periods and period != "gesamt":
                lines += _patterns(policy, period, policy_periods[period])
    return "\n".join(lines)
