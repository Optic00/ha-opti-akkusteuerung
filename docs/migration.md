# Migration auf Opti Akku

## Bisherige YAML-Automation

Der letzte YAML-Stand einschließlich PR #78 liegt auf `legacy-yaml`; der feste Archivtag ist `legacy-yaml-2026-09-15`. Bestehende Installationen werden durch die Repository-Umstellung nicht automatisch auf die neue Steuerung migriert. Bekannte Legacy-Probleme sind dadurch nicht automatisch behoben.

1. Vollständiges HA-Backup und bisherige YAML-Konfiguration sichern. Alle schreibenden Automationen, Adapter, Wächter und deren Aktivierungszustände erfassen.
2. Opti Akku installieren und im Assistenten **Bisherige Opti-Automation übernehmen** wählen. Der Import erzeugt einen dauerhaft lesenden Shadow-Eintrag. Die Vorschau übernimmt nur bestätigte gültige Helferwerte; Hauptfreigabe, Schreibrechte, Betriebszustände und Lernhistorie werden nicht kopiert.
3. Zuordnungen und Anlagenumfang prüfen. Externe Quellen müssen auch ohne alte Packages verfügbar sein. Die Migrationshilfe löst Template-Ketten nicht bis zum ursprünglichen Anbieter auf. Auch alte YAML-Modbus-Hubs und andere Abfragen desselben Wechselrichters erfassen. Benötigte Messquellen im Shadow-Test erhalten; vor dem Aufräumen ihre Verbraucher prüfen. Eine zusätzliche Verbindung nicht ungeprüft als konfliktfrei voraussetzen; Verbindungsfehler und die gleichzeitige Abfrage im Shadow-Test beobachten.
4. Strategie-Berechnung im Shadow aktivieren, eine 24-Stunden-Aufzeichnung starten und Datenlücken sowie Entscheidungen prüfen. Der Test belegt keine physische Schreibwirkung.
5. Für aktiven Betrieb über **Integration hinzufügen → Opti Akku → Shadow-Einstellungen
   für aktive Steuerung übernehmen** einen neuen Eintrag vorbereiten. Der Assistent
   übernimmt die geprüften Quellen, Grenzen und Zusatzfunktionen zur Durchsicht.
   Schreibfreigabe, laufende Betriebszustände und Lernhistorien werden nicht kopiert.
   Vor Schreibfreigabe beide Zusammenfassungen vergleichen; ein Test mit anderen
   Quellen oder Parametern ist keine Abnahme dieses Writers. Der alte Shadow-Eintrag
   bleibt lesend. Alle bisherigen Schreiber deaktivieren, Geräteidentität prüfen
   und erst dann Single-Writer-Bestätigung und Schreibfreigabe setzen. Den vollständigen
   Ablauf beschreibt [Vom Shadow-Test zur aktiven Steuerung](operation.md#vom-shadow-test-zur-aktiven-steuerung).
6. Kontrolliert die erwartete Gerätewirkung und den Rückweg prüfen. Alte Helfer und Packages erst nach Prüfung ihrer Verbraucher und Dashboards aufräumen.

Die Hilfe deaktiviert oder löscht keine alte Automation. Neue Integrationseinträge erzeugen eigene Entitäten; bestehende Dashboards und Dritt-Automationen müssen bewusst auf die neuen Entitäten umgestellt werden. Keine automatische Übernahme alter Entity-IDs versprechen. Optionale BYD-Überwachung bleibt separat.

## Manuell installierte Integration

Für eine vorhandene `custom_components/opti_akku`-Installation ist kein erneuter YAML-Import nötig. Vor der Übernahme und vor jedem Beta-Update **Schreibzugriffe freigeben** ausschalten und den tatsächlichen Gerätezustand prüfen. Vorher vollständiges HA-Backup einschließlich `.storage` und des Komponentenordners sichern. In HACS dasselbe Repository als Integration hinzufügen und die gewünschte Version herunterladen. Nach der ersten Übernahme die Version in HACS einmal **erneut herunterladen**, bevor HA neu gestartet wird: HACS 2.0.5 entfernt unbekannte alte Dateien erst beim bereits als installiert erfassten Repository. Danach HA neu starten und vorhandenen Eintrag, Optionen, Entitäten und Lernhistorie prüfen. Erst nach diesen Prüfungen die Schreibzugriffe bewusst wieder freigeben. Den bestehenden Eintrag nicht löschen und keinen zweiten schreibfähigen Eintrag anlegen. HACS verwaltet die Dateien; es ersetzt kein Backup der gespeicherten Konfiguration.

Die Domain `opti_akku` und vorhandene Identitäten bleiben beim Repositorywechsel unverändert. Alte gespeicherte Schreibfreigaben ohne gültige Gerätebindung verlangen eine erneute bewusste Freigabe.

Wurde nach einer HACS-Installation manuell aktualisiert, kann HACS weiterhin die
zuvor heruntergeladene Version anzeigen. Die Dateien und der HACS-Versionsstand
sind dann nicht synchron. Die gewünschte veröffentlichte Beta über HACS erneut
herunterladen und dabei den oben beschriebenen Ablauf mit Backup, abgeschalteter
Schreibfreigabe und Neustart einhalten. Den Versionsstand nicht durch direkte
Änderungen an `.storage` korrigieren.

## Alte Links

Raw-Downloads unter `main/packages/` oder `main/automations/` werden nicht umgeleitet. Für YAML künftig den Legacy-Branch oder den festen Archivtag verwenden. Beispiel:

- [SMA-Helfer](https://github.com/Optic00/ha-opti-akkusteuerung/blob/legacy-yaml-2026-09-15/packages/sma_helpers.yaml)
- [Strategie-Automation](https://github.com/Optic00/ha-opti-akkusteuerung/blob/legacy-yaml-2026-09-15/automations/opti_strategie.yaml)
- [Alte Installation](https://github.com/Optic00/ha-opti-akkusteuerung/blob/legacy-yaml-2026-09-15/docs/installation.md)

In eigenen Raw-URLs `main` durch `legacy-yaml-2026-09-15` ersetzen. Auch lokale Git-Checkouts für den YAML-Betrieb bewusst auf Legacy wechseln, bevor weitere Aktualisierungen übernommen werden.

## Rückweg

Zuerst neue Schreibzugriffe abschalten und den tatsächlichen Gerätezustand prüfen. Dann das passende Backup aus Integrationscode **und** gespeicherter Konfiguration wiederherstellen oder die gesicherte YAML-Steuerung kontrolliert reaktivieren. Niemals beide Writer gleichzeitig aktivieren. Ein HA-Stopp oder eine bestätigte Pause ist keine garantierte dauerhafte Hardware-Sperre; den Rückweg für das konkrete Gerät vor dem ersten Schreibtest kennen.

Den vorherigen funktionierenden Versionsstand und das zugehörige Backup lokal
behalten. Der öffentliche Hauptzweig bleibt nach dem HACS-Umstieg eine vollständige
Integration; Fehler werden mit gezielten Korrekturen behoben.

## Separater YAML-Adapter

Die Hinweise im separaten `ha-modbus-akku-adapter`-Repository beziehen sich auf die YAML-Generation. Dessen Schreiber nicht parallel zu Opti Akku betreiben; die dort erwähnten `packages/sma_helpers.yaml` liegen jetzt im Legacy-Archiv. Das Adapter-Repository wird durch diesen Umstieg nicht automatisch geändert.

## Änderungen für vorhandene b5-Installationen

Der übersetzbare Rohwert des Betriebsart-Selects für reine Beobachtung heißt jetzt `observation` statt `Beobachtung`; die deutsche Anzeige bleibt **Beobachtung**. Eigene Automationen, die diesen Select-Zustand direkt vergleichen oder setzen, entsprechend anpassen. Die Entitäts-ID bleibt erhalten.

Sehr alte SMA-Einträge ohne gespeicherten Backend-Schlüssel verlieren beim erneuten Speichern der Verbindung ihre Single-Writer-Bestätigung. Nach Prüfung derselben Geräteidentität bewusst neu bestätigen; die Integration erteilt keine automatische neue Freigabe.

Bei SMA bleiben Lernhistorie und Betriebsbericht nach einer reinen Änderung von
Host, Port oder Modbus-Geräteadresse erhalten, wenn dieselbe Seriennummer und
unveränderte Messquellen gespeichert sind. Alte gespeicherte Bindungen werden
nur bei nachweislich gleicher Geräte- und Quellenidentität übernommen. Ohne
Seriennummer bleibt die Historie an die Verbindung gebunden. Bei geändertem
Geräteprofil oder Anlagenumfang beginnt die Auswertung neu. Die Schreibfreigabe
bleibt an die vollständige Verbindung gebunden und muss nach einer
Verbindungsänderung neu bestätigt werden.
