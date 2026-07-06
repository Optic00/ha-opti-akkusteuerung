"""Minimaler HA-Condition-Evaluator fuer die choose-Kette in
automations/opti_strategie.yaml.

Deckt GENAU die dort vorkommenden condition-Typen ab: state, numeric_state
(above/below, jeweils literaler Wert ODER Entity-Referenz), template, or und
sun (after: sunrise / before: sunset). Es gibt in der Kette KEINE and/not/
value_template-numeric_state - taucht so etwas neu auf, wirft der Evaluator
(unbekannter Typ -> Exception), damit der Paritaets-Test laut bricht statt
still zu passen.

Bewusst NICHT nachgebaut wird HAs volle Semantik (z.B. numeric_state ueber
mehrere Entities, for-Delays, Attribut-Vergleiche) - nur was die Automation
real nutzt. Template-Bedingungen laufen ueber die bestehende render()-Funktion
aus ha_harness (identische Jinja-Umgebung wie die Vorschau-Templates)."""
from __future__ import annotations

from .ha_harness import FakeHass, render

_TRUTHY = {"true", "yes", "on", "1", "enable", "enabled"}


def _num(value):
    """State-/Schwellenwert als float, oder None wenn nicht numerisch."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_threshold(hass: FakeHass, raw):
    """above/below aufloesen: Zahl -> float; String -> erst als Zahl, sonst als
    Entity-Referenz (dessen State als float). None wenn unaufloesbar."""
    if isinstance(raw, (int, float)):
        return float(raw)
    direct = _num(raw)
    if direct is not None:
        return direct
    return _num(hass.states(raw))


def _as_list(value):
    if isinstance(value, list):
        return value
    return [value]


def evaluate_condition(hass: FakeHass, cond: dict) -> bool:
    """Wertet EINE HA-Bedingung gegen eine FakeHass-Instanz aus."""
    ctype = cond["condition"]

    if ctype == "state":
        entities = _as_list(cond["entity_id"])
        wanted = _as_list(cond["state"])
        return all(hass.states(e) in wanted for e in entities)

    if ctype == "numeric_state":
        above = cond.get("above")
        below = cond.get("below")
        for entity in _as_list(cond["entity_id"]):
            val = _num(hass.states(entity))
            if val is None:
                return False  # fail-safe: fehlender/kein-Zahl-State -> nicht erfuellt
            if above is not None:
                thr = _resolve_threshold(hass, above)
                if thr is None or not (val > thr):
                    return False
            if below is not None:
                thr = _resolve_threshold(hass, below)
                if thr is None or not (val < thr):
                    return False
        return True

    if ctype == "template":
        result = render(hass, cond["value_template"])
        return result.strip().lower() in _TRUTHY

    if ctype == "or":
        return any(evaluate_condition(hass, c) for c in cond["conditions"])

    if ctype == "sun":
        # In der Automation nur als "Tageslicht": after sunrise & before sunset.
        # Das ist exakt die Vorschau-Definition (is_state('sun.sun','above_horizon')).
        if cond.get("after") == "sunrise" and cond.get("before") == "sunset":
            return hass.states("sun.sun") == "above_horizon"
        raise NotImplementedError(f"sun-Variante nicht unterstuetzt: {cond!r}")

    raise NotImplementedError(f"Unbekannter condition-Typ: {ctype!r}")
