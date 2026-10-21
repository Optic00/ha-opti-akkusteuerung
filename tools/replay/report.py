"""Markdown report comparing policies per period."""
from __future__ import annotations

from typing import Any

TABLE_COLUMNS = [
    ("net_cost_eur", "Nettokosten EUR"),
    ("import_kwh", "Netzbezug kWh"),
    ("export_kwh", "Einspeisung kWh"),
    ("pv_potential_end_of_day_kwh", "PV-Potenzial Tagesende kWh"),
    ("replaceable_import_kwh", "ersetzbarer Netzbezug kWh"),
    ("replaceable_import_eur", "ersetzbarer Netzbezug EUR"),
    ("export_free_battery_raw_kwh", "Export bei freiem Akku (roh) kWh"),
    ("grid_charge_kwh", "Netzladung kWh"),
    ("cycles", "Zyklen"),
    ("hours_soc_above_85", "h SoC > 85 %"),
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
    lines = [f"#### {policy} – {period}: PV-Potenzial am Tagesende {row['pv_potential_end_of_day_kwh']} kWh, "
             f"Export bei freiem Akku (roh) {row['export_free_battery_raw_kwh']} kWh", ""]
    for key, label in (("raw_by_decision", "roh nach decision_id"), ("raw_by_mode", "roh nach Modus"),
                       ("raw_by_soc_band", "roh nach SoC-Band"), ("raw_by_hour", "roh nach Stunde")):
        items = row.get(key, {})
        if items:
            lines.append(f"- {label}: " + ", ".join(f"{k} {v}" for k, v in list(items.items())[:12]))
    if row.get("top_days_potential"):
        lines += ["", "| Tag | Potenzial kWh | ersetzbar kWh | ersetzbar EUR |", "|---|---|---|---|"]
        for day in row["top_days_potential"]:
            lines.append(f"| {day['day']} | {day['potential_kwh']} | {day['replaced_import_kwh']} | {day['replaced_import_eur']} |")
    if row.get("top_days_raw"):
        lines += ["", "| Tag | roh kWh | decision_ids (kWh) | Modi (kWh) |", "|---|---|---|---|"]
        for day in row["top_days_raw"]:
            decisions = ", ".join(f"{k} {v}" for k, v in day["decisions"].items())
            modes = ", ".join(f"{k} {v}" for k, v in day["modes"].items())
            lines.append(f"| {day['day']} | {day['export_free_battery_raw_kwh']} | {decisions} | {modes} |")
    return lines + [""]


def _gap_table(metrics: dict[str, dict[str, dict]], seasons: list[str]) -> list[str]:
    """Net cost against the oracle and against self-consumption per season."""
    oracle = next((name for name in metrics if name == "oracle" or name.startswith("oracle@")), None)
    baseline = "self_consumption" if "self_consumption" in metrics else None
    if oracle is None and baseline is None:
        return []
    lines = ["## Lücke zum Oracle", "",
             "Nettokosten je Policy minus Nettokosten des Oracles (perfekte Voraussicht, gleiche Anlagengrenzen);",
             "negativ ist unmöglich bis auf Rundung. Vorteil ggü. Eigenverbrauch: Nettokosten von self_consumption",
             "minus Nettokosten der Policy (positiv = Ersparnis).", ""]
    header = ["Policy"]
    for season in seasons:
        if oracle:
            header.append(f"Lücke {season} EUR")
        if baseline:
            header.append(f"Vorteil {season} EUR")
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for policy, periods in metrics.items():
        row = [policy]
        for season in seasons:
            cost = periods.get(season, {}).get("net_cost_eur")
            if oracle:
                ref = metrics[oracle].get(season, {}).get("net_cost_eur")
                row.append(_fmt(round(cost - ref, 2)) if cost is not None and ref is not None else "-")
            if baseline:
                ref = metrics[baseline].get(season, {}).get("net_cost_eur")
                row.append(_fmt(round(ref - cost, 2)) if cost is not None and ref is not None else "-")
        lines.append("| " + " | ".join(row) + " |")
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
        if isinstance(value, dict):
            lines.append(f"- {key}:")
            lines.extend(f"  - {inner}: {item}" for inner, item in value.items())
        else:
            lines.append(f"- {key}: {value}")
    lines += ["", "Kosten und Erlöse zählen nur Schritte mit bekanntem Preis (priced_import_kwh in den JSON-Dateien).",
              "PV-Potenzial am Tagesende: Export, den ein virtueller Zusatzspeicher je Tag noch hätte aufnehmen können",
              "(begrenzt durch freie Ladeleistung, freie Kapazität neben dem realen Verlauf und Ladewirkungsgrad;",
              "spätere reguläre Ladung verdrängt ihn, nur der Rest nach dem letzten PV-Schritt zählt).",
              "Ersetzbarer Netzbezug: Anteil dieses Potenzials, der bis zur nächsten PV-Produktion Netzbezug ersetzt",
              "hätte (Entladewirkungsgrad und -leistung, bewertet zum Preis des Schritts).",
              "Export bei freiem Akku (roh): Export bei SoC < maxsoc − 1 % und ungenutzter Ladeleistung je Schritt,",
              "ohne Rücksicht auf spätere Vollladung – nur als Mustersuche brauchbar, nicht als Verlust.",
              "Anlaufstunden nach einem Messblock-Neustart sind aus allen Zeiträumen ausgenommen (warmup_steps).",
              "", "## Zeiträume", ""]
    for period in seasons + months:
        lines += _table(period, metrics)
    lines += _gap_table(metrics, seasons)
    lines += ["## Muster vermeidbarer Einspeisung", ""]
    for policy, policy_periods in metrics.items():
        for period in seasons:
            if period in policy_periods and period != "gesamt":
                lines += _patterns(policy, period, policy_periods[period])
    return "\n".join(lines)
