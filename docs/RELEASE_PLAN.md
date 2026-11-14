# Release-Plan: von Beta zur stabilen Version

Stand: 14.11.2026, geprüft gegen `main` auf `9bf0107` (2026.11-beta8).

Dieses Dokument ergänzt die [Qualitäts-Roadmap #85](https://github.com/Optic00/ha-opti-akkusteuerung/issues/85).
#85 verfolgt die fachlichen und Hardware-Abnahmen; hier stehen die formalen
Prüfpunkte aus den [HACS-Anforderungen](https://hacs.xyz/docs/publish/integration/)
und der [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules)
von Home Assistant sowie der Release-Prozess. Die Qualitätsskala dient wie in
#85 als Prüfliste, nicht als offizielle Einstufung.

## Kurzfazit

Die technische Basis ist für eine Custom-Integration ungewöhnlich weit. CI mit
Hassfest, HACS-Validierung, Ruff, Mypy, Pytest gegen zwei HA-Versionen,
Coverage-Gate und reproduzierbarem Paketbau läuft und ist als Pflicht-Check
für `main` geschützt. Diagnoseexport, Übersetzungen (de/en vollständig
synchron), Options-Flow, Unique-ID-Prüfung und `runtime_data` sind vorhanden.

Was eine stabile Version noch aufhält, sind vor allem drei Dinge:

1. die offenen Abnahmen aus #85 (SMA-Hardware, Planer, Mehrtagesbetrieb),
2. ein fehlender Stabilisierungsschritt im Release-Prozess (Feature-Freeze und
   Release Candidate statt weiterer Feature-Betas),
3. einige Lücken in der Qualitätsskala, die Nutzer direkt sehen:
   unübersetzte Fehlermeldungen, wenige Feldbeschreibungen im Einrichtungsdialog
   und Hinweise über `persistent_notification` statt Reparaturen.

## Priorisierte Lückenliste

Legende: **P0** blockiert die stabile Version, **P1** vor der stabilen Version
erledigen, **P2** bald danach, **P3** Pflege und Komfort.

### P0: Blocker für die stabile Version

| # | Lücke | Nachweis | Vorschlag |
|---|---|---|---|
| 0.1 | Offene Abnahmen aus #85 | #151 (SMA-Hardware und Ausfallverhalten), #155 (Schattenplaner gegen echten Verlauf), Mehrtagesbetrieb, getrennter Huawei-Nachweis, Restore | Bleiben in #85; stabile Version erst nach Haken dort. Huawei ggf. ausdrücklich als „experimentell“ aus dem Stabilitätsversprechen ausnehmen (README nennt es schon so). |
| 0.2 | Kein Feature-Freeze vor Stabil | beta3 bis beta8 in vier Tagen, darin neue Funktionen (#219, #223, #225, #227) und ein Schreibstillstand-Fix (#229) | Release-Candidate-Phase einführen: ab `2026.MM-rc1` nur noch Fehlerkorrekturen, Mindestlaufzeit z. B. 14 Tage auf mindestens einer echten Anlage ohne neuen Critical/Important-Befund. Regel in `docs/development.md` aufnehmen. |
| 0.3 | Stabilitätsversprechen ungeklärt | README und CHANGELOG sagen „bleibt Beta“ | Vor `.0` festlegen, was „stabil“ heißt: welche Backends/Profile unterstützt sind (SMA STP SE), was experimentell bleibt (Huawei, SBS, Sunny Island, Peak-Rangfolge steuernd) und wie Breaking Changes angekündigt werden. In README als Abschnitt „Unterstützungsumfang“. |
| 0.4 | Versionssprung Beta → Stabil nicht getestet | Schema `2026.11-beta8` → geplant `2026.12.0` | Einmal in HACS prüfen, dass `2026.MM.0` als neuer als die letzte Beta erkannt und Nutzern ohne Beta-Schalter angeboten wird (AwesomeVersion-Vergleich). Kurzer Test in `tests/` für den Versionsvertrag in `tools/build_package.py` reicht. |

### P1: Lücken der Qualitätsskala, die Nutzer sehen

| # | Regel (Stufe) | Befund | Vorschlag |
|---|---|---|---|
| 1.1 | `exception-translations` (Gold), `action-exceptions` (Silber) | Rund 30 `HomeAssistantError` mit fest eingebautem Text, gemischt Deutsch und Englisch (z. B. „Wechselrichter ist nicht erreichbar“ neben „Historical import failed …“) in `coordinator.py`; ebenso `ConfigEntryError` in `__init__.py` | Auf `translation_domain`/`translation_key` umstellen, Texte in `strings.json` unter `exceptions`. Beseitigt nebenbei das Sprachmischmasch. |
| 1.2 | `config-flow` (Bronze): `data_description` | Nur 6 `data_description` in einem Flow mit sehr vielen Feldern | Für jedes nicht selbsterklärende Feld eine Beschreibung ergänzen, vor allem Preis-, Reserve- und Spread-Parameter. Spart Support-Anfragen. |
| 1.3 | `parallel-updates` (Silber) | Keine Plattform setzt `PARALLEL_UPDATES` | In `button`, `number`, `select`, `switch` `PARALLEL_UPDATES = 1` (schreibende Modbus-Zugriffe serialisieren), in `sensor`/`binary_sensor` `0` (Coordinator). |
| 1.4 | `repair-issues` (Gold) | Handlungsbedürftige Zustände laufen über `persistent_notification` (`alerts.py`) | Für Fälle mit klarer Nutzeraktion (z. B. Geräteidentität geändert, fehlende Huawei-Bindungen, ungültige Quelle) `issue_registry` mit Repair-Flow nutzen. Laufende Betriebsmeldungen können Benachrichtigungen bleiben. |
| 1.5 | Statusdatei `quality_scale.yaml` | Fehlt | Datei in `custom_components/opti_akku/` anlegen, je Regel `done`/`todo`/`exempt` mit Begründung. So wird der Stand prüfbar statt nur in Issues beschrieben. Ausnahmen: `reauthentication-flow` (keine Anmeldung), `discovery` (Modbus TCP), `inject-websession` (kein HTTP). |

### P2: Gold- und Platinum-Punkte nach der ersten stabilen Version

| # | Regel | Befund | Vorschlag |
|---|---|---|---|
| 2.1 | `reconfiguration-flow` (Gold) | Verbindungswechsel liegt im Options-Flow unter „Wartung → Verbindung“ und schreibt Entry-Daten | Auf `async_step_reconfigure` umziehen. Das ist der HA-Standardort für Entry-Daten, Optionen bleiben Optionen. |
| 2.2 | `icon-translations` (Gold) | Kein `icons.json` | Icons für Entitäten und Zustände (z. B. Betriebsmodus) über `icons.json` statt im Code. |
| 2.3 | `strict-typing` (Platinum) | Mypy strict nur für 12 Module; `coordinator.py`, `config_flow.py`, `engine.py`, Plattformen fehlen | Schrittweise erweitern, Plattformen zuerst (klein), Coordinator zuletzt. |
| 2.4 | Config-Entry-Migration | `VERSION = 1`, kein `async_migrate_entry` | Erst nötig, wenn sich Entry-Daten ändern. Dann `MINOR_VERSION` nutzen und Migration testen, nicht still umdeuten. |
| 2.5 | Wartbarkeit | `coordinator.py` 2.770 Zeilen, `config_flow.py` 1.600 Zeilen | Coordinator nach Zuständigkeiten teilen (Schreibpfad, Quellen, Reporting); Options-Flow-Schritte in ein eigenes Modul. Kein Release-Blocker, senkt aber das Risiko jeder Änderung. |

### P3: CI und Entwicklungsablauf

Diese Punkte gehören in einen eigenen CI-PR.

| # | Lücke | Vorschlag |
|---|---|---|
| 3.1 | Kein geplanter Lauf | `schedule` (wöchentlich) für Hassfest und HACS-Validierung, damit Änderungen in HA oder HACS auffallen, bevor ein PR kommt. |
| 3.2 | Keine `concurrency` in `tests.yml` | `concurrency: { group: ${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: ${{ github.event_name == 'pull_request' }} }` spart Läufe bei schnellen Pushes. |
| 3.3 | Testjobs 25 bis 30 Minuten | `pytest-xdist` (`-n auto`) prüfen. Coverage mit xdist funktioniert über `pytest-cov`. Vorher messen, welche Tests dominieren (`--durations=25`). |
| 3.4 | `ruff format` wird nicht geprüft, Regelset schmal (E4/E7/E9/F/B) | Erst `ruff format` einmalig anwenden (eigener PR, reine Formatierung), dann `ruff format --check` in CI. Danach Regeln stufenweise ergänzen: `I` (Importe), `UP`, `SIM`, `RUF`, `ASYNC`. |
| 3.5 | Kein `pre-commit` | `.pre-commit-config.yaml` mit Ruff (lint und format) und `check-json`/`check-yaml`; optional [pre-commit.ci](https://pre-commit.ci). |

## Bereits erfüllt (nicht erneut umsetzen)

- **HACS:** `hacs.json` mit HA-Mindestversion, Repo-Beschreibung und Topics,
  Issues aktiv, Releases mit Prerelease-Kennzeichen, Brand-Icon im Komponentenordner,
  HACS-Validierung in CI grün.
- **Manifest:** `codeowners`, `documentation`, `issue_tracker`, `iot_class`,
  `version`, `config_flow`; Hassfest in CI grün.
- **Bronze:** Config-Flow mit Verbindungstest, `unique_id` gegen Doppelanlage,
  `runtime_data`, `has_entity_name`, Übersetzungen, Entity-Kategorien,
  `async_config_entry_first_refresh`, Tests für Config-Flow.
- **Silber:** Entladen der Entry, `available` an allen Plattformen, Codeowner,
  Coverage-Gate (Baseline in `.github/coverage-baseline.json`).
- **Gold:** Diagnoseexport mit Allowlist statt Redaction, Geräte, deaktivierte
  Standard-Entitäten, Geräteklassen, umfangreiche Doku (Betrieb, Migration,
  Fehlersuche, Lesetests).
- **Prozess:** Branch-Schutz mit Pflicht-Checks, Dependabot für Actions und
  Pip, gepinnte Action-SHAs, reproduzierbarer Release-Build, CHANGELOG,
  Issue- und PR-Vorlagen, Sicherheitsrichtlinie, Cross-Model-Review-Pflicht.

## Distribution

- **HACS-Standardliste:** Optional. Voraussetzungen erfüllt die Integration
  weitgehend. Eine Aufnahme bringt mehr Nutzer und damit mehr Support. Empfehlung:
  erst nach der ersten stabilen Version und mindestens einem Bugfix-Release
  einreichen.
- **Englische Kurzfassung:** README und Doku sind Deutsch, die UI ist
  zweisprachig. Für die HACS-Standardliste und internationale Nutzer einen
  kurzen englischen Abschnitt (Zweck, Unterstützungsumfang, Installation,
  Sicherheitshinweise) oben im README ergänzen.

## Vorgeschlagene Reihenfolge

1. P3 (CI-PR) und 1.3, 1.5 als kleine PRs, weil sie risikoarm sind.
2. 1.1 und 1.2 (Übersetzungen) in einem PR.
3. 1.4 Reparaturen, danach Feature-Freeze und `rc1` (0.2).
4. Abnahmen aus #85 während der RC-Phase abschließen, dann `.0`.
5. P2 und Distribution nach der ersten stabilen Version.
