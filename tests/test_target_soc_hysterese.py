"""Schmitt-Hysterese von opti_target_soc (packages/opti_derived.yaml, Sensor 3).

Die bestehende Suite (test_derived_sensoren.py) rendert Ziel-SoC ausschliesslich
mit this_attributes={} - also nur den Plain-Pfad, in dem das vorige Level gleich
`plain` ist. Hier wird der Halte-Pfad gepinnt: das Attribut 'level' haelt die
vorige Stufe, bis ratio die Stufengrenze um die Marge m=0.10 ueber-/unterschreitet.

Modell (aus dem Template, Zeilen ~240-252):
    m       = 0.10
    bounds  = [0.375, 0.875, 1.375, 1.875, 2.875]
    targets = [max_soc(95), 90, 80, 70, 60, 50]   # Index = level 0..5
    plain   = Anzahl bounds <= ratio
    prev    = this.attributes.get('level', plain)
    # Hoch: solange level<5 und ratio >= bounds[level] + m  -> level+1
    # Runter: solange level>0 und ratio < bounds[level-1] - m -> level-1
    target  = targets[level]

Ratio wird direkt gestellt: hausverbrauch=0 (60min-Sensor "0") und kein
sun.sun/next_setting -> remaining_hours=6, aber verbrauchsfrei, also
net_available = restproduktion = remaining_today. cap=10 kWh -> ratio = remaining/10.
"""
import os
import pathlib
import tempfile

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

YAML = REPO / "packages" / "opti_derived.yaml"


def _cfg(path=None):
    return load_yaml(path or YAML)


def _entity(cfg=None):
    return find_template_entity(cfg or _cfg(), "sensor", "opti_target_soc")


def _state(hass, cfg=None):
    return render(hass, _entity(cfg)["state"])


def _attr(hass, attr, cfg=None):
    return render(hass, _entity(cfg)["attributes"][attr])


def _states(remaining, cap="10"):
    # hausverbrauch=0, kein sun.sun -> net_available = restproduktion = remaining.
    # cap=10 -> ratio = remaining/10. netzladen off, kein estimate10 -> p10=median.
    return {
        "sensor.opti_battery_capacity_kwh": cap,
        "sensor.opti_forecast_remaining_today_kwh": remaining,
        "sensor.opti_house_consumption_60min_w": "0",
        "input_number.maxsoc": "95",
        "input_number.minsoc": "10",
        "input_boolean.hausakku_aus_netz_laden": "off",
    }


def _mutant_cfg(old, new):
    """opti_derived.yaml nach /private/tmp kopieren, Konstante mutieren, laden."""
    src = YAML.read_text(encoding="utf-8")
    assert old in src, f"Mutations-Anker {old!r} nicht im Template gefunden"
    fd, path = tempfile.mkstemp(suffix=".yaml", dir="/private/tmp")
    os.close(fd)
    p = pathlib.Path(path)
    try:
        p.write_text(src.replace(old, new), encoding="utf-8")
        return _cfg(path)
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Halte-Pfad: Level bleibt oberhalb von `plain` haengen, solange ratio in der
# Marge um die Stufengrenze liegt.
# ---------------------------------------------------------------------------

def test_hysterese_haelt_stufe_und_ist_pfadabhaengig():
    # ratio = 8.5/10 = 0.85. Grenze bounds[1]=0.875, Marge 0.10.
    # plain = |{0.375} <= 0.85| = 1  (0.875 <= 0.85 ist False).
    # prev=2: Runter braucht ratio < bounds[1]-m = 0.775; 0.85 nicht -> level 2 GEHALTEN.
    #         (plain waere 1 -> 90%, gehalten liefert 80%.)
    # prev=1: Hoch braucht ratio >= bounds[1]+m = 0.975; 0.85 nicht -> level 1.
    # Gleiches ratio, zwei Vorzustaende, zwei Ergebnisse = echte Pfadabhaengigkeit.
    st = _states("8.5")

    held = FakeHass(states=st, this_attributes={"level": "2"})
    assert float(_state(held)) == 80.0
    assert _attr(held, "level") == "2"
    branch_held = _attr(held, "branch")
    assert "level 2" in branch_held
    assert "(gehalten)" in branch_held  # ns.l (2) != plain (1)

    lower = FakeHass(states=st, this_attributes={"level": "1"})
    assert float(_state(lower)) == 90.0
    assert _attr(lower, "level") == "1"
    assert "(gehalten)" not in _attr(lower, "branch")  # ns.l (1) == plain (1)


def test_hysterese_haelt_an_unterster_stufe():
    # ratio = 3/10 = 0.30, plain = 0 (kein bound <= 0.30).
    # prev=1: Runter braucht ratio < bounds[0]-m = 0.275; 0.30 nicht -> level 1 GEHALTEN.
    # Ohne Historie waere es Stufe 0 (maxsoc 95%), gehalten liefert 90%.
    st = _states("3")
    held = FakeHass(states=st, this_attributes={"level": "1"})
    assert float(_state(held)) == 90.0
    assert _attr(held, "level") == "1"
    branch = _attr(held, "branch")
    assert "ratio=0.3" in branch
    assert "level 1" in branch
    assert "(gehalten)" in branch


# ---------------------------------------------------------------------------
# Wechsel-Pfad: ueberschreitet ratio die Grenze um mehr als m, wechselt das Level.
# ---------------------------------------------------------------------------

def test_hysterese_wechsel_hoch():
    # prev=1, ratio = 10/10 = 1.0 >= bounds[1]+m = 0.975 -> Aufstieg auf level 2.
    # Naechste Schwelle bounds[2]+m = 1.475 nicht erreicht -> bleibt 2 -> 80%.
    st = _states("10")
    hass = FakeHass(states=st, this_attributes={"level": "1"})
    assert float(_state(hass)) == 80.0
    assert _attr(hass, "level") == "2"


def test_hysterese_wechsel_runter():
    # prev=2, ratio = 7/10 = 0.70 < bounds[1]-m = 0.775 -> Abstieg auf level 1.
    # bounds[0]-m = 0.275 nicht unterschritten -> bleibt 1 -> 90%.
    st = _states("7")
    hass = FakeHass(states=st, this_attributes={"level": "2"})
    assert float(_state(hass)) == 90.0
    assert _attr(hass, "level") == "1"


# ---------------------------------------------------------------------------
# Seed-Fall: ohne 'level'-Key faellt prev auf `plain` zurueck (Plain-Verhalten).
# ---------------------------------------------------------------------------

def test_hysterese_seed_ohne_level_key():
    # ratio=0.85 -> plain=1. Kein 'level'-Key (auch mit fremdem Attribut) ->
    # prev=plain=1 -> level 1 -> 90%, kein Halte-Marker. Kontrast zu prev=2 (80%).
    st = _states("8.5")
    for this_attrs in ({}, {"foo": "bar"}):
        hass = FakeHass(states=st, this_attributes=this_attrs)
        assert float(_state(hass)) == 90.0
        assert _attr(hass, "level") == "1"
        assert "(gehalten)" not in _attr(hass, "branch")


# ---------------------------------------------------------------------------
# Diskriminierung: mit Marge m=0 verschwindet das Halteband -> der gehaltene
# Fall faellt auf `plain` zurueck. Beweist, dass die Tests die Marge pinnen.
# ---------------------------------------------------------------------------

def test_marge_mutant_kollabiert_halteband():
    st = _states("8.5")  # ratio 0.85, prev=2, plain=1
    real = _cfg()
    mutant = _mutant_cfg("{% set m = 0.10 %}", "{% set m = 0.0 %}")

    hass = FakeHass(states=st, this_attributes={"level": "2"})
    # Echtes Template haelt Stufe 2 (80%); mit m=0 kein Halteband -> plain=1 (90%).
    assert float(_state(hass, real)) == 80.0
    assert float(_state(hass, mutant)) == 90.0
    assert _attr(hass, "level", mutant) == "1"
