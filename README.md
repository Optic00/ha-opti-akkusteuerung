<img src="https://raw.githubusercontent.com/Optic00/ha-opti-akkusteuerung/main/custom_components/opti_akku/brand/icon.png" alt="Opti Akku" width="128">

# Opti Akku für Home Assistant

Opti Akku verbindet lokale Akkusteuerung mit einer optionalen Strategie für PV-Überschuss, Strompreise und Verbrauchsreserve. Die Einrichtung erfolgt über einen geführten Assistenten. Zusätzliche YAML-Automationen oder manuell angelegte Helfer sind für die Integration nicht erforderlich.

**Beta-Kandidat 0.5.2b6, noch ohne Release.** Die bisherige YAML-Automation bleibt im [Legacy-Branch](https://github.com/Optic00/ha-opti-akkusteuerung/tree/legacy-yaml) und im [Archivtag](https://github.com/Optic00/ha-opti-akkusteuerung/tree/legacy-yaml-2026-09-15) erhalten. Bestehende Anlagen bitte nach der [Migrationsanleitung](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/migration.md) umstellen.

> **Aktive Akkusteuerung:** Freigegebene Schreibzugriffe verändern das Lade- und Entladeverhalten. Falsche Einstellungen oder Fehler können zusätzliche Kosten und unerwünschten Betrieb verursachen. Herstellervorgaben beachten und andere schreibende Steuerungen vor der Freigabe deaktivieren. Neue Einträge starten ohne Schreibfreigabe; Shadow-Einträge bleiben dauerhaft lesend.

## Unabhängiges Projekt und Nutzungshinweise

Opti Akku ist ein unabhängiges Community-Projekt, keine offizielle Integration von SMA, Huawei, BYD oder Tibber. Aus der genannten Kompatibilität folgt keine Freigabe, Zertifizierung oder Supportzusage dieser Unternehmen. Hersteller- und Produktnamen dienen zur Beschreibung der unterstützten Geräte und Schnittstellen; die Rechte daran verbleiben bei den jeweiligen Rechteinhabern.

Die Software wird unter der [MIT-Lizenz](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/LICENSE) ohne zugesicherte Fehlerfreiheit, Verfügbarkeit oder Eignung für eine bestimmte Anlage bereitgestellt. Es gibt keine Zusage bestimmter Einsparungen oder einer bestimmten Batterielebensdauer. Aktive Steuerung kann Kosten, Batterieverschleiß oder Schäden verursachen. Der Lizenztext enthält Gewährleistungs- und Haftungsausschlüsse; zwingende gesetzliche Rechte bleiben unberührt. Warnhinweise und eine Schreibfreigabe sind kein pauschaler Haftungsverzicht. Herstellerbedingungen, Anlagenparameter und einen geeigneten Rückweg vor dem Schreibbetrieb prüfen.

## Voraussetzungen und Geräte

- Home Assistant ab **2026.9.1**, HACS für die komfortable Installation.
- **SMA Sunny Tripower Smart Energy** STP5.0/6.0/8.0/10.0-3SE-40 mit aktiviertem Modbus TCP. Die Einrichtung prüft Geräteprofil und Identität lesend. Der bisherige praktische Pilotumfang ersetzt keine Abnahme jeder Modell-/Firmwarekombination.
- **Huawei Solar ist experimentell:** nutzt eine bereits eingerichtete Huawei-Solar-Integration und deren Entitäten/Dienste. Sie wird nicht ersetzt. Voraussetzungen und Grenzen: [Huawei](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/HUAWEI_CONTROL.md).
- Stromtarif, PV-Prognose, Wärmepumpe und Fahrzeug sind optionale externe Quellen. Anbieter müssen bereits in HA eingerichtet sein.

## Installation mit HACS

1. In HACS unter **Benutzerdefinierte Repositories** `https://github.com/Optic00/ha-opti-akkusteuerung` als Typ **Integration** hinzufügen.
2. **Opti Akku** herunterladen. Solange kein Vorabrelease veröffentlicht ist, bietet HACS nur den Hauptzweig an. Dieser ist Entwicklungsstand und keine stabile Freigabe. Sobald ein Vorabrelease existiert, die Beta-Auswahl aktivieren und die gewünschte Beta wählen.
3. Home Assistant neu starten.
4. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen → Opti Akku** den Assistenten starten. Für den ersten Vergleich Shadow eingeschaltet lassen.

Dies ist ein benutzerdefiniertes HACS-Repository, keine behauptete Aufnahme in die HACS-Standardliste. Wurde das Repository unter einer anderen Kategorie hinzugefügt, in HACS entfernen und als Integration neu hinzufügen. Eine bereits manuell installierte Opti-Akku-Integration nicht löschen oder neu anlegen: [Übernahme durch HACS](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/migration.md#manuell-installierte-integration).

## Funktionen

- SMA-Telemetrie und Steuerung über die Modbus-Schnittstelle von HA; wahlweise Beobachtung, Strategie oder manuelle Betriebsarten.
- Lade-/Entladegrenzen, Hysterese, Preisfenster, Reserveplanung und optionale Balancing-Planung.
- Native Tibber-Preise über die vorhandene HA-Integration oder zugeordnete Preisentitäten.
- Optionales Stundenverbrauchsprofil mit Recorder-Import und gesonderter Aktivierung für die Peak-Reserve.
- Optionale EV-Entladesperre und frühere PV-Ladung des Hausakkus bei Fahrzeug-Ladebedarf.
- Geführtes Einstellungsmenü, Diagnoseentitäten und lesende 24-Stunden-Shadow-Aufzeichnung.

BYD-Zellüberwachung, KI-Tagesreport und eine eigene Wallbox-/Wärmepumpensteuerung gehören nicht dazu. Eine vollständige zukünftige Heizlast- oder Fahrzeug-Rückkehrprognose wird nicht versprochen. Huawei-Hardwarepilot und vollständige EV-/Nacht-/Preispeak-Abnahme des öffentlichen Kandidaten stehen vor einer stabilen Freigabe noch aus.

## Dokumentation

- [Einrichtung und Einstellungen](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/configuration.md)
- [Betrieb, Shadow und Schreibschutz](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/operation.md)
- [Migration und Rückweg](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/migration.md)
- [Strompreise](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/PRICE_SOURCES.md), [Anlagenbilanz](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/PLANT_MODEL.md), [Huawei](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/HUAWEI_CONTROL.md)
- [Bedarfsprofil, Peak-Reserve und EV-Vorbereitung](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/forecast-and-ev.md)
- [Entwicklung und Prüfung](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/docs/development.md)

Die Strategiequellen liegen unter `strategy/`; das daraus erzeugte JSON wird mit der Integration ausgeliefert. Herkunft und Anpassungen sind dort dokumentiert. Gerätetreiber und Strategie bleiben intern getrennt, damit später andere Adapter oder eine eigenständige Strategie angebunden werden können.

Fehler bitte über die [Issue-Vorlage](https://github.com/Optic00/ha-opti-akkusteuerung/issues/new/choose) mit Version, Gerät/Firmware, erwartetem Verhalten und ausgewählten bereinigten Diagnosewerten melden. Issues und Anhänge sind öffentlich. Keine Zugangsdaten, Seriennummern, Adressen, Standort-/Fahrzeugdaten, vollständigen HA-Konfigurationen oder privaten Messhistorien anhängen. Logs, Screenshots und Shadow-Dateien nicht als automatisch anonymisiert ansehen; nur den nötigen Ausschnitt nach eigener Prüfung teilen. Für einen Bericht sind keine vollständigen Dateien erforderlich. Lizenz: [MIT](https://github.com/Optic00/ha-opti-akkusteuerung/blob/main/LICENSE).
