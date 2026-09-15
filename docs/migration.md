# Migration auf Opti Akku

## Bisherige YAML-Automation

Der letzte YAML-Stand einschließlich PR #78 liegt auf `legacy-yaml`; der feste Archivtag ist `legacy-yaml-2026-09-15`. Bestehende Installationen werden durch die Repository-Umstellung nicht automatisch auf die neue Steuerung migriert. Bekannte Legacy-Probleme sind dadurch nicht automatisch behoben.

1. Vollständiges HA-Backup und bisherige YAML-Konfiguration sichern. Alle schreibenden Automationen, Adapter, Wächter und deren Aktivierungszustände erfassen.
2. Opti Akku installieren und im Assistenten **Bisherige Opti-Automation übernehmen** wählen. Der Import erzeugt einen dauerhaft lesenden Shadow-Eintrag. Die Vorschau übernimmt nur bestätigte gültige Helferwerte; Hauptfreigabe, Schreibrechte, Betriebszustände und Lernhistorie werden nicht kopiert.
3. Zuordnungen und Anlagenumfang prüfen. Externe Quellen müssen auch ohne alte Packages verfügbar sein. Die Migrationshilfe löst Template-Ketten nicht bis zum ursprünglichen Anbieter auf.
4. Strategie-Berechnung im Shadow aktivieren, eine 24-Stunden-Aufzeichnung starten und Datenlücken sowie Entscheidungen prüfen. Der Test belegt keine physische Schreibwirkung.
5. Für aktiven Betrieb einen ausdrücklich schreibfähigen Eintrag mit geprüften Einstellungen vorbereiten. Ein Shadow-Eintrag lässt sich nicht nachträglich freischalten. Alle bisherigen Schreiber deaktivieren, Geräteidentität prüfen und erst dann Single-Writer-Bestätigung und Schreibfreigabe setzen.
6. Kontrolliert die erwartete Gerätewirkung und den Rückweg prüfen. Alte Helfer und Packages erst nach Prüfung ihrer Verbraucher und Dashboards aufräumen.

Die Hilfe deaktiviert oder löscht keine alte Automation. Neue Integrationseinträge erzeugen eigene Entitäten; bestehende Dashboards und Dritt-Automationen müssen bewusst auf die neuen Entitäten umgestellt werden. Keine automatische Übernahme alter Entity-IDs versprechen. Optionale BYD-Überwachung bleibt separat.

## Manuell installierte Integration

Für eine vorhandene `custom_components/opti_akku`-Installation ist kein erneuter YAML-Import nötig. Vorher vollständiges HA-Backup einschließlich `.storage` und des Komponentenordners sichern. In HACS dasselbe Repository als Integration hinzufügen und die gewünschte Version herunterladen. Danach HA neu starten und vorhandenen Eintrag, Optionen, Entitäten und Lernhistorie prüfen. Den bestehenden Eintrag nicht löschen und keinen zweiten schreibfähigen Eintrag anlegen. HACS verwaltet die Dateien; es ersetzt kein Backup der gespeicherten Konfiguration.

Die Domain `opti_akku` und vorhandene Identitäten bleiben beim Repositorywechsel unverändert. Alte gespeicherte Schreibfreigaben ohne gültige Gerätebindung können aus Sicherheitsgründen eine erneute bewusste Freigabe verlangen.

## Alte Links

Raw-Downloads unter `main/packages/` oder `main/automations/` werden nicht umgeleitet. Für YAML künftig den Legacy-Branch oder den festen Archivtag verwenden. Beispiel:

- [SMA-Helfer](https://github.com/Optic00/ha-opti-akkusteuerung/blob/legacy-yaml-2026-09-15/packages/sma_helpers.yaml)
- [Strategie-Automation](https://github.com/Optic00/ha-opti-akkusteuerung/blob/legacy-yaml-2026-09-15/automations/opti_strategie.yaml)
- [Alte Installation](https://github.com/Optic00/ha-opti-akkusteuerung/blob/legacy-yaml-2026-09-15/docs/installation.md)

In eigenen Raw-URLs `main` durch `legacy-yaml-2026-09-15` ersetzen. Auch lokale Git-Checkouts für den YAML-Betrieb bewusst auf Legacy wechseln, bevor weitere Aktualisierungen übernommen werden.

## Rückweg

Zuerst neue Schreibzugriffe abschalten und den tatsächlichen Gerätezustand prüfen. Dann das passende Backup aus Integrationscode **und** gespeicherter Konfiguration wiederherstellen oder die gesicherte YAML-Steuerung kontrolliert reaktivieren. Niemals beide Writer gleichzeitig aktivieren. Ein HA-Stopp oder eine bestätigte Pause ist keine garantierte dauerhafte Hardware-Sperre; den Rückweg für das konkrete Gerät vor dem ersten Schreibtest kennen.

Beim ersten HACS-Release existiert noch keine ältere HACS-Version als Rückfall. Deshalb das vorherige funktionierende Paket lokal behalten. Der öffentliche Hauptzweig bleibt nach dem HACS-Umstieg eine vollständige Integration; Fehler werden mit gezielten Korrekturen behoben.
