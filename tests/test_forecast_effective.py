"""sensor.opti_forecast_effective_remaining_kwh (packages/opti_derived.yaml).

Zentraler Blend-Sensor zwischen Median (opti_forecast_remaining_today_kwh) und
dessen P10 (estimate10-Attribut), gesteuert ueber input_number.opti_forecast_optimismus
(alpha in % 0..100). Formel:
    p10     = estimate10 falls > 0, sonst median (Solcast-"keine Schaetzung"-Guard)
    alpha   = clamp(helper, 0, 100) / 100
    blended = alpha * median + (1 - alpha) * p10
    state   = min(median, blended)   # nie optimistischer als der Median

alpha=0 (Default/Erststart, kein Helfer-initial) ist bit-identisch zum
bisherigen min(median, p10)-Verhalten der Konsumenten (opti_target_soc,
opti_forecast_score) - das ist die Regressions-Garantie fuer Feature #30.

Diese Datei uebernimmt zusaetzlich die reine P10-Auswahl-Semantik, die vorher
in test_derived_sensoren.py an opti_target_soc haing (jetzt Konsumenten-seitig
nur noch ein Passthrough des Effective-Sensors) - keine Coverage-Luecke.
"""
import os
import pathlib
import tempfile

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

YAML = REPO / "packages" / "opti_derived.yaml"
UID = "opti_forecast_effective_remaining_kwh"


def _cfg(path=None):
    return load_yaml(path or YAML)


def _entity(cfg=None):
    return find_template_entity(cfg or _cfg(), "sensor", UID)


def _state(hass, cfg=None):
    return render(hass, _entity(cfg)["state"])


def _attr(hass, attr, cfg=None):
    return render(hass, _entity(cfg)["attributes"][attr])


def _availability(hass, cfg=None):
    return render(hass, _entity(cfg)["availability"])


def _states(remaining="10", estimate10=None, alpha=None):
    states = {"sensor.opti_forecast_remaining_today_kwh": remaining}
    if alpha is not None:
        states["input_number.opti_forecast_optimismus"] = alpha
    attrs = {}
    if estimate10 is not None:
        attrs["sensor.opti_forecast_remaining_today_kwh"] = {"estimate10": estimate10}
    return states, attrs


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
# alpha=0 (Default/Erststart) = heutiges min(median, p10)-Verhalten.
# ---------------------------------------------------------------------------

def test_alpha_0_helper_explizit_null():
    states, attrs = _states(remaining="10", estimate10=3, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 3.0


def test_alpha_0_helper_fehlt_ganz():
    # Kein input_number gesetzt -> float(0) Fallback -> identisch zu alpha="0".
    states, attrs = _states(remaining="10", estimate10=3, alpha=None)
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 3.0


def test_alpha_0_ohne_estimate10_faellt_auf_median():
    states, attrs = _states(remaining="10", estimate10=None, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 10.0


def test_alpha_0_estimate10_null_faellt_auf_median():
    states, attrs = _states(remaining="10", estimate10=0, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 10.0


def test_alpha_0_estimate10_hoeher_als_median_median_gewinnt():
    states, attrs = _states(remaining="10", estimate10=20, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 10.0


# ---------------------------------------------------------------------------
# alpha=50 / alpha=100: Blend Richtung Median.
# ---------------------------------------------------------------------------

def test_alpha_50_blend():
    states, attrs = _states(remaining="10", estimate10=4, alpha="50")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 7.0


def test_alpha_100_reiner_median():
    states, attrs = _states(remaining="10", estimate10=4, alpha="100")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 10.0


# ---------------------------------------------------------------------------
# Clamp: Helper-Werte ausserhalb [0,100] werden auf die Grenzen geklemmt.
# ---------------------------------------------------------------------------

def test_clamp_ueber_100_wie_100():
    states, attrs = _states(remaining="10", estimate10=4, alpha="150")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 10.0


def test_clamp_unter_0_wie_0():
    states, attrs = _states(remaining="10", estimate10=3, alpha="-10")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 3.0


# ---------------------------------------------------------------------------
# Attribute: median_kwh / p10_kwh / alpha.
# ---------------------------------------------------------------------------

def test_attribute_median_p10_alpha():
    states, attrs = _states(remaining="10", estimate10=4, alpha="50")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_attr(hass, "median_kwh")) == 10.0
    assert float(_attr(hass, "p10_kwh")) == 4.0
    assert float(_attr(hass, "alpha")) == 0.5


def test_attribute_p10_kwh_faellt_bei_fehlendem_estimate10_auf_median():
    states, attrs = _states(remaining="10", estimate10=None, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_attr(hass, "p10_kwh")) == 10.0


def test_attribute_alpha_geklemmt():
    states, attrs = _states(remaining="10", estimate10=4, alpha="150")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_attr(hass, "alpha")) == 1.0


# ---------------------------------------------------------------------------
# Availability: haengt nur am Median-Sensor.
# ---------------------------------------------------------------------------

def test_availability_folgt_remaining_today():
    hass_ok = FakeHass(states={"sensor.opti_forecast_remaining_today_kwh": "10"})
    assert _availability(hass_ok) == "True"

    hass_missing = FakeHass(
        states={"sensor.opti_forecast_remaining_today_kwh": "unavailable"})
    assert _availability(hass_missing) == "False"


# ---------------------------------------------------------------------------
# P10-Auswahl-Semantik (verschoben aus test_derived_sensoren.py, vormals an
# opti_target_soc gepinnt - jetzt zentral am Effective-Sensor).
# ---------------------------------------------------------------------------

def test_p10_niedriger_als_median_wird_verwendet():
    states, attrs = _states(remaining="8", estimate10=3, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 3.0


def test_p10_sonnenuntergang_randfall():
    # Median und P10 beide 0 -> estimate10 <= 0 -> Fallback Median (ebenfalls 0).
    states, attrs = _states(remaining="0", estimate10=0, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)
    assert float(_state(hass)) == 0.0


# ---------------------------------------------------------------------------
# Mutations-Diskriminierung (permanent, Muster wie test_target_soc_hysterese.py):
# entfernt man die aeussere min(median, blended), waere der Kern-Test bei
# est10 > median und alpha=0 optimistischer (20 statt 10).
# ---------------------------------------------------------------------------

def test_aeussere_min_mutant_waere_optimistischer():
    real = _cfg()
    mutant = _mutant_cfg(
        "{{ ([median, blended] | min) | round(3) }}",
        "{{ blended | round(3) }}",
    )
    states, attrs = _states(remaining="10", estimate10=20, alpha="0")
    hass = FakeHass(states=states, attrs=attrs)

    assert float(_state(hass, real)) == 10.0
    assert float(_state(hass, mutant)) == 20.0
