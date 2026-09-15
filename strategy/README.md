# Strategieentwicklung

Diese Dateien sind die maßgebliche Strategiequelle von Opti Akku. Die JSON-Datei
unter `custom_components/opti_akku/resources/strategy.json` ist das eingecheckte
Laufzeit-Artefakt. Auf installierten Systemen wird kein YAML geladen.

- `settings.yaml`: virtuelle Parameter, Definitionen und Anfangswerte
- `templates.yaml`: abgeleitete Werte und Diagnosevorlagen
- `decisions.yaml`: Entscheidungszweige und Fail-safe
- `statistics.yaml`: gleitende Statistiken
- `balancing.yaml`: Balancing-Zähler und Abschlussbedingungen
- `provenance.yaml` und `porting_notes.yaml`: historische Herkunft und bewusste Abweichungen

Die Herkunft bezeichnet den einmalig übernommenen Stand, nicht eine laufende
Abhängigkeit. Neue Strategieänderungen erfolgen hier. Keine echte HA-Konfiguration
oder privaten Messreihen einchecken. Virtuelle `input_*`-Schlüssel sind interne
Zustände; Nutzer müssen diese Helfer nicht anlegen.

Nach einer Änderung `python tools/build_strategy_resources.py` ausführen und
Quelle sowie JSON gemeinsam prüfen. `--check` erkennt ein veraltetes Bundle.
`tests/test_engine_parity.py` hält historische Entscheidungsfälle fest; bewusst
verbessertes Verhalten braucht einen begründeten Regressionstest.

`tools/legacy_import_strategy.py` ist ausschließlich ein historisches
Importwerkzeug mit explizitem Ausgabeziel. Es gehört nicht zum Entwicklungs- oder
Release-Build und darf die gepflegten Quellen nicht überschreiben.

## Engine- und Integrationsparameter

`settings.yaml` enthält die virtuellen Engine-Defaults und den historischen
Helfervertrag. Die Standardwerte, Typen und zulässigen Bereiche der HA-Einstellungen
stehen in `custom_components/opti_akku/definitions.py`. Der Coordinator überlagert
die Engine-Eingaben mit diesen gespeicherten Integrationsparametern. Eine Änderung
in `settings.yaml` allein ändert daher nicht zwingend die HA-Standardwerte oder
Menügrenzen. Beide Ebenen bei Parameteränderungen gezielt prüfen; Unterschiede
können beabsichtigte sichere Vorgaben sein und werden nicht automatisch angeglichen.
