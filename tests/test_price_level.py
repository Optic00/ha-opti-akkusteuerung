from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render


def _hass(current, today, tomorrow=None):
    return FakeHass(
        states={"sensor.opti_price_current_ct_kwh": str(current)},
        attrs={"sensor.opti_price_series": {"today": today, "tomorrow": tomorrow or []}},
    )


def _level(hass):
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    entity = find_template_entity(cfg, "sensor", "opti_price_level")
    return render(hass, entity["state"])


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


def test_weniger_als_4_preise_normal():
    assert _level(_hass(99.0, [99.0, 99.0])) == "NORMAL"


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
