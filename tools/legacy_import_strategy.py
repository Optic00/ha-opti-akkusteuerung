#!/usr/bin/env python3
"""Historical one-time importer. Not used by the HACS build.

Never regenerate the maintained HACS strategy from a newer legacy checkout.

Run with an explicit source checkout. Files are read from its HEAD, never from
ignored site mappings or a Home Assistant configuration directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

SOURCE_FILES = (
    "packages/opti_derived.yaml",
    "packages/opti_ev_sperre.yaml",
    "packages/sma_helpers.yaml",
    "packages/sma_statistik.yaml",
    "automations/opti_strategie.yaml",
    "automations/opti_balancing_counter.yaml",
)

# Safe initial configuration, using the documented recommended core settings.
# Physical writes are a separate, initially disabled coordinator capability.
RECOMMENDED = {
    "input_select.akkusteuerung_modus": "Akku Pause",
    "input_number.minsoc": 10,
    "input_number.maxsoc": 95,
    "input_number.akkusteuerung_max_ladestaerke": 3000,
    "input_number.akkusteuerung_max_entladestaerke": 5000,
    "input_number.akkusteuerung_ladestaerke_soll": 2000,
    "input_number.akkusteuerung_entladestaerke_soll": 2000,
    "input_number.opti_peak_verbrauch_kw": 0.8,
    "input_number.opti_netzlade_spread_ct": 10,
    "input_number.opti_peak_min_aufschlag_ct": 5,
    "input_number.opti_halte_spread_ct": 5,
    "input_number.mindestpreisdifferenz_lade_entladepreis": 0.08,
    "input_number.akkusteuerung_ueberschuss_veto_grenze": 500,
    "input_number.akkusteuerung_ueberschuss_veto_aus_grenze": 250,
    "input_number.akkusteuerung_ueberschuss_veto_knappheit_faktor": 1,
    "input_number.opti_balancing_karenz_tage": 3,
    "input_number.opti_balancing_bedarf_cooldown_tage": 5,
    "input_boolean.opti_prognose_netzladen": "on",
    "input_boolean.opti_pv_ueberschuss_ladung": "on",
}



def adapt_peak_calendar(template: str) -> str:
    """Keep real 1h/15min slots through short days and the repeated DST hour."""
    replacements = {
        "{% if laenge not in [23, 24, 25, 92, 96, 100] %}":
            "{% set base = heute if key == 'today' else heute + timedelta(days=1) %} "
            "{% set day_hours = ((base + timedelta(days=1)).timestamp() - base.timestamp()) / 3600 %} "
            "{% if laenge not in [day_hours, day_hours * 4] %}",
        "{% set slot_h = 24 / laenge %}\n    {% set base = heute if key == 'today' else heute + timedelta(days=1) %}":
            "{% set slot_h = 1 if laenge == day_hours else 0.25 %}",
        "base + timedelta(hours=loop.index0 * slot_h)":
            "(base | as_utc) + timedelta(hours=loop.index0 * slot_h)",
        "{% set horizont_max = now() + timedelta(hours=36) %}":
            "{% set horizont_max = ((now() | as_utc) + timedelta(hours=36)) | as_local %}",
    }
    for old, new in replacements.items():
        if template.count(old) != 1:
            raise ValueError("Upstream peak template changed; inspect DST adaptation")
        template = template.replace(old, new)
    start = template.index("{# 1. Preisbasis")
    end = template.index("#}", start) + 2
    template = template[:start] + (
        "{# Integration adaptation: validate each list against the actual local "
        "calendar day (23/24/25h or 92/96/100 quarter-hours). Add fixed-duration "
        "slots to UTC midnight; this preserves both occurrences of a repeated "
        "hour without inventing a spring-forward hour. #}"
    ) + template[end:]
    return template


def build(source: Path) -> dict:
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    loaded = {}
    provenance = {}
    for relative in SOURCE_FILES:
        content = subprocess.check_output(
            ["git", "-C", str(source), "show", f"{revision}:{relative}"]
        )
        loaded[relative] = yaml.safe_load(content)
        provenance[relative] = {"sha256": hashlib.sha256(content).hexdigest()}

    helpers = loaded["packages/sma_helpers.yaml"]
    definitions = {
        f"{domain}.{key}": {"domain": domain, **definition}
        for domain, entries in helpers.items()
        for key, definition in entries.items()
    }
    defaults = {}
    for entity, definition in definitions.items():
        domain = definition["domain"]
        if domain == "input_number":
            value = definition.get("initial", definition["min"])
        elif domain == "counter":
            value = definition.get("initial", 0)
        elif domain == "input_boolean":
            value = "on" if definition.get("initial", False) else "off"
        elif domain == "input_select":
            value = definition.get("initial", definition["options"][0])
        else:
            value = definition.get("initial", "")
        defaults[entity] = value
    defaults.update(RECOMMENDED)
    blocks = []
    for source_file in ("packages/opti_derived.yaml", "packages/opti_ev_sperre.yaml"):
        for block in loaded[source_file]["template"]:
            if "peak" in block.get("variables", {}):
                block["variables"]["peak"] = adapt_peak_calendar(block["variables"]["peak"])
            blocks.append({"source": source_file, **block})
    return {
        "schema_version": 1,
        "source_revision": revision,
        "source_repository": "ha-akkusteuerung-sma",
        "source_files": provenance,
        "helper_definitions": definitions,
        "helper_defaults": defaults,
        "template_blocks": blocks,
        "statistics": loaded["packages/sma_statistik.yaml"]["sensor"],
        "strategy": loaded["automations/opti_strategie.yaml"][0],
        "fail_safe": loaded["automations/opti_strategie.yaml"][1],
        "balancing_automations": loaded["automations/opti_balancing_counter.yaml"],
        "porting_notes": [
            "Internal entity IDs are virtual state keys, never input_* entities installed in HA.",
            "Core-data failure pauses immediately; source YAML debounces running-state failure for ten seconds.",
            "Pending binary-sensor delays restart after engine restore; offline time is not confirmation.",
            "Statistics use evaluate observations, a bounded arithmetic sample mean; source event frequency can differ from HA statistics.",
            "Balancing confirms at most one minute per observed minute boundary; offline minutes never count.",
            "Balancing daily increments are observed at 23:59 local time; missed offline days are not backfilled.",
            "The configurable balancing done-SoC persists, unlike the YAML initial value applied on every HA restart.",
            "BYD-specific raw cell monitoring and AI analysis are optional external features, not bundled hardware support.",
            "Peak slots use actual local calendar day lengths and UTC arithmetic, correcting the source DST approximation. The source hourly floor for quarter-hour reserve remains conservative.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--output", type=Path,
        required=True,
    )
    args = parser.parse_args()
    result = build(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Bundled {len(SOURCE_FILES)} tracked files from {result['source_revision']}")


if __name__ == "__main__":
    main()
