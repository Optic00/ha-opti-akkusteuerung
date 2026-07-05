"""Stunden-Backtest der Opti-Strategie: faehrt die ECHTEN Jinja-Templates
(Reserve-Sensor + Strategie-Vorschau) gegen Preis-/Lastprofile und simuliert
den SoC-Verlauf. Vergleich alt (neue Regeln stillgelegt) vs. neu.

Modus-Wirkung im Simulator (vereinfachtes Adapter-Modell):
  Akku nur Laden     -> reine Entladesperre (kein Netzbezug); Haus laeuft aus
                        dem Netz, Akku bleibt idle.
  Akku Netzladen     -> erzwungenes Netzladen (beide Laderegeln setzen diesen
                        Modus); laedt aus dem Netz bis maxsoc.
  Akku nur Entladen  -> deckt Hauslast aus dem Akku (bis minsoc).
  Akku Dynamisch     -> deckt Hauslast aus dem Akku (bis minsoc), laedt PV ein.
Verluste: Laden und Entladen je mit eta=0.95 (Round-Trip ~0.9).

Bewusste Limitationen (Ergebnisse entsprechend eng interpretieren):
  - Es wird durchgehend NACHT simuliert (sun.sun below_horizon, Score 1,
    opti_target_soc fest auf 95): alle Tag-Zweige der Strategie (70%/AC-
    Ueberschuss, "dyn bis Ziel tag", Ziel-SoC-getriebenes Entladen) werden
    nie durchlaufen. Der Backtest validiert den Nacht-/Peak-Pfad.
  - _price_level ist eine Python-Reimplementierung des opti_price_level-
    Templates (Midrank-Perzentil) - bei Aenderungen am Template dort
    mit nachziehen. Reserve-Sensor und Strategie-Vorschau laufen dagegen
    als ECHTE Templates.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests.ha_harness import (REPO, TZ, FakeHass, find_template_entity,
                              find_trigger_block_variables, load_yaml,
                              render, render_native)

ETA = 0.95
LADE_KW = 2.5  # angenommene Netz-Ladeleistung im Simulator

_derived = load_yaml(REPO / "packages" / "opti_derived.yaml")
_vorschau = find_template_entity(_derived, "sensor", "opti_strategie_vorschau")
_peak_template = find_trigger_block_variables(_derived, "peak")


def _states(hour_price, soc, load_kw, neu, cap_kwh, peak, *,
            halte_spread_ct, netzlade_spread_ct, minsoc, maxsoc):
    minv = peak["min_preis_vor_peak_ct"] if peak else None
    avg = peak["peak_preis_avg_ct"] if peak else None
    ve_avg = peak["ve_preis_avg_ct"] if peak else None
    aktiv = bool(peak and peak["gueltig"] and peak["benoetigt_kwh"] > 0)
    return FakeHass(
        states={
            "sensor.opti_soc": str(round(soc, 2)),
            "sensor.opti_battery_capacity_kwh": str(cap_kwh),
            "sensor.opti_forecast_score": "1",
            "sensor.opti_forecast_score_tomorrow": "1",
            "sensor.opti_price_level": "unknown",  # wird unten gesetzt
            # Ziel-SoC fest auf maxsoc gepinnt (keine Tag-Dynamik simuliert,
            # siehe Limitationen im Docstring) - folgt aber maxsoc-Sweeps.
            "sensor.opti_target_soc": str(maxsoc),
            "sensor.opti_price_current_ct_kwh": str(hour_price),
            "sensor.opti_grid_export_w": "0",
            "sensor.opti_pv_power_w": "0",
            "sensor.opti_peak_reserve_soc":
                str(peak["ges_soc"]) if aktiv else "unavailable",
            "binary_sensor.opti_peak_reserve_aktiv": "on" if (neu and aktiv) else "off",
            "binary_sensor.opti_winter_charging_allowed": "on",
            "input_number.minsoc": str(minsoc),
            "input_number.maxsoc": str(maxsoc),
            "input_number.opti_peak_verbrauch_kw": str(load_kw),
            "input_number.opti_einspeiseverguetung_ct": "8" if neu else "0",
            "input_number.opti_netzlade_spread_ct": str(netzlade_spread_ct) if neu else "999",
            "input_number.opti_halte_spread_ct": str(halte_spread_ct),
            "input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": "500",
            "input_number.akkusteuerung_wr_ac_ueberschuss_grenze": "4500",
            "input_boolean.opti_prognose_netzladen": "on",
            "input_boolean.opti_pv_ueberschuss_ladung": "on",
            "input_select.akkusteuerung_modus": "Akku Dynamisch",
            "sun.sun": "below_horizon",
        },
        attrs={"sensor.opti_peak_reserve_soc": {
            "reserve_ve_soc": peak["ve_soc"] if peak else None,
            "min_preis_vor_peak_ct": minv,
            "peak_preis_avg_ct": avg,
            "peak_preis_ve_avg_ct": ve_avg}},
    )


def _price_level(prices, current):
    kleiner = len([p for p in prices if p < current])
    gleich = len([p for p in prices if p == current])
    pct = (kleiner + 0.5 * gleich) / len(prices)
    if pct < 0.20:
        return "VERY_CHEAP"
    if pct < 0.40:
        return "CHEAP"
    if pct < 0.60:
        return "NORMAL"
    if pct < 0.80:
        return "EXPENSIVE"
    return "VERY_EXPENSIVE"


def simulate_day(prices_today, prices_tomorrow, *, load_kw, pv_kwh_per_hour,
                 start_soc, cap_kwh, neu, modus_start="Akku Dynamisch",
                 aufschlag_ct=None, halte_spread_ct=None, netzlade_spread_ct=None,
                 minsoc=10.0, maxsoc=95.0):
    # Tuning-Hebel: neu=True -> Gewinner-Defaults aus dem Winter-Backtest-Sweep
    # 2026-07-02 (aufschlag 5, halte 5, netzlade_spread 10), neu=False -> Gate
    # stillgelegt, Parameter egal (aufschlag/halte 0 zur Klarheit).
    # Ueberschreibbar fuer Sweeps.
    if aufschlag_ct is None:
        aufschlag_ct = 5 if neu else 0
    if halte_spread_ct is None:
        halte_spread_ct = 5 if neu else 0
    if netzlade_spread_ct is None:
        netzlade_spread_ct = 10
    soc = start_soc
    modus = modus_start
    cost = 0.0
    soc_verlauf, modus_verlauf = [], []
    alle_preise = list(prices_today) + list(prices_tomorrow)
    for hour in range(24):
        preis = prices_today[hour]
        now = dt.datetime(2026, 1, 15, hour, 30, tzinfo=TZ)
        # 1. Reserve-Sensor mit echtem Template rechnen
        peak_hass = FakeHass(
            now=now,
            states={
                "sensor.opti_battery_capacity_kwh": str(cap_kwh),
                "sensor.opti_forecast_score": "1",
                "sensor.opti_forecast_score_tomorrow": "1",
                "input_number.opti_peak_verbrauch_kw": str(load_kw),
                "input_number.minsoc": str(minsoc),
                "input_number.maxsoc": str(maxsoc),
                "input_number.opti_peak_min_aufschlag_ct": str(aufschlag_ct),
                "sun.sun": "below_horizon",
            },
            attrs={
                "sensor.opti_price_series":
                    {"today": prices_today, "tomorrow": prices_tomorrow},
                "sun.sun": {"next_rising": "2026-01-16T08:15:00+01:00"},
            })
        peak = render_native(peak_hass, _peak_template)
        # 2. Strategie-Entscheidung mit echtem Vorschau-Template
        hass = _states(preis, soc, load_kw, neu, cap_kwh, peak,
                       halte_spread_ct=halte_spread_ct,
                       netzlade_spread_ct=netzlade_spread_ct,
                       minsoc=minsoc, maxsoc=maxsoc)
        hass.now_value = now
        hass.states_map["sensor.opti_price_level"] = _price_level(alle_preise, preis)
        hass.states_map["input_select.akkusteuerung_modus"] = modus
        modus = render(hass, _vorschau["state"])
        # 3. Energiefluss der Stunde
        pv = pv_kwh_per_hour[hour]
        last = load_kw
        if modus == "Akku nur Entladen" or modus == "Akku Dynamisch":
            entnehmbar = max(0.0, (soc - minsoc) / 100 * cap_kwh) * ETA
            aus_akku = min(last, entnehmbar)
            soc -= aus_akku / ETA / cap_kwh * 100
            cost += (last - aus_akku) * preis / 100
        elif modus == "Akku Netzladen":  # erzwungenes dynamisches Netzladen
            cost += last * preis / 100
            lade = min(LADE_KW, (maxsoc - soc) / 100 * cap_kwh / ETA)
            soc += lade * ETA / cap_kwh * 100
            cost += lade * preis / 100
        else:  # Akku nur Laden: reine Entladesperre (idle), Haus aus dem Netz
            cost += last * preis / 100
        if pv > 0 and soc < maxsoc:
            soc = min(maxsoc, soc + pv * ETA / cap_kwh * 100)
        soc = max(0.0, min(100.0, soc))
        soc_verlauf.append(round(soc, 1))
        modus_verlauf.append(modus)
    return {"cost_eur": round(cost, 2), "soc_verlauf": soc_verlauf,
            "modus_verlauf": modus_verlauf}


SZENARIEN = {
    "Abendspitze (Bens 2-EUR-Szenario)": {
        "prices_today": [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0],
        "prices_tomorrow": [50.0] * 24, "start_soc": 15.0},
    "Flacher Wintertag": {
        "prices_today": [32.0] * 24, "prices_tomorrow": [32.0] * 24,
        "start_soc": 50.0},
    "Typischer Tibber-Wintertag (Morgen- + Abendpeak)": {
        "prices_today": [28, 26, 25, 25, 26, 30, 38, 45, 42, 35, 32, 30,
                         29, 29, 30, 33, 38, 44, 48, 46, 40, 34, 30, 28],
        "prices_tomorrow": [28, 26, 25, 25, 26, 30, 38, 45, 42, 35, 32, 30,
                            29, 29, 30, 33, 38, 44, 48, 46, 40, 34, 30, 28],
        "start_soc": 35.0},
}


def main():
    print("Hinweis: Nacht-only-Simulation (keine Tag-Zweige, Ziel-SoC fest 95) -"
          " siehe Limitationen im Modul-Docstring.\n")
    for name, sz in SZENARIEN.items():
        kwargs = dict(load_kw=0.9, pv_kwh_per_hour=[0.0] * 24, cap_kwh=12.8, **sz)
        alt = simulate_day(neu=False, **kwargs)
        neu = simulate_day(neu=True, **kwargs)
        delta = alt["cost_eur"] - neu["cost_eur"]
        print(f"{name}\n  alt: {alt['cost_eur']:6.2f} EUR   "
              f"neu: {neu['cost_eur']:6.2f} EUR   Ersparnis: {delta:+.2f} EUR")
        print(f"  Modi neu: {neu['modus_verlauf']}")


if __name__ == "__main__":
    main()
