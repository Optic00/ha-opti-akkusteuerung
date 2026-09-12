"""Wiederauflade-Horizont: Sonnentag-Score und Horizont-Hysterese (Issue #70)."""
import datetime as dt

import pytest

from .ha_harness import REPO, TZ, FakeHass, find_template_entity, load_yaml, render
from .test_peak_reserve import _hass, _peak, _reichtag_preise

NACHT = dt.datetime(2026, 9, 11, 3, 2, tzinfo=TZ)
ABEND = dt.datetime(2026, 9, 10, 22, 15, tzinfo=TZ)
AUFGANG = "2026-09-11T06:45:00+02:00"


def _entity(kind, uid):
    return find_template_entity(
        load_yaml(REPO / "packages" / "opti_derived.yaml"), kind, uid)


def _prognose_hass(*, now=NACHT, next_rising=AUFGANG, heute="6", heute_p10=None,
                   morgen="40", morgen_p10=None, verbrauch="500", alpha=None):
    states = {
        "sensor.opti_forecast_today_kwh": heute,
        "sensor.opti_forecast_tomorrow_kwh": morgen,
        "sensor.opti_house_consumption_w": verbrauch,
        "sensor.opti_house_consumption_60min_w": verbrauch,
    }
    if alpha is not None:
        states["input_number.opti_forecast_optimismus"] = alpha
    return FakeHass(
        now=now,
        states=states,
        attrs={
            "sensor.opti_forecast_today_kwh": {"estimate10": heute_p10},
            "sensor.opti_forecast_tomorrow_kwh": {"estimate10": morgen_p10},
            "sun.sun": {"next_rising": next_rising},
        },
    )


def _sonnentag(hass, part="state"):
    entity = _entity("sensor", "opti_forecast_score_sonnentag")
    template = entity[part] if part in ("state", "availability") else entity["attributes"][part]
    return render(hass, template)


def _lang(score, this_state=None, part="state"):
    entity = _entity("binary_sensor", "opti_peak_horizont_lang")
    hass = FakeHass(states={"sensor.opti_forecast_score_sonnentag": score},
                    this_state=this_state)
    template = entity["state"] if part == "state" else entity["attributes"][part]
    return render(hass, template)


# ---------------------------------------------------------------------------
# Sonnentag-Score
# ---------------------------------------------------------------------------

def test_nach_mitternacht_zaehlt_ganztagsprognose_heute():
    # 6 kWh / (0,5 kW * 24 h) = 0,5 -> 5. Die Morgen-Prognose (40 kWh) meint
    # nach Mitternacht schon den uebernaechsten Sonnentag und darf nicht zaehlen.
    hass = _prognose_hass()
    assert _sonnentag(hass) == "5"
    assert _sonnentag(hass, "quelle") == "sensor.opti_forecast_today_kwh"


def test_vor_mitternacht_zaehlt_prognose_morgen():
    hass = _prognose_hass(now=ABEND, heute="40", morgen="6")
    assert _sonnentag(hass) == "5"
    assert _sonnentag(hass, "quelle") == "sensor.opti_forecast_tomorrow_kwh"


@pytest.mark.parametrize("morgen, p10, alpha, verbrauch", [
    ("6", None, None, "500"),
    ("100", "20", "0", "4000"),
    ("100", "20", "40", "4000"),
    ("100", "20", "100", "4000"),
    ("30", "0", "0", "800"),
    ("0", None, None, "0"),
])
def test_vor_mitternacht_identisch_mit_score_tomorrow(morgen, p10, alpha, verbrauch):
    hass = _prognose_hass(now=ABEND, heute="999", morgen=morgen, morgen_p10=p10,
                          alpha=alpha, verbrauch=verbrauch)
    tomorrow = render(hass, _entity("sensor", "opti_forecast_score_tomorrow")["state"])
    assert _sonnentag(hass) == tomorrow


def test_nachts_p10_und_alpha_wie_score_tomorrow():
    # Gleicher Blend wie score_tomorrow, nur aus der Ganztagsprognose heute.
    assert _sonnentag(_prognose_hass(heute="100", heute_p10="20", alpha="0",
                                     verbrauch="4000")) == "2"
    assert _sonnentag(_prognose_hass(heute="100", heute_p10="20", alpha="40",
                                     verbrauch="4000")) == "5"


def test_verbrauchsdrift_aus_issue_70_bleibt_weit_von_der_kante():
    # Issue #70, 03:02: 11,15 kWh Tagesprognose, 60-min-Verbrauch 498 -> 563 W.
    # Die alte Tagesformel fiel dabei von 4 auf 2 und schaltete 36 h; der
    # Sonnentag-Score bewegt sich nur zwischen 9 und 8.
    assert _sonnentag(_prognose_hass(heute="11.15", verbrauch="498")) == "9"
    assert _sonnentag(_prognose_hass(heute="11.15", verbrauch="563")) == "8"


def test_availability_haengt_an_der_quelle_des_sonnentags():
    assert _sonnentag(_prognose_hass(), "availability") == "True"
    # Nachts fehlt die Ganztagsprognose: kein Rueckgriff auf die Morgen-Prognose.
    assert _sonnentag(_prognose_hass(heute="unavailable"), "availability") == "False"
    # Abends reicht die Morgen-Prognose, heute darf fehlen.
    assert _sonnentag(_prognose_hass(now=ABEND, heute="unavailable"),
                      "availability") == "True"
    # Ohne naechsten Sonnenaufgang gibt es keinen Sonnentag.
    assert _sonnentag(_prognose_hass(next_rising=None), "availability") == "False"


# ---------------------------------------------------------------------------
# Horizont-Hysterese
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score, vorher, lang", [
    ("0", None, "True"),
    ("1", "off", "True"),
    ("2", "off", "False"),    # Ausschlag 3 -> 2 verlaengert nicht
    ("2", "on", "True"),      # Anstieg 1 -> 2 verkuerzt nicht
    ("2", None, "True"),      # Neustart: alte Schwelle
    ("3", "on", "False"),
    ("3", None, "False"),
    ("10", "on", "False"),
    ("unavailable", "off", "True"),
    ("unknown", None, "True"),
    ("kein-score", "off", "True"),
])
def test_horizont_hysterese_und_failsafe(score, vorher, lang):
    assert _lang(score, this_state=vorher) == lang


def test_horizont_branch_zeigt_score_und_vorzustand():
    gehalten = _lang("2", this_state="off", part="branch")
    assert "Score=2" in gehalten and "Vorzustand gehalten" in gehalten
    assert "fehlt" in _lang("unavailable", this_state="off", part="branch")


# ---------------------------------------------------------------------------
# Peak-Rechenkern
# ---------------------------------------------------------------------------

MORGEN = dt.datetime(2026, 7, 27, 3, 40, tzinfo=TZ)


def _peak_mit(horizont_lang, score_heute="10"):
    return _peak(_hass(
        _reichtag_preise(), [], now=MORGEN, score_heute=score_heute,
        score_morgen="10", cap="12.8", verbrauch="0.8", minsoc="5",
        maxsoc="95", sun_state="below_horizon",
        next_rising="2026-07-27T05:45:00+02:00", reichtag="off",
        horizont_lang=horizont_lang))


def test_peak_verkuerzt_nur_bei_explizitem_off():
    assert _peak_mit("off")["horizont_ende"] == "2026-07-27T08:45:00+02:00"
    for zustand in ("on", "unknown", "unavailable"):
        assert _peak_mit(zustand)["horizont_ende"] == "2026-07-28T15:40:00+02:00"


def test_peak_liest_nachts_nicht_mehr_den_tages_score():
    # Tages-Score 0 (Mitternachts-Dip, Tagesformel) aendert den Horizont nicht,
    # solange die Horizont-Entscheidung off ist.
    assert _peak_mit("off", score_heute="0")["horizont_ende"] == \
        "2026-07-27T08:45:00+02:00"


def test_reichtag_liest_sonnentag_statt_tages_score():
    entity = _entity("binary_sensor", "opti_pv_reichtag")
    hass = FakeHass(
        now=MORGEN,
        states={"sensor.opti_forecast_score": "0",
                "sensor.opti_forecast_score_sonnentag": "10"},
        attrs={"sun.sun": {"next_rising": "2026-07-27T05:45:00+02:00"}},
        this_state="off",
    )
    assert render(hass, entity["state"]) == "True"


def test_issue_70_kette_haelt_kurzen_horizont():
    # 03:02: Verbrauchsdrift wie im Issue, Vorzustand off (Horizont kurz).
    score = _sonnentag(_prognose_hass(heute="11.15", verbrauch="563"))
    lang = _lang(score, this_state="off")
    assert lang == "False"
    assert _peak_mit("on" if lang == "True" else "off")["horizont_ende"] == \
        "2026-07-27T08:45:00+02:00"
