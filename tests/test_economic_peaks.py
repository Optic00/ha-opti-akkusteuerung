"""Wirtschaftliche Peaks in Reserve, Preisniveau und Leiter (Issue #231).

Die Peak-Reserve zählte einen Slot nur, wenn sein Midrank-Perzentil über die
ganze Preisreihe (heute und morgen) mindestens 0,6 erreichte. War morgen
insgesamt teurer, fielen klar teure Slots von heute heraus, obwohl sie den
Ladepreis samt Verlusten und Zykluskosten deutlich überstiegen.

Fall 13.11.2026: Tief 28,0 ct um die Mittagszeit, Abend und Nacht 38 bis 45 ct,
morgen bis 49 ct. Gezählt wurden 8,75 h, die Reserve lag bei 68 %, der Akku bei
67 % im Stoppband; Peak-Vorladen griff nicht.

Neu zählt ein Slot zusätzlich, wenn er das wirtschaftliche Kriterium des
Akkumodells gegen das günstigste Tief VOR ihm (ab 00:00 des Preistags) erfüllt.
Ein solcher Slot gilt als EXPENSIVE, damit die Leiter ihn bedient. Damit er
keine Energie für spätere, teurere Peaks verbraucht, schützt die Leiter dort
zusätzlich die Reserve für teurere Folge-Slots (Rangschutz).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.arbitrage import _price_periods
from custom_components.opti_akku.battery_model import BatteryModel
from custom_components.opti_akku.peak_rank import _classify, _hour_start
from tests.battery_model_fixture import model_attributes, model_states

_SPEC = importlib.util.spec_from_file_location(
    "opti_economic_peaks_engine",
    Path(__file__).parents[1] / "custom_components/opti_akku/engine.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
TZ = ZoneInfo("Europe/Berlin")

KEEP = {
    "opti_peak_reserve_soc",
    "opti_price_level",
    "opti_strategie_vorschau",
    "opti_peak_reserve_aktiv",
    "opti_extreme_price_hold",
}

# Day-ahead-Preise 13. und 14.11.2026 (Viertelstunden, ct/kWh, live gelesen).
TODAY_1311 = [
    34.77, 34.78, 34.69, 34.42, 34.98, 34.6, 34.56, 34.3, 34.13, 33.73, 33.71, 33.8,
    33.76, 33.54, 33.6, 33.74, 33.65, 33.67, 33.69, 33.99, 34.24, 34.14, 34.79, 35.53,
    36.52, 37.46, 38.01, 39.2, 40.51, 41.06, 41.83, 42.23, 42.68, 42.17, 41.29, 39.98,
    37.72, 36.86, 34.54, 32.37, 33.66, 32.07, 29.84, 28.75, 29.46, 28.71, 28.18, 28.04,
    28.42, 28.34, 28.19, 28.02, 29.4, 29.82, 29.37, 28.99, 29.38, 31.51, 33.59, 35.29,
    34.48, 36.15, 37.97, 39.37, 39.76, 42.39, 43.07, 43.09, 43.78, 44.08, 44.68, 45.02,
    43.57, 43.76, 43.7, 43.61, 43.63, 43.15, 42.93, 42.67, 42.93, 42.22, 41.1, 39.24,
    40.61, 38.8, 37.24, 35.22, 42.94, 42.05, 41.05, 40.19, 40.32, 38.81, 38.68, 38.51,
]
TOMORROW_1311 = [
    41.51, 40.14, 39.65, 39.69, 40.1, 39.81, 39.44, 39.29, 39.17, 39.08, 38.76, 38.62,
    39.27, 39.14, 39.07, 38.83, 38.61, 38.54, 38.91, 38.92, 38.63, 38.86, 38.79, 39.19,
    38.86, 40.13, 41.16, 41.88, 43.47, 44.19, 44.84, 44.2, 46.05, 44.1, 43.25, 41.24,
    40.46, 39.77, 38.45, 37.26, 37.52, 36.6, 36.67, 37.09, 35.32, 34.9, 34.98, 35.14,
    34.92, 34.72, 34.73, 35.15, 35.64, 35.85, 36.56, 36.43, 36.78, 38.74, 41.16, 43.45,
    39.91, 41.75, 44.21, 45.86, 45.08, 46.38, 47.13, 47.6, 47.84, 49.17, 49.3, 48.94,
    49.01, 48.41, 47.69, 46.76, 46.91, 47.21, 46.64, 45.37, 43.55, 41.57, 41.68, 41.96,
    41.57, 42.19, 41.58, 40.89, 42.52, 41.09, 41.98, 40.83, 41.15, 40.33, 39.48, 38.92,
]


def _resource():
    resource = _MODULE.load_resources()
    blocks = []
    for block in resource["template_blocks"]:
        kept = {
            kind: [item for item in block.get(kind, []) if item.get("unique_id") in KEEP]
            for kind in ("sensor", "binary_sensor")
        }
        if not any(kept.values()):
            continue
        blocks.append({**block, **kept})
    resource["template_blocks"] = blocks
    resource["statistics"] = []
    resource["balancing_automations"] = []
    return resource


ENGINE = _MODULE.StrategyEngine(_resource())


def _slot_price(now, today, tomorrow):
    slots = len(today) // 24
    index = now.hour * slots + now.minute * slots // 60
    return today[index]


def evaluate(now, *, soc, today=TODAY_1311, tomorrow=TOMORROW_1311, mode="Akku Dynamisch",
             rising=dt.datetime(2026, 11, 14, 7, 48, tzinfo=TZ), model=True, score=0,
             capacity=12.8, minsoc=15, halte_spread=5, current_price=None):
    values = {
        "input_boolean.akku_opti_automatik": "on",
        "input_boolean.opti_prognose_netzladen": "on",
        "input_boolean.opti_pv_ueberschuss_ladung": "on",
        "sensor.opti_soc": soc,
        "sensor.opti_battery_capacity_kwh": capacity,
        "sensor.opti_price_current_ct_kwh": (
            _slot_price(now, today, tomorrow) if current_price is None else current_price),
        "sensor.opti_forecast_score": score,
        "sensor.opti_forecast_score_tomorrow": 4,
        "binary_sensor.opti_peak_horizont_lang": "off",
        "binary_sensor.opti_pv_reichtag": "off",
        "input_number.minsoc": minsoc,
        "input_number.maxsoc": 95,
        "input_number.opti_peak_verbrauch_kw": 0.7,
        "input_number.opti_peak_min_aufschlag_ct": 5,
        "input_number.opti_netzlade_spread_ct": 10,
        "input_number.opti_halte_spread_ct": halte_spread,
        "input_select.akkusteuerung_modus": mode,
        "sun.sun": "above_horizon" if 8 <= now.hour < 16 else "below_horizon",
        **(model_states() if model else {}),
    }
    attributes = {
        "sensor.opti_price_series": {"today": list(today), "tomorrow": list(tomorrow)},
        "sun.sun": {
            "next_rising": rising.astimezone(dt.timezone.utc).isoformat(),
            "next_setting": now.replace(hour=16, minute=39).astimezone(dt.timezone.utc).isoformat(),
        },
        **(model_attributes() if model else {}),
    }
    result = ENGINE.evaluate(values, attributes, now)
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    assert result.states["sensor.opti_strategie_vorschau"] == result.mode
    return result


def reserve(result):
    return result.attributes["sensor.opti_peak_reserve_soc"]


def at(hour, minute=0, day=13):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_cheap_window_precharges_for_economic_evening_and_night():
    # 13:45, 28,99 ct: guenstigster Slot vor dem ersten Peak (13:15 mit 29,82 ct
    # liegt ueber Fenster-Tief + 0,5 ct und wartet zu Recht).
    result = evaluate(at(13, 45), soc=67)
    attrs = reserve(result)
    # Bisher 8,75 h (nur Perzentil); 39,6-ct-Abend und Nacht zählen jetzt mit.
    assert attrs["peak_stunden_ve"] + attrs["peak_stunden_exp"] > 10
    assert float(result.states["sensor.opti_peak_reserve_soc"]) > 75
    assert result.decision_id == "peak_precharge"
    assert result.mode == "Akku Netzladen"


def test_afternoon_after_the_low_keeps_holding_the_charged_energy():
    # 15:00, 34,5 ct: das 28-ct-Tief ist vorbei. Die gespeicherte Energie zaehlt
    # fuer Slots, die das Tief vor ihnen schlagen und mindestens den
    # Halte-Spread (5 ct) ueber dem jetzigen Preis liegen; der Akku entlaedt
    # nicht unter Ladepreis, die Reserve steigt aber nicht ueber den SoC.
    result = evaluate(at(15), soc=80)
    attrs = reserve(result)
    assert result.states["sensor.opti_price_level"] in ("VERY_CHEAP", "CHEAP", "NORMAL")
    assert float(attrs["reserve_kern_soc"]) < 80
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == pytest.approx(80)
    assert result.decision_id == "peak_l4"
    assert result.mode == "Akku nur Laden"


def test_stored_energy_never_triggers_precharge():
    # Gespeicherte wirtschaftliche Slots halten nur, was geladen ist.
    result = evaluate(at(15), soc=40)
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == pytest.approx(
        float(reserve(result)["reserve_kern_soc"]))
    assert result.decision_id != "peak_precharge"


def test_economic_slot_discharges_only_the_surplus_over_later_expensive_slots():
    # 15:45, 39,37 ct: Perzentil unter 0,6, aber ueber dem Mindestpreis des
    # 28-ct-Tiefs. Bei vollem Akku reicht die Energie auch fuer alle teureren
    # Folge-Slots: angehoben, L2 entlaedt bis zur Schutzreserve.
    result = evaluate(at(15, 45), soc=95)
    attrs = reserve(result)
    assert result.attributes["sensor.opti_price_level"]["perzentil"] < 60
    assert result.attributes["sensor.opti_price_level"]["wirtschaftlich_angehoben"] is True
    assert attrs["aktueller_slot_wirtschaftlich"] is True
    assert result.states["sensor.opti_price_level"] == "EXPENSIVE"
    assert 42.3 < float(attrs["reserve_schutz_soc"]) < 95
    assert result.decision_id == "peak_l2"


def test_economic_slot_below_hold_spread_is_not_raised_but_may_discharge():
    # 15:45, 39,37 ct, SoC 82 %: zu wenig fuer alle teureren Folge-Slots, also
    # nicht angehoben. Die Abendslots liegen aber weniger als den Halte-Spread
    # hoeher, deshalb haelt die Leiter den Inhalt ueber der Kernreserve nicht.
    result = evaluate(at(15, 45), soc=82)
    attrs = reserve(result)
    assert attrs["aktueller_slot_wirtschaftlich"] is False
    assert result.states["sensor.opti_price_level"] == "NORMAL"
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == pytest.approx(
        float(attrs["reserve_kern_soc"]))
    assert result.decision_id != "peak_l4"


def _issue_231_prices():
    """10./11.11.-Muster: Tag 30 ct, Nacht 43 ct, Morgen 55 ct, Folgetag 65 ct."""
    today = [36.0] * 96
    today[44:56] = [30.0] * 12          # 11:00-14:00
    today[68:] = [43.0] * 28            # 17:00-24:00
    tomorrow = [43.0] * 96
    tomorrow[28:43] = [55.0] * 15       # 07:00-10:45
    tomorrow[43:76] = [65.0] * 33       # 10:45-19:00
    return today, tomorrow


@pytest.mark.parametrize(("soc", "level", "decision"), [
    (35, "NORMAL", "peak_l4"),
    (45, "EXPENSIVE", "peak_l2"),
])
def test_issue_231_night_slot_does_not_spend_the_morning_reserve(soc, level, decision):
    today, tomorrow = _issue_231_prices()
    result = evaluate(at(22), soc=soc, today=today, tomorrow=tomorrow,
                      rising=dt.datetime(2026, 11, 14, 7, 45, tzinfo=TZ))
    attrs = reserve(result)
    # Morgen 07:00-10:45 zu 55 ct: 3,75 h x 0,7 kW / 0,9 = 2,9 kWh, also +22,8 %.
    assert float(attrs["reserve_kern_soc"]) == pytest.approx(37.8)
    # Unter der Morgenreserve haelt L4; darueber entlaedt L2 nur den Ueberschuss.
    assert result.states["sensor.opti_price_level"] == level
    assert result.decision_id == decision
    if decision == "peak_l2":
        assert float(attrs["reserve_schutz_soc"]) == pytest.approx(37.8)
        assert float(attrs["peak_preis_schutz_avg_ct"]) == 55.0


def test_issue_231_after_midnight_night_is_normal_and_morning_reserved():
    # Nach Mitternacht ist der Folgetag "heute": Referenz ist die 43-ct-Nacht.
    _today, tomorrow = _issue_231_prices()
    following = [43.0] * 96
    result = evaluate(at(1, day=14), soc=35, today=tomorrow, tomorrow=following,
                      rising=dt.datetime(2026, 11, 14, 7, 45, tzinfo=TZ))
    attrs = reserve(result)
    assert attrs["aktueller_slot_wirtschaftlich"] is False
    assert result.states["sensor.opti_price_level"] in ("VERY_CHEAP", "CHEAP", "NORMAL")
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == pytest.approx(37.8)
    # Haelt fuer den Morgen; bei schlechtem Score greift davor schon "SOC<45 sehr
    # guenstig" (ebenfalls Halten), sonst L4.
    assert result.decision_id in ("peak_l4", "reserve_cheap")
    assert result.mode == "Akku nur Laden"


def test_without_valid_battery_model_only_the_percentile_counts():
    result = evaluate(at(13, 20), soc=67, model=False)
    attrs = reserve(result)
    assert attrs["peak_stunden_ve"] + attrs["peak_stunden_exp"] == pytest.approx(8.75)
    assert attrs["aktueller_slot_wirtschaftlich"] is False


def test_empty_daytime_horizon_never_raises_the_price_level():
    # Guter Tages-Score, mehr als 3 h bis Sonnenuntergang: Horizont leer.
    result = evaluate(at(11), soc=40, score=8)
    assert reserve(result)["aktueller_slot_wirtschaftlich"] is False
    assert result.states["sensor.opti_price_level"] in ("VERY_CHEAP", "CHEAP", "NORMAL")


@pytest.mark.parametrize("moment", [(13, 45), (15, 0), (20, 45), (23, 30)])
def test_peak_rank_mirror_counts_the_same_peak_hours(moment):
    now = at(*moment)
    attrs = reserve(evaluate(now, soc=60))
    periods = _price_periods({"today": TODAY_1311, "tomorrow": TOMORROW_1311}, now, TZ)
    end = dt.datetime.fromisoformat(attrs["horizont_ende"])
    start = _hour_start(now, TZ)
    window = [i for i, slot in enumerate(periods) if start <= slot[0] < end]
    peaks, _stored, _percentile = _classify(
        periods, window, now, prices=[slot[2] for slot in periods],
        window_min=min(periods[i][2] for i in window), surcharge=5,
        model=BatteryModel.default(), spread=10)
    assert len(peaks) / 4 == pytest.approx(attrs["peak_stunden_ve"] + attrs["peak_stunden_exp"])


def test_mismatched_current_price_never_raises_the_level():
    # Review-Befund: Preisreihe und aktueller Preis kommen aus getrennten
    # Quellen. Passt der aktuelle Preis (28 ct) nicht zum eingestuften Slot
    # (39,37 ct), bleibt es beim Perzentil und der Akku entlaedt nicht.
    now = at(15, 45)
    values_price = 28.0
    result = evaluate(now, soc=95, current_price=values_price)
    assert reserve(result)["aktueller_slot_wirtschaftlich"] is True
    assert result.attributes["sensor.opti_price_level"]["wirtschaftlich_angehoben"] is False
    assert result.states["sensor.opti_price_level"] != "EXPENSIVE"
    assert result.decision_id not in ("peak_l1", "peak_l2")
