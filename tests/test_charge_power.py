"""opti_charge_power_w (packages/opti_derived.yaml, Sensor 4).

Modell (Template Zeilen ~386-430):
    soc        = opti_soc | float(50)
    temp       = opti_battery_temp | float(25)
    cap        = opti_battery_capacity_kwh | float(12.8) * 1000   [W]
    max_helper = input_number.akkusteuerung_max_ladestaerke | float(3000)
    score      = opti_forecast_score | int(10)   (fehlt -> 10 = schonend)

    if temp >= 50 or temp <= -5:            -> 0
    else:
        temp_faktor: temp>=45 -> 0.5 ; temp<=0 -> 0.25 ; sonst 1.0
        c-Faktor je Score-Band und SoC:
          score<=1 (aggressiv): soc<40 .40, <70 .30, <90 .20, <97 .10, sonst .05
          score<=4 (moderat):   soc<35 .35, <65 .25, <87 .15, <97 .08, sonst .05
          score>=5 (schonend):  soc<30 .30, <60 .20, <85 .15, <97 .08, sonst .05
        c = cap * faktor
        ergebnis = min(c * temp_faktor, max_helper)
        gewaehlter Balancing-Zweig und soc>=96 -> min(ergebnis, cap*0.02)
        gewaehlter Balancing-Zweig und soc>=92 -> min(ergebnis, cap*0.05)
        danach round(0)

availability: has_value(opti_soc) und has_value(opti_battery_temp)
              und has_value(opti_battery_capacity_kwh).

cap=10 kWh -> 10000 W. max_helper hoch (100000) gewaehlt, damit der Deckel die
c*temp_faktor-Formel nicht verdeckt; ein eigener Test prueft den Deckel gezielt.
"""
import os
import pathlib
import tempfile

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

YAML = REPO / "packages" / "opti_derived.yaml"


def _cfg(path=None):
    return load_yaml(path or YAML)


def _entity(cfg=None):
    return find_template_entity(cfg or _cfg(), "sensor", "opti_charge_power_w")


def _state(hass, cfg=None):
    return render(hass, _entity(cfg)["state"])


def _avail(hass, cfg=None):
    return render(hass, _entity(cfg)["availability"])


def _states(soc, temp, score=None, cap="10", max_helper="100000", watchdog=None):
    s = {
        "sensor.opti_soc": soc,
        "sensor.opti_battery_temp": temp,
        "sensor.opti_battery_capacity_kwh": cap,
        "input_number.akkusteuerung_max_ladestaerke": max_helper,
    }
    if score is not None:
        s["sensor.opti_forecast_score"] = score
    if watchdog is not None:
        s["sensor.opti_balancing_watchdog"] = watchdog
    return s


def _charge_hass_mit_vorschau(states):
    """Vorschau-Grund aus denselben States rendern und dem Power-Sensor geben."""
    hass = FakeHass(states=states)
    vorschau = find_template_entity(_cfg(), "sensor", "opti_strategie_vorschau")
    grund = render(hass, vorschau["attributes"]["grund"])
    hass.attrs_map["sensor.opti_strategie_vorschau"] = {"grund": grund}
    return hass


def _mutant_cfg(old, new):
    """opti_derived.yaml in eine Temp-Datei kopieren, Konstante mutieren, laden."""
    src = YAML.read_text(encoding="utf-8")
    assert old in src, f"Mutations-Anker {old!r} nicht im Template gefunden"
    # Kein fixes dir=: /private/tmp gibt es nur auf macOS, auf Linux (CI)
    # schlaegt mkstemp damit fehl. Der Plattform-Default respektiert TMPDIR.
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    p = pathlib.Path(path)
    try:
        p.write_text(src.replace(old, new), encoding="utf-8")
        return _cfg(path)
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Temperatur-Cutoffs: ausserhalb [-5, 50) wird gar nicht geladen.
# ---------------------------------------------------------------------------

def test_temp_cutoff_heiss():
    # temp >= 50 -> 0 W, unabhaengig von SoC/Score.
    assert float(_state(FakeHass(states=_states("50", "50", score="1")))) == 0.0
    assert float(_state(FakeHass(states=_states("50", "55", score="1")))) == 0.0


def test_temp_cutoff_kalt():
    # temp <= -5 -> 0 W.
    assert float(_state(FakeHass(states=_states("50", "-5", score="1")))) == 0.0
    assert float(_state(FakeHass(states=_states("50", "-10", score="1")))) == 0.0


# ---------------------------------------------------------------------------
# Temperatur-Faktor-Abstufung: 45<=temp<50 -> 0.5 ; -5<temp<=0 -> 0.25 ; sonst 1.0.
# Basis: soc=50, score=5 (schonend) -> c = cap*0.20 = 2000 W.
# ---------------------------------------------------------------------------

def test_temp_faktor_abstufung():
    # temp=25 -> faktor 1.0 -> 2000 W
    assert float(_state(FakeHass(states=_states("50", "25", score="5")))) == 2000.0
    # temp=45 -> faktor 0.5 -> 1000 W (Grenze inklusiv)
    assert float(_state(FakeHass(states=_states("50", "45", score="5")))) == 1000.0
    # temp=0 -> faktor 0.25 -> 500 W (Grenze inklusiv, noch > -5)
    assert float(_state(FakeHass(states=_states("50", "0", score="5")))) == 500.0
    # temp=-4 -> faktor 0.25 -> 500 W (kalt, aber nicht abgeschaltet)
    assert float(_state(FakeHass(states=_states("50", "-4", score="5")))) == 500.0


# ---------------------------------------------------------------------------
# Score-Band-Taper: gleiches soc=50/temp=25, drei Baender liefern drei C-Raten.
#   score<=1: soc<70 -> .30 -> 3000 W
#   score<=4: soc<65 -> .25 -> 2500 W
#   score>=5: soc<60 -> .20 -> 2000 W
# ---------------------------------------------------------------------------

def test_score_band_taper():
    assert float(_state(FakeHass(states=_states("50", "25", score="1")))) == 3000.0
    assert float(_state(FakeHass(states=_states("50", "25", score="0")))) == 3000.0
    assert float(_state(FakeHass(states=_states("50", "25", score="4")))) == 2500.0
    assert float(_state(FakeHass(states=_states("50", "25", score="5")))) == 2000.0


def test_score_fehlt_faellt_auf_schonend():
    # Ohne opti_forecast_score -> int(10) -> schonendes Band (score>=5).
    # soc=50 -> .20 -> 2000 W, identisch zum score=5-Fall.
    hass = FakeHass(states=_states("50", "25", score=None))
    assert float(_state(hass)) == 2000.0


# ---------------------------------------------------------------------------
# Deckel: min(c*temp_faktor, max_helper).
# ---------------------------------------------------------------------------

def test_max_helper_deckel():
    # score=1, soc=50 -> c=3000, faktor 1.0. max_helper=1500 -> gedeckelt auf 1500.
    hass = FakeHass(states=_states("50", "25", score="1", max_helper="1500"))
    assert float(_state(hass)) == 1500.0


# ---------------------------------------------------------------------------
# Balancing-Vollladung: im oberen SoC-Bereich deutlich langsamer ins LFP-Knie.
# Der Zusatzdeckel darf die normale dynamische Ladung nicht veraendern.
# ---------------------------------------------------------------------------

def test_balancing_taper_ab_92_prozent():
    # 12,8 kWh * 0,05C = 640 W statt regulaer 0,08C = 1024 W.
    for watchdog in ("pv", "netz"):
        hass = _charge_hass_mit_vorschau(
            _states("93", "25", score="5", cap="12.8", watchdog=watchdog)
        )
        assert float(_state(hass)) == 640.0


def test_balancing_taper_ab_96_prozent():
    # 12,8 kWh * 0,02C = 256 W statt regulaer 0,05C = 640 W.
    for watchdog in ("pv", "netz"):
        hass = _charge_hass_mit_vorschau(
            _states("97", "25", score="5", cap="12.8", watchdog=watchdog)
        )
        assert float(_state(hass)) == 256.0


def test_balancing_taper_inaktiv_laesst_regulaeren_taper_unveraendert():
    hass = FakeHass(states=_states("93", "25", score="5", cap="12.8",
                                  watchdog="aus"))
    assert float(_state(hass)) == 1024.0


def test_watchdog_faellig_aber_hoeherer_ladezweig_bleibt_unveraendert():
    # Der Watchdog ist faellig, aber Negativpreis-Laden steht in der Strategie
    # davor und gewinnt. Dieser normale Ladefall darf nicht gedrosselt werden.
    states = _states("93", "25", score="1", cap="12.8", watchdog="netz")
    states.update({
        "input_boolean.opti_prognose_netzladen": "on",
        "sensor.opti_price_current_ct_kwh": "3",
        "input_number.opti_einspeiseverguetung_ct": "8",
        "input_number.maxsoc": "100",
    })
    hass = _charge_hass_mit_vorschau(states)
    assert hass.attrs_map["sensor.opti_strategie_vorschau"]["grund"].startswith(
        "Negativpreis-Laden"
    )
    assert float(_state(hass)) == 1280.0


# ---------------------------------------------------------------------------
# Availability: alle drei Quell-Sensoren muessen einen Wert haben.
# ---------------------------------------------------------------------------

def test_availability_vollstaendig():
    assert _avail(FakeHass(states=_states("50", "25", score="5"))) == "True"


def test_availability_temp_unavailable():
    st = _states("50", "25", score="5")
    st["sensor.opti_battery_temp"] = "unavailable"
    assert _avail(FakeHass(states=st)) == "False"


def test_availability_soc_unavailable():
    st = _states("50", "25", score="5")
    st["sensor.opti_soc"] = "unavailable"
    assert _avail(FakeHass(states=st)) == "False"


def test_availability_kapazitaet_unavailable():
    st = _states("50", "25", score="5")
    st["sensor.opti_battery_capacity_kwh"] = "unavailable"
    assert _avail(FakeHass(states=st)) == "False"


# ---------------------------------------------------------------------------
# Diskriminierung: verschiebt man den Heiss-Cutoff (>=50 -> >=999), laedt der
# Sensor bei 50 statt abzuschalten. Pinnt, dass der Cutoff-Wert getestet wird.
# ---------------------------------------------------------------------------

def test_temp_cutoff_mutant_laedt_bei_50():
    # score=5, soc=50 -> c=2000; bei temp=50 im else-Zweig faktor 0.5 -> 1000 W.
    st = _states("50", "50", score="5")
    real = _cfg()
    mutant = _mutant_cfg("temp >= 50", "temp >= 999")
    assert float(_state(FakeHass(states=st), real)) == 0.0
    assert float(_state(FakeHass(states=st), mutant)) == 1000.0
