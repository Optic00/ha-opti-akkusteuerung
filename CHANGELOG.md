# Änderungsprotokoll

Änderungen an Opti Akku, angelehnt an Keep a Changelog, mit den neuesten Versionen zuerst.
Die veröffentlichten Versionen bleiben Beta; automatisierte Tests ersetzen keine
Geräte-, Ausfall- oder Langzeitabnahme. Voraussetzung ist Home Assistant ab 2026.9.1.
Vor Updates ein HA-Backup erstellen und Opti-Schreibzugriffe ausschalten; nach dem
Neustart Quellen und Gerätestatus prüfen, bevor Schreibzugriffe wieder freigegeben werden.

## [Unreleased]

### Hinzugefügt

- SMA Sunny Boy Storage als ausschließlich lesendes Testprofil über Modbus TCP
  ([#170](https://github.com/Optic00/ha-opti-akkusteuerung/issues/170),
  [#171](https://github.com/Optic00/ha-opti-akkusteuerung/pull/171)). Der Eintrag bleibt
  im Shadow-Modus, bietet keine Steuermodi oder Übernahme in eine aktive Steuerung
  und weist jeden Schreibversuch ab. Diagnose und Registerabzug dienen dem
  [SBS-Lesetest](docs/sbs-lesetest.md); Werte, Vorzeichen und Skalierung am Gerät
  sind noch nicht bestätigt.
- Schattenvergleich mit dem evcc-Optimizer (`sensor.opti_evcc_comparison`,
  [#172](https://github.com/Optic00/ha-opti-akkusteuerung/pull/172)). Er steuert
  nichts, ist von Anfang an aktiv und fragt erst ab, wenn unter
  **Erweitert und Diagnose** eine evcc-Quelle eingetragen ist. Die Einrichtung
  lädt die Integration nicht neu.

### Behoben

- Abgelaufene Preisslots sperren kein Peak-Vorladefenster mehr; für die
  Reserveberechnung bleiben sie berücksichtigt
  ([#175](https://github.com/Optic00/ha-opti-akkusteuerung/issues/175),
  [#176](https://github.com/Optic00/ha-opti-akkusteuerung/pull/176)).

## [2026.10-beta11] - 2026-10-27

### Geändert

- CI prüft zusätzlich HA 2026.10.4; die Mindestversion bleibt HA 2026.9.1 (#173).

### Behoben

- Nächtliches Halten bei langem Peak-Horizont und Erholung nach fehlenden
  Eingabewerten korrigiert (#169).

## [2026.10-beta10] - 2026-10-22

### Geändert

- Morgen- und Sonnentag-Score nutzen nach ausreichender Datenabdeckung ein
  gespeichertes gleitendes 24-h-Mittel der Basislast. Bis dahin gilt die bisherige
  60-Minuten-Hochrechnung; `load_source` zeigt den verwendeten Pfad (#124, #163).

### Behoben

- SMA-Nacht-Standby mit Betriebscode 16777213 gilt bei SoC an oder unter MinSOC,
  höchstens 50 W PV- und Akkuleistung und ohne angefordertes Netzladen nicht mehr
  als Verbindungsstörung. Der Schreib-Watchdog pausiert; ein tatsächlicher Fehler
  mit demselben Code bleibt unter diesen Bedingungen vorübergehend unerkannt (#103, #164).
- Anzeigename „Schattenplaner Horizont“ wiederhergestellt; Entity-IDs bleiben erhalten (#165, #166).

## [2026.10-beta9] - 2026-10-21

### Hinzugefügt

- Standardmäßig deaktivierter „Schattenplaner Horizont“ mit Empfehlungen über bis
  zu 36 Stunden und stündlicher Aufzeichnung von PV-Prognosen und Preisen; ohne Steuerung (#160).
- Diagnose „Opti PV Spitze“ zur Freigabe maximaler PV-Ladeleistung bei echtem
  Netto-Überschuss über 200 W und SoC unter 85 %. Die Freigabe hält bis 87 % und
  endet nach 60 Sekunden ohne Überschuss (#159).

### Geändert

- Die Peak-Leiter erlaubt tagsüber PV-Überschussladen über „Akku Dynamisch“ auch
  in teuren Preisphasen, sofern PV-Überschussladen erlaubt ist (#158).
- Prognose-Altersgrenze gilt auch für Bedarfsprognose, Clipping und Planer;
  neue Einträge starten mit zwölf Stunden. Pessimistische 0-kWh-Prognosen sind gültig (#103, #161).
- Routinezustand wird höchstens einmal pro Minute gespeichert, Schutzübergänge
  sofort. „Mindestgewinn beim Entladen“ ist als reine Anzeige gekennzeichnet.

### Behoben

- Zulässiges Peak-Vorladen und Netzladen unter der Einspeisevergütung haben auch
  am oder unter MinSOC Vorrang. Bei bestätigter externer SMA-Sollwertvorgabe wird
  vor dem Netzladen ein verbliebener Entlade-Sollwert gelöscht (#154, #156).
- Langer Peak-Horizont bleibt nachts bei Hauslastschwankungen erhalten (#124, #161).
- Tages-Score ist ohne gültigen Hausverbrauch nicht verfügbar.

## [2026.10-beta8] - 2026-10-20

### Hinzugefügt

- SMA-Betriebscode als Attribut `inverter_status` der Bereitschaftsdiagnose (#103, #149).

### Geändert

- Sperrwächter bewertet nur für den jeweiligen Modus bestätigte Schreibvorgänge,
  frühestens zehn Sekunden nach dem Moduswechsel. Höchstens drei Erneuerungen der
  Sollwerte je Vorfall mit mindestens 30 Sekunden Abstand (#40, #150).
- Lade- und Entladestärke in 1-W-Schritten einstellbar; Huawei rundet weiter auf
  die Schrittweite des Aktors ab (#147, #148).

### Behoben

- SMA-Sicherheits-Pause liest einen veralteten Betriebsstatus erneut, damit sie
  nach langsamen oder fehlgeschlagenen Befehlen nicht allein am Alter des Status
  scheitert. Unzulässige Betriebszustände sperren weiterhin alle Schreibzugriffe (#152).

## [2026.10-beta7] - 2026-10-13

### Hinzugefügt

- Passiver Vergleich von Refill- und Peak-Reserve in `refill_reserve_comparison`
  mit Unterschieden in SoC-Prozentpunkten und kWh sowie gespeicherten Zeitsummen (#42, #134).

### Behoben

- Eigene optionale Altersgrenze für geglätteten Hausverbrauch verhindert falsche
  Quellenfehler bei unverändertem Mittelwert. Ohne Angabe gilt die allgemeine
  Altersgrenze; Setzen oder Ändern startet das Verbrauchsprofil neu (#144).

## [2026.10-beta6] - 2026-10-11

### Hinzugefügt

- Standardmäßig deaktivierte Clipping-Prognose schätzt PV-Energie über der
  AC-Überschussgrenze und vergleicht sie mit freier Akkukapazität (#137, #140).
- Standardmäßig deaktivierte Diagnose zum Zurückhalten von PV-Ladung bis zu
  Preisen unter der Einspeisevergütung mit Energiebilanz; ohne Steuereingriff (#136, #141).

### Geändert

- AC-Überschussgrenze ist schon in der Ersteinrichtung unter „Akku und
  Leistungsgrenzen“ sichtbar. Erkannte SMA-Modelle zeigen einen vorgeschlagenen
  Startwert; er wird nicht automatisch gesetzt, 0 bleibt „aus“ (#135, #139).

## [2026.10-beta5] - 2026-10-11

### Hinzugefügt

- `branch` an „Geplante Ladeleistung“ und ergänzter „Entscheidungsgrund“ machen
  Regeln und Begrenzungen nachvollziehbar; `branch` wird nicht historisiert (#129, #130).
- Passiver Vergleich fester und aus Kosten und Wirkungsgraden berechneter
  Peak-Vorladeschwellen in `precharge_comparison`; ohne Änderung der Steuerung (#41, #131).
- Private Meldung von Sicherheitslücken und eigene Issue-Vorlage für Fragen (#132).

## [2026.10-beta4] - 2026-10-08

### Behoben

- Automatischer MinSOC-Schutz bleibt bei springendem BMS-Ladestand stabil:
  Bei MinSOC 10 % bleibt er bis 13 % aktiv, auch über Neustarts und Sensorlücken.
  Geänderte Grenzen werden neu bewertet; zulässiges Netz-Vorladen und
  Netz-Balancing bleiben oberhalb der harten Untergrenze möglich. Vorschau,
  Entscheidungsgrund und Betriebsdokumentation angepasst (#127).

## [2026.10-beta3] - 2026-10-04

### Geändert

- Reserveplan zeigt Sonnentag-Score, Horizontgrund und Fehler der EV-Bereinigung.
- Neue Einträge wählen bei mittlerem gültigem Sonnentag-Score zunächst den
  kurzen Peak-Horizont; fehlende Scores führen weiter zum langen Horizont.

### Behoben

- Aktivierter EV-Abzug gilt auch für Tages-, Morgen- und Sonnentag-Score;
  bereits ausgeschlossene Wallbox-Leistung wird nicht doppelt abgezogen (#124).
- Nativer Tibber-Abruf wartet auf die vollständig geladene Integration und
  prüft deren Bereitschaft bei regulären Aktualisierungen erneut. Die bisherige
  fünfminütige Fehlerwartezeit entfällt für diesen Startfall; echte Abruffehler
  behalten sie. Kein zusätzlicher Opti-Reload erforderlich.

## [2026.10-beta2] - 2026-10-02

### Behoben

- Rundungsfehler in Lastprofil und Messquellenvergleich behoben: Vollständig
  erfasste Stunden erscheinen nicht mehr fälschlich als Aufwärmphase oder Aufbau
  des Mittelwerts. Echte Messlücken bleiben erkennbar.

## [2026.10-beta1] - 2026-10-02

### Hinzugefügt

- Optionaler EV-Abzug aus Hausverbrauch, Genauigkeitsmessung, Historienimport
  und Peak-Reserve; standardmäßig aus. Aktivierung oder geänderte Zuordnung
  setzt gelernte Profile und Genauigkeitsdaten zurück.
- Zusätzliche Reservewerte bis zum nächsten PV-Nachladen im passiven Journal.

### Geändert

- Wechsel auf den Veröffentlichungsmonat Oktober nach `2026.9-beta11`.
- Quellenalarme nennen betroffene Quellen und dämpfen kurze Fehler, ohne die
  Wartezeit anderer Fehler zu verlängern.

## [2026.9-beta11] - 2026-09-27

### Behoben

- Nachts entfallen HA-Meldung und Push, wenn ausschließlich die optionale
  PV-Erzeugungsquelle ungültig ist. Der Fehler bleibt in Sensor und Diagnose
  sichtbar. Bei Tageslicht, unbekanntem Sonnenstand oder weiteren Quellenfehlern
  wird nach 60 Sekunden gemeldet; ein Tagesalarm wird nachts nach 60 Sekunden entfernt.

## [2026.9-beta10] - 2026-09-25

### Hinzugefügt

- EV-Entladesperre unterstützt evcc-Modus `smart` ab evcc 0.316; `alwaysCharge`
  bleibt ohne eigene HA-Quelle nicht erkennbar.
- Meldung nach 15 Minuten aktiver Strategie ohne Schreibfreigabe.

### Behoben

- Vorladen schaltet innerhalb eines Preisfensters nicht mehr im Viertelstundentakt um.
- Langsame Journal-Schreibvorgänge verzögern die Regelung nicht mehr.
- 24-Stunden-Vergleich und begonnene Bedarfsprognose-Vergleiche laufen nach
  Neustarts weiter; eine stillstehende Referenz wird gekennzeichnet.

## [2026.9-beta9] - 2026-09-22

### Hinzugefügt

- Passiver 24-Stunden-Vergleich für Standard-Einträge mit privatem Journal zu
  Refill-Prognosen, Profilreife und aktiven bzw. vorgeschlagenen Werten. Start und
  Stopp ändern weder Strategie noch Schreibfreigabe oder Abfragetakt.

### Behoben

- Push-Auswahl „Home Assistant“ lässt sich speichern; ein bestehendes
  zusätzliches Push-Ziel kann entfernt werden.

## [2026.9-beta8] - 2026-09-22

### Geändert

- Altersgrenze 0 gilt auch für Ruhe-Zellspreizung und separat zugeordnete
  Wärmepumpen-Leistung. Verfügbarkeit der HA-Quelle wird vertraut; eingefrorene,
  weiterhin verfügbare Werte werden nicht erkannt. Standard bleibt 900 Sekunden.
- „PV-Prognose morgen“ ist bei neuen Installationen aktiviert; bestehende
  deaktivierte Entitäten, IDs und eigene Namen bleiben erhalten.
- Anleitungen zu direkten Quellen, Shadow-Übernahme und Fehlermeldungen
  überarbeitet; fehlende deutsche Texte ergänzt.

## [2026.9-beta7] - 2026-09-21

### Hinzugefügt

- Geprüfte Übernahme der Einstellungen eines geladenen Shadow-Eintrags in einen
  neuen Standard-Eintrag. Shadow bleibt lesend; Strategie und Schreiben starten
  ausgeschaltet. Lerndaten und Aufzeichnungen werden nicht kopiert.
- Diagnose „Letzte SMA-Steuerwerte“ für quittierte Registerwerte, Zeitpunkt und
  Ausführungsstatus; kein unabhängiger Nachweis der Gerätewirkung (#105).
- Altersgrenze 0 für externe HA-Leistungsquellen; Standard bleibt 900 Sekunden.
- Untermenüs für optionale Funktionen und Diagnose sowie Refill-Anzeigen für
  fehlende Energie und Deckungsgrad.

### Geändert

- Ohne Bedarfsprognose entfällt der Diagnoseblock `strategy_comparison`.
- Dokumentation zu Prognoseentitäten und direkter Quellenzuordnung ergänzt (#106).

## [2026.9-beta6] - 2026-09-21

### Geändert

- Alte 0-W-Werte ausdrücklich gewählter Ausschlusslasten dürfen weitergelten,
  wenn die Quelle Ausfälle zuverlässig als `unavailable` meldet. Positive Werte
  und echte Ausfälle bleiben überwacht.
- Prognoseunabhängige Ladegrenze nach der Einrichtung konfigurierbar, auch bei
  automatischer Moduswahl; nach Neustart oder Neuladen aus. Temperatur-, SoC-
  und Balancing-Grenzen bleiben wirksam.
- Verbindungs- und Quellenwarnungen nennen konkrete Prüfschritte.

### Behoben

- Reine Verbindungsänderungen desselben bestätigten SMA-Geräts erhalten
  Lerndaten; die Schreibfreigabe muss erneut geprüft werden.
- Ungültige Release-Tags werden vor dem Paketbau verständlich abgewiesen.

## [2026.9-beta5] - 2026-09-19

### Hinzugefügt

- Optionaler Restwertschutz hält Energie für wertvollere spätere Restlast,
  ohne selbst Netzladung auszulösen.
- Optionale Auto-Vorbereitung berücksichtigt Abfahrtszeit und Ladebedarf.
- Begrenzter Zusatzpuffer bis zehn SoC-Punkte für künftige Preisspitzen über 60 ct/kWh.

### Behoben

- Resttages-Score nutzt bei aktivierter Profiloption das vollständig gelernte
  Onlineprofil statt überbewerteter kurzer Lastspitzen; bei unsicheren Daten
  bleibt die bisherige 60-Minuten-Hochrechnung aktiv (#88).
- Entladung endet auch exakt am eingestellten Mindest-SoC.

## [2026.9-beta4] - 2026-09-18

### Hinzugefügt

- Rein informativer Arbitrage-Vergleich anhand selbst eingetragener Kosten-,
  Zyklen- und Effizienzannahmen.
- Bedarfsprognose zeigt, ob erwarteter PV-Überschuss nach dem Hausverbrauch
  ausreicht, den benötigten Batteriepuffer wieder aufzufüllen.

### Geändert

- SMA-Befehlsnachweis unterscheidet Leistungsmessungen vor und nach dem
  Schreibvorgang; kein Sollwert-Readback oder Nachweis physischer Wirkung.
- Passiver Profilvergleich läuft bei eingeschalteter Bedarfsprognose auch auf
  aktiv steuernden Einträgen, ohne deren Strategie zu ändern.

## [2026.9-beta3] - 2026-09-17

### Behoben

- Unveränderte evcc-Werte für Modus, Ladestatus und Smart Cost werden nicht mehr
  allein wegen ihres Alters verworfen. Nicht verfügbare oder unbekannte Werte
  bleiben ungültig; die Quelle muss Verbindungsverluste an HA melden.
- Fehlermeldungen und Betriebsdokumentation erläutern die Ausfallmeldung durch
  Integrationen und MQTT-Bridges.

## [2026.9-beta2] - 2026-09-17

### Hinzugefügt

- Optionales evcc-`smart_cost_active` sperrt die Hausakku-Entladung bei bewusstem
  günstigen Netzladen im Modus `pv`.
- Passiver Vergleich der 60-Minuten-Prognose mit gelerntem Stundenprofil im
  Shadow-Modus; die aktive Strategie bleibt unverändert.

### Geändert

- Diagnose und Befehlsnachweis trennen Adapterausführung, Sollwert-Rücklesung
  und physische Wirkung deutlicher.

### Behoben

- Ladedeckel beendet Laden bei `maxsoc` auch während gehaltener Peak-Reserve;
  geplante Peak-Entladung und fälliges Balancing behalten Vorrang.

## [2026.9-beta1] - 2026-09-15

### Hinzugefügt

- Erste öffentliche HACS-Beta mit geführter Einrichtung, SMA-STP-SE-Modbus,
  optionaler PV-/Preis-/Verbrauchsstrategie, EV-Entladesperre, Migrationshilfe
  und Shadow-Aufzeichnung. Huawei über die vorhandene Huawei-Solar-Integration
  bleibt experimentell.
- Neue Einträge starten ohne Schreibfreigabe; Shadow-Einträge bleiben lesend.
- Manuelles Installations-ZIP enthält `custom_components/opti_akku/`;
  HACS nutzt den Komponentenordner aus dem Release-Tag.

### Geändert

- Kalender-Version `2026.9-beta1` ersetzt die interne `0.5.2b6`, ohne deren
  Steuerlogik zu ändern. Jahres- und Monatsangabe bezeichnen Opti Akku.
- Frühere YAML-Lösung bleibt im Legacy-Archiv erhalten. Bestehende
  Integrationseinträge bei der Migration beibehalten.

[Unreleased]: https://github.com/Optic00/ha-opti-akkusteuerung/compare/2026.10-beta11...HEAD
[2026.10-beta11]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta11
[2026.10-beta10]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta10
[2026.10-beta9]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta9
[2026.10-beta8]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta8
[2026.10-beta7]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta7
[2026.10-beta6]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta6
[2026.10-beta5]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta5
[2026.10-beta4]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta4
[2026.10-beta3]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta3
[2026.10-beta2]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta2
[2026.10-beta1]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.10-beta1
[2026.9-beta11]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta11
[2026.9-beta10]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta10
[2026.9-beta9]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta9
[2026.9-beta8]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta8
[2026.9-beta7]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta7
[2026.9-beta6]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta6
[2026.9-beta5]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta5
[2026.9-beta4]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta4
[2026.9-beta3]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta3
[2026.9-beta2]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta2
[2026.9-beta1]: https://github.com/Optic00/ha-opti-akkusteuerung/releases/tag/2026.9-beta1
