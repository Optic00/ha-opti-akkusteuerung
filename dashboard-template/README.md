# Dashboard-Template: Opti-Akkusteuerung Übersicht

Portables Lovelace-Dashboard als Steuerzentrale für die opti-Strategie-Schicht dieses Repos.
Die Datei `uebersicht.json` enthält eine abstrahierte Kopie der Live-Ansicht "Übersicht" (View-Pfad `uebersicht`) aus dem Dashboard `opti-akkusteuerung`.
Sie ist so aufgebaut, dass eine andere HA-Instanz sie mit minimalem Aufwand nachbauen kann.

Weil die Zielinstanz die opti-Schicht dieses Repos bereits fährt (Shadow-Betrieb), sind fast alle Karten direkt auf die kanonischen `sensor.opti_*`-Sensoren und die opti-Helfer gemappt.
Diese Karten brauchen beim Nachbau keinen Platzhalter und funktionieren sofort.
Nur echte hardwarespezifische Sensoren (Zell-/BMS-Werte) sind als `__ROLLE__`-Platzhalter markiert.

## Quelle und Auswahl

- Basis: Dashboard `opti-akkusteuerung`, View "Übersicht" (`type: sections`, `max_columns: 3`), rein lesend per `ha_config_get_dashboard` extrahiert.
- Alle Karten dieser View wurden 1:1 übernommen (Struktur, Layout, Templates), abgesehen von den unten dokumentierten Abstraktionen und Auslassungen.
- Eine Fremd-Karte wurde neu ergänzt (siehe "Ergänzte Karten"): ein Live-Energiefluss-Diagramm, weil die Zielinstanz `power-flow-card-plus` bereits installiert hat und ein Flussdiagramm der Kern einer Steuerzentrale ist.

## Nachbau (Kurzfassung)

1. Fehlende HACS-Karten installieren (Tabelle unten).
2. `uebersicht.json` als neues Dashboard oder neue View anlegen (nicht die vorhandene Übersicht der Zielinstanz überschreiben).
3. Die Platzhalter `__ROLLE__` durch echte Entitäten der Zielinstanz ersetzen oder die betroffenen Karten/Abschnitte entfernen.
4. Beim Energiefluss-Diagramm die Vorzeichen-Hinweise beachten (Batterie-Invertierung, Netzbezug-Sensor).

## Rollen-Tabelle: Platzhalter

Nur diese Rollen sind im Template als `__ROLLE__` offen.
Alles andere ist bereits fest auf kanonische opti-Entitäten verdrahtet (siehe nächste Tabelle).

| Platzhalter | Bedeutung | Einheit / Vorzeichenkonvention | Bens Original-Entität (Referenz) | Hinweis für die Zielinstanz |
|---|---|---|---|---|
| `__ZELL_SPREIZUNG_MV__` | Zellspannungs-Spreizung (max - min) | mV, positiv | `sensor.byd_zellspreizung` | Zell-Level-Wert eines externen BMS-Auslesers. Auf Huawei LUNA2000 in der Regel nicht vorhanden. Abschnitt "Akku-Gesundheit" sonst entfernen. |
| `__ZELL_SPANNUNG_MIN_V__` | niedrigste Zellspannung | V | `sensor.byd_zellspannung_min` | wie oben |
| `__ZELL_SPANNUNG_MAX_V__` | höchste Zellspannung | V | `sensor.byd_zellspannung_max` | wie oben |
| `__ZELL_SPANNUNG_AVG_V__` | mittlere Zellspannung | V | `sensor.byd_zellspannung_avg` | wie oben |
| `__BATT_SOH__` | State of Health der Batterie | % | `sensor.byd_soh` | Falls die Zielbatterie einen SoH-Sensor liefert, hier eintragen, sonst Kachel entfernen. |
| `__BMS_BALANCING_AKTIV__` | BMS meldet aktives Zell-Balancing | binary (on/off) | `binary_sensor.byd_balancing_aktiv` | Zell-Level, meist nur mit externem BMS-Ausleser. Sonst Kachel entfernen. |
| `__BMS_SOC__` | SoC laut BMS (zum Vergleich mit WR-SoC) | % | `sensor.byd_soc` | Optionaler Zweit-SoC. Sonst Kachel entfernen. |

## Rollen-Tabelle: fest verdrahtete kanonische opti-Entitäten

Diese Entitäten stehen unverändert im Template, weil die Zielinstanz die opti-Schicht bereits fährt.
Die Vorzeichen-/Einheiten-Konventionen stammen aus `docs/canonical-layer.md` dieses Repos.

| Entität im Template | Bedeutung | Einheit / Vorzeichenkonvention |
|---|---|---|
| `sensor.opti_soc` | Akku-SoC | %, 0 bis 100 |
| `sensor.opti_battery_power_w` | Batterieleistung | W, **positiv = laden**, negativ = entladen |
| `sensor.opti_pv_power_w` | PV-AC-Leistung | W, immer >= 0 |
| `sensor.opti_house_consumption_w` | Hausverbrauch | W, >= 0 |
| `sensor.opti_grid_export_w` | Einspeisung ins Netz | W, **positiv = Einspeisung**, immer >= 0 |
| `sensor.opti_grid_import_w` | Netzbezug (Import) | W, **positiv = Bezug**, immer >= 0 (optionaler Anzeige-Sensor, siehe `docs/canonical-layer.md`) |
| `sensor.opti_battery_temp` | Akku-Temperatur | °C |
| `sensor.opti_battery_capacity_kwh` | nutzbare Kapazität | kWh |
| `sensor.opti_target_soc` | intelligenter Ziel-SoC | % |
| `sensor.opti_price_current_ct_kwh` | aktueller Strompreis | ct/kWh |
| `sensor.opti_price_level` | Preisniveau-Enum | VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE / VERY_EXPENSIVE |
| `sensor.opti_mindestentladepreis_ct_kwh` | Mindestentladepreis | ct/kWh |
| `sensor.opti_forecast_score` / `_score_tomorrow` | PV-Fit-Score heute / morgen | 0 bis 10 |
| `sensor.opti_forecast_effective_remaining_kwh` | effektive Rest-Prognose | kWh, Attribute `p10_kwh`, `median_kwh`, `alpha` |
| `sensor.opti_forecast_tomorrow_kwh` | Prognose morgen | kWh, Attribut `estimate10` (P10) |
| `sensor.opti_peak_reserve_soc` | Reserve-SoC für Preisspitzen | % |
| `binary_sensor.opti_peak_reserve_aktiv` | Peak-Reserve-Gate | on/off |
| `binary_sensor.opti_winter_charging_allowed` | saisonales Lade-Gate | on/off (fail-open) |
| `sensor.opti_strategie_vorschau` | Vorschau-Modus der Strategie | Text, Attribut `grund` |
| `sensor.opti_balancing_watchdog` | Balancing-/Deep-Charge-Watchdog | `aus` / `pv` / `netz`, Attribut `grund` |
| `sensor.opti_ki_analyse` | KI-Tagesreport-Status | `ok` / `auffaellig` / …, Attribut `zusammenfassung` |
| `counter.tage_seit_akku100` | Tage seit letzter 100%-Ladung | Ganzzahl |
| `input_select.akkusteuerung_modus` | manueller/aktueller Modus | Auswahl |
| `input_boolean.akku_opti_automatik` | Master-Schalter Automatik | on/off |
| `input_boolean.opti_prognose_netzladen` | Gate Prognose-Netzladen | on/off |
| `input_boolean.opti_pv_ueberschuss_ladung` | Gate PV-Überschussladen | on/off |
| `input_boolean.hausakku_aus_netz_laden` | manueller Netzlade-Booster | on/off |
| `input_boolean.opti_balancing_netzladen` | Gate Balancing-Netzladen | on/off (Default aus) |
| `input_boolean.opti_ki_tagesreport_immer` | KI-Report immer senden | on/off |
| `input_number.minsoc` / `input_number.maxsoc` | SoC-Grenzen | % |
| `input_number.opti_forecast_optimismus` | Forecast-Optimismus α | %, 0 bis 100 |
| `input_number.opti_balancing_intervall_tage` / `_karenz_tage` / `_max_ct` / `_done_soc` | Balancing-Parameter | Tage / Tage / ct/kWh / % |
| `input_datetime.opti_ki_last_success` | letzter KI-Erfolg | Zeitstempel |
| `script.ki_opti_warum_erklaerung` | KI-Warum-Erklärung auslösen | Skript |

## HACS- und Theme-Abhängigkeiten

Auf der Zielinstanz laut Vorgabe bereits vorhanden: `mushroom`, `power-flow-card-plus`, `trash-card`.
`trash-card` wird von diesem Template nicht verwendet.

| Karte im Template | HACS-Typname | Repo | Version (Bens Stand) | Status Zielinstanz |
|---|---|---|---|---|
| Mushroom-Template-/Select-Card | `custom:mushroom-template-card`, `custom:mushroom-select-card` | `piitaya/lovelace-mushroom` | v5.1.1 | vorhanden |
| Power Flow Card Plus | `custom:power-flow-card-plus` | `flixlix/power-flow-card-plus` | v0.3.7 | vorhanden |
| card-mod (CSS in `card_mod:`) | (Ressource, kein Kartentyp) | `thomasloven/lovelace-card-mod` | v4.2.1 | **nachinstallieren** |
| ApexCharts Card | `custom:apexcharts-card` | `RomRider/apexcharts-card` | v2.2.3 | **nachinstallieren** |

Die frühere Bar Card (`custom-cards/bar-card`) ist unmaintained und aus dem HACS-Default-Index entfernt.
Der "Tage seit 100%"-Balken ist deshalb eine native `tile`-Karte mit `bar-gauge`-Feature (min 0 / max 14); dabei entfallen die Severity-Farbschwellen der alten Karte.

Ohne `card-mod` rendern die Mushroom-Karten trotzdem, nur der Zeilenumbruch im `secondary`-Text greift nicht.
Der `card_mod`-Block kann dann alternativ entfernt werden.

## Views und Abschnitte

Das Template hat eine einzige View "Übersicht" mit folgenden Abschnitten (von oben nach unten):

- **⚡ Energiefluss (Live-Diagramm)**: ergänzte `power-flow-card-plus`, siehe "Ergänzte Karten".
- **🔋 Status jetzt**: aktueller Modus, Strategie-Vorschau mit Begründung, SoC-Gauge, Batterieleistung, Button für die KI-Warum-Erklärung.
- **⚡ Energiefluss (Kacheln)**: die ursprünglichen Kachel-Werte (PV, Haus, Einspeisung, Batterie, Temp, Kapazität). Bewusst zusätzlich zum Diagramm behalten, weil die Kacheln exakte Zahlen zeigen.
- **🧠 Strategie-Intelligenz**: Ziel-SoC, Preis, Preisniveau, Mindestentladepreis, Prognose-Scores, Peak-Reserve, Winter-Gate.
- **🔮 Prognose-Sicherheit**: P10/Median-Bänder heute und morgen, α-Regler.
- **🩺 Balancing-Watchdog**: Watchdog-Status, Tage-seit-100%-Balken, Balancing-Parameter.
- **🤖 KI-Tagesanalyse**: Report-Status, Sende-Schalter, letzter Erfolg.
- **🔬 Akku-Gesundheit (BMS, optional)**: nur Platzhalter, siehe unten. Abschnitt bei fehlenden Zell-Sensoren komplett löschen.
- **🎛️ Steuerung & Regler**: Master-Automatik, Modus-Auswahl, Netzlade-/Überschuss-Schalter, MinSOC/MaxSOC.
- **📈 Verlauf (24 h)**: ApexCharts für Preis/SoC und Leistung (kanonisch), plus ein optionaler Zellspannungs-Chart (Platzhalter).

## Ergänzte Karten

- **Energiefluss-Diagramm** (`custom:power-flow-card-plus`), Abschnitt "⚡ Energiefluss (Live-Diagramm)": neu empfohlen, nicht aus einem bestehenden Dashboard 1:1 kopiert. Die Karte ist auf die kanonischen opti-Sensoren gemappt.
  - PV: `sensor.opti_pv_power_w`, Haus: `sensor.opti_house_consumption_w`, Netzbezug: `sensor.opti_grid_import_w`, Einspeisung: `sensor.opti_grid_export_w`, Batterie: `sensor.opti_battery_power_w` mit SoC `sensor.opti_soc`.
  - **Netzbezug**: `sensor.opti_grid_import_w` ist der optionale, symmetrisch zu `opti_grid_export_w` ergänzte Canonical-Sensor (siehe `docs/canonical-layer.md`, Sensor 6b). Fehlt er auf der Zielinstanz, im Mapping die Bezugsquelle nachtragen oder den grid-consumption-Eintrag entfernen.
  - **Batterie-Vorzeichen**: opti nutzt "positiv = laden". `power-flow-card-plus` erwartet standardmäßig "positiv = entladen (zum Haus)". Deshalb steht `invert_state: true` in der Batterie-Konfiguration. Beim Nachbau prüfen, ob die Flussrichtung stimmt, und `invert_state` sonst umstellen.

## Bewusst ausgelassen

- **Personenbezogene Karten**: In dieser Übersicht-View gab es keine (keine Personen-Tracker, Kalender, Kameras, Anwesenheit). Es musste nichts entfernt werden.
- **Zelltemperatur über 5 Module** (`custom:mushroom-template-card` mit `sensor.byd_modul_1..5_temp_min/max`): ausgelassen, stark BYD-spezifisch (fünf Einzelmodule eines externen BMS-Auslesers).
- **Per-Modul-Balkenkarte** "Module: min. Zellspannung" (`sensor.byd_modul_1..5_zellspannung_min`, in Bens Live-Config inzwischen fünf native `tile`-Karten mit `bar-gauge` statt der früheren `custom:bar-card`): ausgelassen, gleiche BYD-Modul-Spezifik.
- Beide Karten lassen sich auf einer Instanz mit vergleichbaren Modul-Sensoren aus Bens Live-Config wieder ergänzen. Für die Huawei-Zielinstanz sind sie nicht sinnvoll portierbar.

## Privacy-Hinweise

- Keine internen IP-Adressen, Tokens, Secrets oder lokalen Pfade im Template.
- Die einzigen konkreten Fremd-Entitäten sind Bens `byd_*`-Sensornamen, ausschließlich als dokumentierte Original-Referenz in der Rollen-Tabelle. Im Template selbst stehen dafür nur Platzhalter.
- Screenshots wurden nicht beigelegt: ein Rendern hätte Service-Calls oder das Screenshot-Beta am Live-System erfordert, und die Aufgabe war strikt read-only.
