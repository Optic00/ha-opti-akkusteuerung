"""Read-only, explicit legacy settings import. Never import runtime permissions."""
from __future__ import annotations

from .definitions import NUMBER_DEFINITIONS, SWITCH_DEFINITIONS
from .sources import finite

# Deliberately fixed: new integration settings are never implicitly importable.
MIGRATABLE = ('input_number.akkusteuerung_ladestaerke_soll', 'input_number.akkusteuerung_min_ladestaerke', 'input_number.akkusteuerung_max_ladestaerke', 'input_number.akkusteuerung_entladestaerke_soll', 'input_number.akkusteuerung_min_entladestaerke', 'input_number.akkusteuerung_max_entladestaerke', 'input_number.akkusteuerung_wr_ac_ueberschuss_grenze', 'input_number.akkusteuerung_wr_70proz_ueberschuss_grenze', 'input_number.akkusteuerung_ueberschuss_veto_grenze', 'input_number.akkusteuerung_ueberschuss_veto_aus_grenze', 'input_number.akkusteuerung_ueberschuss_veto_knappheit_faktor', 'input_number.minsoc', 'input_number.maxsoc', 'input_number.ladepreis', 'input_number.mindestpreisdifferenz_lade_entladepreis', 'input_number.opti_peak_verbrauch_kw', 'input_number.opti_einspeiseverguetung_ct', 'input_number.opti_netzlade_spread_ct', 'input_number.opti_peak_min_aufschlag_ct', 'input_number.opti_forecast_optimismus', 'input_number.opti_halte_spread_ct', 'input_number.opti_balancing_intervall_tage', 'input_number.opti_balancing_karenz_tage', 'input_number.opti_balancing_max_ct', 'input_number.opti_balancing_done_soc', 'input_number.opti_balancing_spreizungs_schwelle', 'input_number.opti_balancing_bedarf_cooldown_tage', 'input_boolean.hausakku_aus_netz_laden', 'input_boolean.opti_prognose_netzladen', 'input_boolean.opti_pv_ueberschuss_ladung', 'input_boolean.opti_balancing_netzladen')
PAIRS = (
    ("input_number.minsoc", "input_number.maxsoc"),
    ("input_number.akkusteuerung_min_ladestaerke", "input_number.akkusteuerung_max_ladestaerke"),
    ("input_number.akkusteuerung_min_entladestaerke", "input_number.akkusteuerung_max_entladestaerke"),
    ("input_number.akkusteuerung_ueberschuss_veto_aus_grenze", "input_number.akkusteuerung_ueberschuss_veto_grenze"),
)


def snapshot(states, mapping: dict[str, str]) -> tuple[dict, dict]:
    """Freeze valid values and a complete status report without mutating HA."""
    values, report = {}, {}
    for key in MIGRATABLE:
        entity = mapping.get(key)
        state = states.get(entity) if entity else None
        status = "missing"
        if state is not None:
            status = "invalid"
            if entity.split(".", 1)[0] == key.split(".", 1)[0]:
                if key in SWITCH_DEFINITIONS:
                    if state.state in ("on", "off"):
                        values[key] = state.state == "on"
                        status = "accepted"
                else:
                    definition = NUMBER_DEFINITIONS[key]
                    value = finite(state.state)
                    unit = state.attributes.get("unit_of_measurement") or None
                    if unit != (definition.get("unit_of_measurement") or None):
                        status = "unit"
                    elif value is not None and definition["min"] <= value <= definition["max"]:
                        values[key] = value
                        status = "accepted"
        report[key] = {"entity": entity or "", "status": status}
    for lower, upper in PAIRS:
        if (lower in values or upper in values) and (
            lower not in values or upper not in values or values[lower] > values[upper]
        ):
            for key in (lower, upper):
                values.pop(key, None)
                if report[key]["status"] == "accepted":
                    report[key]["status"] = "pair"
    return values, report


def describe(values: dict, report: dict) -> str:
    """Display the exact frozen proposal, including every skipped setting."""
    return "\n".join(
        f"- `{key}` ← `{item['entity'] or '—'}`: "
        + (str(values[key]) + " " + str(NUMBER_DEFINITIONS.get(key, {}).get("unit_of_measurement", ""))
           if key in values else f"[{item['status']}]")
        for key, item in report.items()
    )
