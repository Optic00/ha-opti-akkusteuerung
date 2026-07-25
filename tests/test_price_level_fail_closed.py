"""Downstream-Nachweis fuer den Fail-closed-Umbau von opti_price_level.

Kontext (Live-Befund 23./24.07.2026): opti_price_level lieferte bei leerer
Preisreihe NORMAL. Fuer die Strategie war das ein gueltig aussehendes
Mittelpreis-Signal - die Peak-Leiter L1 fiel durch, der Default setzte
'Akku Dynamisch', und beim naechsten erfolgreichen Poll sprang der Modus
zurueck (~15 Episoden in 7 Tagen, teils 20-40 s Verweildauer).

Der Umbau auf 'unavailable' ist nur dann ein Fortschritt, wenn KEIN Konsument
daraus eine neue Zweig-Berechtigung ableitet. Genau das pinnen diese Tests:
jeder preisabhaengige Zweig muss bei 'unavailable' verstummen, statt in einen
Preis-Default zu fallen. Deshalb wird hier die echte choose-Kette ausgewertet
und nicht nur der Sensor selbst.
"""
from __future__ import annotations

import pytest

from .condition_eval import evaluate_condition
from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render
from .test_strategie_paritaet import CHOOSE_OPTIONS, _mode_from_sequence
from .test_strategie_vorschau import BASIS, reserve_attrs

PRICE = "sensor.opti_price_level"
UNAVAIL = ("unavailable", "unknown")


def _hass(overrides):
    overrides = dict(overrides)
    attrs = overrides.pop("_attrs", None) or {}
    states = dict(BASIS)
    states.update(overrides)
    return FakeHass(states=states, attrs=attrs)


def _matching_branches(hass):
    """Aliase aller choose-Optionen, deren Bedingungen greifen (nicht nur die
    erste): so faellt auch ein geschatteter, aber faelschlich scharfer Zweig auf."""
    treffer = []
    for option in CHOOSE_OPTIONS:
        conds = option.get("conditions", [])
        if all(evaluate_condition(hass, c) for c in conds):
            treffer.append(option.get("alias", "?"))
    return treffer


def _preis_zweige():
    """Alle choose-Optionen, die opti_price_level ueberhaupt lesen."""
    def liest_preis(node):
        if isinstance(node, dict):
            if node.get("entity_id") == PRICE:
                return True
            return any(liest_preis(v) for v in node.values())
        if isinstance(node, list):
            return any(liest_preis(v) for v in node)
        return PRICE in node if isinstance(node, str) else False

    return [o for o in CHOOSE_OPTIONS if liest_preis(o.get("conditions", []))]


def test_preiszweige_existieren():
    # Schutz vor einem stillen Leerlauf der Tests unten.
    assert len(_preis_zweige()) >= 4


@pytest.mark.parametrize("zustand", UNAVAIL)
def test_kein_preiszweig_greift_ohne_preisniveau(zustand):
    """Der Kern: mit unbekanntem Preisniveau darf kein preisabhaengiger Zweig
    scharf werden - weder ein Entlade- noch ein Netzlade-Zweig."""
    hass = _hass({
        **LEITER,
        PRICE: zustand,
        "sensor.opti_soc": "85",
        "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0),
    })
    getroffen = set(_matching_branches(hass))
    preis_aliase = {o.get("alias") for o in _preis_zweige()}
    assert getroffen & preis_aliase == set()


LEITER = {
    "sensor.opti_forecast_score": "1",
    "sensor.opti_forecast_score_tomorrow": "1",
    "sensor.opti_peak_reserve_soc": "45",
    "binary_sensor.opti_peak_reserve_aktiv": "on",
    "sensor.opti_price_current_ct_kwh": "50",
}


def test_l1_entlaedt_mit_gueltigem_preis_weiter():
    """Gegenprobe: derselbe Zustand mit gueltigem VERY_EXPENSIVE trifft L1.
    Ohne diesen Test wuerde ein generell kaputter Fixture-Zustand die
    Fail-closed-Tests oben false-green machen."""
    hass = _hass({
        **LEITER,
        PRICE: "VERY_EXPENSIVE",
        "sensor.opti_soc": "85",
        "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0),
    })
    treffer = _matching_branches(hass)
    assert any("Peak-Leiter L1" in alias for alias in treffer)
    erste = next(o for o in CHOOSE_OPTIONS
                 if o.get("alias") == treffer[0])
    assert _mode_from_sequence(erste["sequence"]) == "Akku nur Entladen"


# ---------------------------------------------------------------------------
# Balancing-Watchdog: der Netzlade-Pfad 'Bezahltes Netz' haengt an
# price in ['VERY_CHEAP','CHEAP']. Bei unbekanntem Preisniveau darf er nicht
# ziehen - sonst kauft der Watchdog Netzstrom zu unbekanntem Preis.
# ---------------------------------------------------------------------------

def _watchdog(hass):
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    entity = find_template_entity(cfg, "sensor", "opti_balancing_watchdog")
    return render(hass, entity["state"])


WATCHDOG_BASE = {
    "sensor.opti_soc": "40",
    "counter.tage_seit_akku100": "20",
    "input_number.opti_balancing_intervall_tage": "7",
    "input_number.opti_balancing_karenz_tage": "2",
    "input_number.opti_balancing_max_ct": "15",
    "input_number.opti_einspeiseverguetung_ct": "10",
    "input_boolean.opti_balancing_netzladen": "on",
    "input_number.opti_balancing_spreizungs_schwelle": "0",
    "input_number.opti_balancing_bedarf_cooldown_tage": "0",
    "sun.sun": "below_horizon",
    "sensor.opti_price_current_ct_kwh": "12",
}


def test_watchdog_bezahltes_netz_bei_guenstigem_preis():
    hass = FakeHass(states={**WATCHDOG_BASE, PRICE: "CHEAP"})
    assert _watchdog(hass) == "netz"


@pytest.mark.parametrize("zustand", UNAVAIL)
def test_watchdog_kein_bezahltes_netz_ohne_preisniveau(zustand):
    hass = FakeHass(states={**WATCHDOG_BASE, PRICE: zustand})
    assert _watchdog(hass) == "aus"


def test_watchdog_gratis_netz_bleibt_ohne_preisniveau_moeglich():
    """Bewusste Ausnahme: 'Gratis-Netz' vergleicht den absoluten Preis gegen die
    Einspeiseverguetung und braucht das Perzentil-Niveau nicht. Dieser Pfad darf
    vom Fail-closed NICHT mitgenommen werden."""
    hass = FakeHass(states={**WATCHDOG_BASE, PRICE: "unavailable",
                            "sensor.opti_price_current_ct_kwh": "5"})
    assert _watchdog(hass) == "netz"
