from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render


def _hass(current, today, tomorrow=None):
    return FakeHass(
        states={"sensor.opti_price_current_ct_kwh": str(current)},
        attrs={"sensor.opti_price_series_stable": {"today": today, "tomorrow": tomorrow or []}},
    )


def _entity():
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    return find_template_entity(cfg, "sensor", "opti_price_level")


def _level(hass):
    return render(hass, _entity()["state"])


def _available(hass):
    return render(hass, _entity()["availability"])


def test_flache_preise_sind_normal():
    # Tie-Bug: mit select('le') war pct=1.0 -> VERY_EXPENSIVE. Midrank: pct=0.5 -> NORMAL.
    assert _level(_hass(30.0, [30.0] * 24)) == "NORMAL"


def test_plateau_mit_spitze_bleibt_normal():
    # Bens Szenario: 21x 50 ct flach, 3x 200 ct Spitze. Plateau-Stunde ist NORMAL.
    today = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]
    assert _level(_hass(50.0, today)) == "NORMAL"


def test_spitze_ist_very_expensive():
    today = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]
    assert _level(_hass(200.0, today)) == "VERY_EXPENSIVE"


def test_unterscheidbare_preise_wie_bisher():
    # Streng monotone Preise: Midrank == altes Verhalten minus halbem Selbst-Tie.
    today = [float(10 + i) for i in range(24)]  # 10..33
    assert _level(_hass(10.0, today)) == "VERY_CHEAP"
    assert _level(_hass(33.0, today)) == "VERY_EXPENSIVE"
    assert _level(_hass(21.0, today)) == "NORMAL"


# ---------------------------------------------------------------------------
# Fail-closed bei fehlender Preisreihe (Live-Befund 23./24.07.2026).
# Vorher lieferte der Sensor NORMAL, sobald die Reihe leer war - fuer die
# Strategie ein gueltig aussehendes Mittelpreis-Signal. Jetzt: unavailable.
# ---------------------------------------------------------------------------

def test_weniger_als_4_preise_ist_unavailable():
    assert _available(_hass(99.0, [99.0, 99.0])) == "False"
    assert _level(_hass(99.0, [99.0, 99.0])) == "unavailable"


def test_leere_preisreihe_ist_unavailable():
    # Der beobachtete Tibber-REST-Ausfall: Skalarpreis da, Reihe weg.
    assert _available(_hass(42.4, [])) == "False"
    assert _level(_hass(42.4, [])) == "unavailable"


def test_fehlendes_reihen_attribut_ist_unavailable():
    hass = FakeHass(states={"sensor.opti_price_current_ct_kwh": "42.4"}, attrs={})
    assert _available(hass) == "False"
    assert _level(hass) == "unavailable"


def test_nicht_numerische_reihe_ist_unavailable():
    # Reihe vorhanden, aber unbrauchbar: darf nicht als 4 Preise durchgehen.
    assert _available(_hass(42.4, ["a", "b", None, "c"])) == "False"


def test_reihe_aus_dicts_zaehlt_mit():
    # Tibber-Format (Dicts mit 'total'): availability muss dieselbe Parse-Logik
    # wie state benutzen, sonst waere der Sensor dauerhaft unavailable.
    today = [{"total": 0.30 + i / 100} for i in range(24)]
    assert _available(_hass(0.30, today)) == "True"
    assert _level(_hass(0.30, today)) == "VERY_CHEAP"


def test_reihe_verteilt_auf_today_und_tomorrow():
    # 2 + 2 Werte erreichen die Schwelle gemeinsam.
    assert _available(_hass(50.0, [50.0, 50.0], [50.0, 50.0])) == "True"
    assert _available(_hass(50.0, [50.0, 50.0], [50.0])) == "False"


def test_skalarpreis_unavailable_bleibt_unavailable():
    hass = FakeHass(
        states={"sensor.opti_price_current_ct_kwh": "unavailable"},
        attrs={"sensor.opti_price_series_stable": {"today": [30.0] * 24, "tomorrow": []}},
    )
    assert _available(hass) == "False"


def test_vollstaendige_reihe_ist_verfuegbar():
    assert _available(_hass(30.0, [30.0] * 24)) == "True"


# ---------------------------------------------------------------------------
# Legacy-Pendant sma_templates.yaml::strompreis_niveau (Backport des Tie-Fixes,
# Commit 3): gleicher Midrank-Fix, andere Quell-Entity (sensor.DEIN_STROMPREIS).
# ---------------------------------------------------------------------------

def _legacy_hass(current, today, tomorrow=None):
    return FakeHass(
        states={"sensor.DEIN_STROMPREIS": str(current)},
        attrs={"sensor.DEIN_STROMPREIS": {"today": today, "tomorrow": tomorrow or []}},
    )


def _legacy_level(hass):
    cfg = load_yaml(REPO / "packages" / "sma_templates.yaml")
    entity = find_template_entity(cfg, "sensor", "strompreis_niveau")
    return render(hass, entity["state"])


def test_legacy_flache_preise_sind_normal():
    # Tie-Bug wie im Canonical-Sensor: mit select('le') war pct=1.0 -> VERY_EXPENSIVE.
    # Midrank: pct=0.5 -> NORMAL.
    assert _legacy_level(_legacy_hass(30.0, [30.0] * 24)) == "NORMAL"
