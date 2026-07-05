import datetime as dt

from .ha_harness import (REPO, TZ, FakeHass, find_template_entity,
                         find_trigger_block_variables, load_yaml, render_native)

WINTER_ABEND = dt.datetime(2026, 1, 15, 17, 30, tzinfo=TZ)  # nach Sonnenuntergang


def _hass(today, tomorrow, *, now=WINTER_ABEND, score_heute="1", score_morgen="1",
          cap="12.8", verbrauch="0.9", minsoc="10", maxsoc="95",
          sun_state="below_horizon", next_rising="2026-01-16T08:15:00+01:00",
          aufschlag="0"):
    # aufschlag default "0" = alte Tests bleiben semantisch unveraendert (keine
    # oekonomische Filterung); neue Tests setzen aufschlag explizit aktiv.
    return FakeHass(
        now=now,
        states={
            "sensor.opti_battery_capacity_kwh": cap,
            "sensor.opti_forecast_score": score_heute,
            "sensor.opti_forecast_score_tomorrow": score_morgen,
            "input_number.opti_peak_verbrauch_kw": verbrauch,
            "input_number.minsoc": minsoc,
            "input_number.maxsoc": maxsoc,
            "input_number.opti_peak_min_aufschlag_ct": aufschlag,
            "sun.sun": sun_state,
        },
        attrs={
            "sensor.opti_price_series": {"today": today, "tomorrow": tomorrow},
            "sun.sun": {"next_rising": next_rising},
        },
    )


def _peak(hass):
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    template = find_trigger_block_variables(cfg, "peak")
    return render_native(hass, template)


def test_abendspitze_heute_zaehlt():
    # 19-22 Uhr heute 200 ct, sonst 50 ct; morgen flach 50. Score morgen schlecht -> 36h-Fenster.
    today = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]
    peak = _peak(_hass(today, [50.0] * 24))
    assert peak["ve_stunden"] == 3
    assert peak["exp_stunden"] == 0
    # 3 h * 0.9 kW / 0.9 eta = 3.0 kWh -> 10 + 3.0/12.8*100 = 33.4 %
    assert abs(peak["ges_soc"] - 33.4) < 0.2
    assert abs(peak["ve_soc"] - 33.4) < 0.2
    assert peak["benoetigt_kwh"] == 3.0
    assert peak["min_preis_vor_peak_ct"] == 50.0
    assert peak["peak_preis_avg_ct"] == 200.0


def test_abendspitze_trotz_sonne_morgen_im_horizont():
    # Morgen sonnig (Score 8): Horizont endet morgen 08:15+3h. Spitze 20-22 Uhr HEUTE zaehlt.
    today = [50.0] * 20 + [200.0, 200.0, 50.0, 50.0]
    peak = _peak(_hass(today, [50.0] * 24, score_morgen="8"))
    assert peak["ve_stunden"] == 2
    # Morgen-Spitzen wuerden NICHT mehr zaehlen (nach 11:15): Gegenprobe.
    tomorrow_mit_spitze = [50.0] * 18 + [200.0] * 3 + [50.0] * 3
    peak2 = _peak(_hass([50.0] * 24, tomorrow_mit_spitze, score_morgen="8"))
    assert peak2["ve_stunden"] == 0


def test_morgen_schlecht_spitze_morgen_zaehlt():
    tomorrow = [50.0] * 18 + [200.0] * 3 + [50.0] * 3
    peak = _peak(_hass([50.0] * 24, tomorrow, score_morgen="1"))
    assert peak["ve_stunden"] == 3


def test_tomorrow_fehlt_nur_heute_rest():
    today = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]
    peak = _peak(_hass(today, []))
    assert peak["gueltig"] is True
    assert peak["ve_stunden"] == 3


def test_kappung_bei_maxsoc():
    # Kleiner Akku (2 kWh): 3 VE-Stunden brauchen 3 kWh -> Kappung bei maxsoc.
    today = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]
    peak = _peak(_hass(today, [50.0] * 24, cap="2.0"))
    assert peak["ges_soc"] == 95.0


def test_weniger_als_4_preise_ungueltig():
    peak = _peak(_hass([50.0, 60.0], []))
    assert peak["gueltig"] is False


def test_sommer_tag_guter_score_horizont_leer():
    mittag = dt.datetime(2026, 6, 20, 12, 0, tzinfo=TZ)
    today = [20.0] * 18 + [80.0, 80.0, 80.0, 20.0, 20.0, 20.0]
    peak = _peak(_hass(today, [20.0] * 24, now=mittag, score_heute="9",
                       sun_state="above_horizon"))
    assert peak["ve_stunden"] == 0
    assert peak["benoetigt_kwh"] == 0.0


def test_sommer_tag_horizont_leer_laufende_stunde_zaehlt_nicht():
    # Randfall zum Test oben: now = 12:30 (NICHT volle Stunde) und die
    # LAUFENDE Stunde (12 Uhr) ist teuer. Der "leere" Horizont (Tag + guter
    # Score) darf die laufende Stunde nicht klassifizieren - sonst geht die
    # Reserve an einem sonnigen Tag an, nur weil die aktuelle Stunde teuer ist.
    mittag_halb = dt.datetime(2026, 6, 20, 12, 30, tzinfo=TZ)
    today = [20.0] * 12 + [200.0] + [20.0] * 11
    peak = _peak(_hass(today, [20.0] * 24, now=mittag_halb, score_heute="9",
                       sun_state="above_horizon"))
    assert peak["ve_stunden"] == 0
    assert peak["benoetigt_kwh"] == 0.0


def test_min_preis_vor_peak_nur_bis_erster_spitze():
    # Dip (30 ct) VOR der Spitze zaehlt, Dip (10 ct) NACH der Spitze nicht.
    today = [50.0] * 18 + [30.0, 200.0, 200.0, 10.0, 50.0, 50.0]
    peak = _peak(_hass(today, [50.0] * 24))
    assert peak["min_preis_vor_peak_ct"] == 30.0


def test_exp_stunden_gehen_in_gesamt_nicht_ve():
    # Preisprofil mit klarer EXP-Schicht (70-79 Perzentil) und VE-Schicht.
    today = [20.0 + i for i in range(24)]           # 20..43 aufsteigend
    tomorrow = [20.0 + i for i in range(24)]
    peak = _peak(_hass(today, tomorrow))
    assert peak["ve_stunden"] > 0
    assert peak["exp_stunden"] > 0
    assert peak["ges_soc"] >= peak["ve_soc"]


def test_viertelstunden_raster_gueltig():
    # 96 Werte (15-min-Raster): slot_h = 24/96 = 0.25 h. 12 teure Slots
    # (19:00-22:00, je 200 ct) inmitten eines flachen 50-ct-Tages -> 3 h VE.
    today = [50.0] * 76 + [200.0] * 12 + [50.0] * 8
    peak = _peak(_hass(today, [50.0] * 24))
    assert peak["gueltig"] is True
    assert peak["benoetigt_kwh"] == 3.0
    assert peak["ve_stunden"] == 3.0


def test_gemischtes_raster():
    # today im Stundenraster (24, flach -> 0 VE-Stunden), tomorrow im
    # Viertelstundenraster (96, mit 12-Slot-Spitze -> 3 VE-Stunden):
    # unterschiedliche slot_h pro Liste, beide muessen korrekt gezaehlt werden.
    today = [50.0] * 24
    tomorrow = [50.0] * 76 + [200.0] * 12 + [50.0] * 8
    peak = _peak(_hass(today, tomorrow, score_morgen="1"))
    assert peak["gueltig"] is True
    assert peak["ve_stunden"] == 3.0
    assert peak["benoetigt_kwh"] == 3.0


def test_unplausible_laenge_ungueltig():
    # 40 Werte passen weder ins Stundenraster (20-27) noch ins
    # Viertelstundenraster (80-108) -> komplette Preisbasis wird verworfen.
    today = [50.0] * 40
    peak = _peak(_hass(today, [50.0] * 24))
    assert peak["gueltig"] is False


def test_dst_grenzfall_92_und_100_gueltig():
    # DST-Tage koennen 92 (Fruehjahr, -1h) oder 100 (Herbst, +1h) Viertelstunden
    # haben statt 96 - beide Grenzfaelle bleiben gueltig (Bereich 80-108).
    today_92 = [50.0] * 92
    peak_92 = _peak(_hass(today_92, [50.0] * 24))
    assert peak_92["gueltig"] is True

    today_100 = [50.0] * 100
    peak_100 = _peak(_hass(today_100, [50.0] * 24))
    assert peak_100["gueltig"] is True


def test_laufende_peak_stunde_zaehlt():
    # now = 20:30, mitten in der 19-22-Uhr-Spitze. Das Fenster beginnt jetzt an
    # der AKTUELLEN vollen Stunde (20:00), nicht erst ab 21:00 - die laufende
    # Peak-Stunde (20 Uhr) zaehlt mit, damit das Gate waehrend der Spitze nicht
    # abfaellt.
    mitten_spitze = dt.datetime(2026, 1, 15, 20, 30, tzinfo=TZ)
    today = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]
    peak = _peak(_hass(today, [50.0] * 24, now=mitten_spitze))
    assert peak["ve_stunden"] == 2  # Stunden 20 + 21
    assert peak["benoetigt_kwh"] > 0


# --- Tuning-Runde: Hebel 1 (oekonomische Peak-Filterung) ---

def test_oekonomische_filterung_flacher_tag_gate_off():
    # 24x 30.0 (heute) + 24x 30.5 (morgen): ohne Filterung waeren die 30.5-
    # Stunden EXP (Perzentil 0.75), obwohl der Spread zum Horizont-Tief nur
    # 0.5 ct betraegt - ein Zwangs-Peak eines flachen Tages.
    today = [30.0] * 24
    tomorrow = [30.5] * 24
    gefiltert = _peak(_hass(today, tomorrow, aufschlag="10"))
    assert gefiltert["benoetigt_kwh"] == 0.0
    assert gefiltert["ve_stunden"] == 0
    assert gefiltert["exp_stunden"] == 0

    ungefiltert = _peak(_hass(today, tomorrow, aufschlag="0"))
    assert ungefiltert["exp_stunden"] == 24
    assert ungefiltert["benoetigt_kwh"] > 0


def test_oekonomische_filterung_spike_tag_unveraendert():
    # Spread 200-50=150 ct >> aufschlag 10 -> Filterung aendert nichts.
    today = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]
    peak = _peak(_hass(today, [50.0] * 24, aufschlag="10"))
    assert peak["ve_stunden"] == 3
    assert peak["benoetigt_kwh"] == 3.0


def test_oekonomische_filterung_grenzfall_gleich_zaehlt():
    # Peak exakt fenster_min (50) + aufschlag (10) = 60 -> zaehlt (>=).
    today = [50.0] * 20 + [60.0, 60.0, 60.0, 60.0]
    tomorrow = [50.0] * 24
    genau = _peak(_hass(today, tomorrow, aufschlag="10"))
    assert genau["ve_stunden"] == 4
    assert genau["fenster_min_ct"] == 50.0
    # Knapp drueber am Schwellwert (10.5) -> faellt raus.
    knapp_drueber = _peak(_hass(today, tomorrow, aufschlag="10.5"))
    assert knapp_drueber["ve_stunden"] == 0


# --- Tuning-Runde: Hebel 2 (peak_preis_ve_avg_ct) ---

def test_peak_preis_ve_avg_nur_ve_stunden():
    # Aufsteigende Preisreihe: ve_stunden und exp_stunden beide > 0, der
    # VE-Durchschnitt muss sich vom Gesamt-Peak-Durchschnitt unterscheiden.
    today = [20.0 + i for i in range(24)]
    tomorrow = [20.0 + i for i in range(24)]
    peak = _peak(_hass(today, tomorrow, aufschlag="0"))
    assert peak["ve_stunden"] > 0
    assert peak["exp_stunden"] > 0
    assert peak["ve_preis_avg_ct"] == 41.0
    assert peak["ve_preis_avg_ct"] != peak["peak_preis_avg_ct"]


def test_peak_preis_ve_avg_none_ohne_peaks():
    mittag = dt.datetime(2026, 6, 20, 12, 0, tzinfo=TZ)
    today = [20.0] * 18 + [80.0, 80.0, 80.0, 20.0, 20.0, 20.0]
    peak = _peak(_hass(today, [20.0] * 24, now=mittag, score_heute="9",
                       sun_state="above_horizon"))
    assert peak["ve_stunden"] == 0
    assert peak["ve_preis_avg_ct"] is None
