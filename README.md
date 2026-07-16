# ha-opti-akkusteuerung

Prognosebasierte Akku-Ladesteuerung für Home Assistant - die Strategie ist hardware-agnostisch (Canonical-`opti_*`-Layer), als Referenz-Adapter dient der **SMA STP SE Hybrid-Wechselrichter** (direkt über Modbus, ohne Grid Guard Code).

> ⚠️ **Disclaimer:** Dieses Projekt wird nicht von SMA begleitet oder supportet. Nutzung auf eigene Gefahr. Kein persönlicher Support, aber die Community hilft gerne über [Issues](https://github.com/Optic00/ha-opti-akkusteuerung/issues).

> **Für wen?** HA-Nutzer mit dynamischem Stromtarif und PV-Speicher, die den Akku prognosebasiert statt stumpf auf 100 % steuern wollen.
> **Konkret getestet:** SMA STP SE Hybrid-WR + BYD-Akku, direkt über Modbus TCP.
> **Theoretisch adaptierbar:** andere Wechselrichter (Huawei, …) über den Canonical-Layer - erfordert aber eigenes Hardware-Mapping und ggf. Register-Recherche, ist also Eigenarbeit.

---

## Was macht das hier?

Prognosebasierte Akku-Ladesteuerung, **hardware-agnostisch** über einen separaten Modbus-Adapter, komplett als HA-Packages paketiert.

**Prognosebasierter Ziel-SoC** (Kernfeature, `sensor.opti_target_soc`)
Lädt den Akku morgens **nicht** stumpf auf 100 %, sondern nur so weit, dass die erwartete
Rest-PV des Tages ihn bis zum Abend von selbst voll macht - schont die Zellen und maximiert
den PV-Eigenverbrauch. Der Zielwert ergibt sich aus Solcast-Restprognose, Hausverbrauch und
Restzeit bis Sonnenuntergang, als Stufenkennlinie mit echter Hysterese (kein Flattern).
→ **[Herleitung in docs/strategie-logik.md](docs/strategie-logik.md#der-intelligente-ziel-soc--herzstück-der-akkuschonung)**

**Entlade-Peak-Allokation** (`sensor.opti_peak_reserve_soc`, Peak-Leiter L1-L4)
Reserviert einen Teil des SoC gezielt für die kommenden teuersten Stunden, statt ihn
undifferenziert an eine beliebige Stunde davor zu verlieren. Dazu kommen eine
Negativpreis-Laderegel und eine spread-basierte Peak-Vorladeregel, beide mit
selbstkorrigierender Ladefenster-Wahl.
→ **[Details in docs/strategie-logik.md](docs/strategie-logik.md#entlade-peak-allokation-reserve-für-die-teuersten-stunden)**

**Strategie** (`automations/opti_strategie.yaml`)
Entscheidet prognosebasiert, welcher Modus wann gilt: Lädt bei schlechter PV-Prognose aus
dem Netz (gestaffelt nach SoC und Preisniveau), nutzt PV-Überschuss tagsüber und schützt
MinSOC-Grenzen. Schreibt primär `input_select.akkusteuerung_modus` - keine direkte
Hardware-Ansteuerung.

**Hardware-Adapter** (separates Repo: [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter))
Liest den Modus aus `input_select.akkusteuerung_modus` und steuert den WR via Modbus TCP.
Läuft als eigenständiger Blueprint-Adapter - Strategie und Hardware-Ansteuerung sind
bewusst getrennt (Single-Writer-Regel: immer nur ein Adapter aktiv).

**Canonical-Layer** (`opti_mapping.example.yaml` → `packages/opti_mapping.yaml`)
Bildet hardware-spezifische Entitäten (SMA, Huawei oder andere WR) auf 13 kanonische
`sensor.opti_*`-Sensoren ab. Strategie und abgeleitete Sensoren konsumieren nur diese
kanonischen Namen - keine Seriennummern im Code. → **[docs/canonical-layer.md](docs/canonical-layer.md)**

### Architektur in einem Bild

```
Strategie  →  input_select.akkusteuerung_modus  →  [ ADAPTER-BLUEPRINT ]  →  Modbus-Register  →  WR
(setzt Modus)        (+ input_number.* in W)              übersetzt
```

Die Strategie (dieses Repo) entscheidet nur den **Modus** - der Hardware-Adapter (separates Repo) übersetzt ihn in Modbus. Das macht die Strategie unabhängig vom Speicherfabrikat.
→ Wer genau was liefert, in welcher Reihenfolge, plus Versions-Kompatibilität: **[docs/installation.md](docs/installation.md#komponenten-und-reihenfolge)**

---

## Voraussetzungen

- Home Assistant mit **SMA-Integration** (für SoC, PV-Leistung, etc.) - das ist der getestete SMA-Referenzweg; bei anderer Hardware stattdessen deren Integration + eigenes Canonical-Mapping
- **Solcast-Integration** für PV-Prognosen
- Ein dynamischer Stromtarif mit stündlicher `today`/`tomorrow`-Preisliste (z. B. Tibber, Nordpool, EPEX)
- **Home Assistant 2025.1 oder neuer** (getestet mit 2026.6; technische Untergrenze ist 2024.10, weil die abgeleiteten Sensoren trigger-basierte Template-Sensoren mit `variables:` nutzen)
- Aktuelle WR-Firmware - **kein Beta-Firmware und kein Grid Guard Code nötig**
- Modbus TCP am WR erreichbar (Standard-Port 502)

> 💡 **Wichtig:** Die prognosebasierte Akkusteuerung im SMA Home Manager / SunnyPortal muss deaktiviert sein, sonst überschreibt sie die Modbus-Werte regelmäßig wieder.

---

## Schnell-Start

Der Minimalpfad. Voller Ablauf mit beiden Einspiel-Varianten, allen Erststart-Werten und der Watchdog-Konfiguration: **[docs/installation.md](docs/installation.md)**.

1. **Packages aktivieren** in `configuration.yaml`:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages/
   ```
2. **Hardware-Mapping:** `opti_mapping.example.yaml` → `packages/opti_mapping.yaml` kopieren, alle `DEIN_*`-Platzhalter durch echte Entitäts-IDs ersetzen (→ [docs/canonical-layer.md](docs/canonical-layer.md)).
3. **Package-Dateien** aus [`packages/`](packages/) ins HA-`packages/`-Verzeichnis kopieren (Überblick unter [Dateien](#dateien); `sma_templates.yaml`/`opti_ki_analyse.yaml`/`byd_bmu.yaml` sind optional). In `sma_modbus.yaml` die **WR-IP** anpassen - oder die Datei weglassen, falls der Modbus-Hub schon aus dem Adapter-Repo kommt (siehe „Nur aus einer Quelle" unten).
4. **Home Assistant neu starten.**
5. ✅ **Verify-Gate - erst prüfen, dann scharf schalten:** In den Entwicklertools sicherstellen, dass `sensor.opti_target_soc`, `sensor.opti_charge_power_w` und `sensor.opti_price_level` plausible Werte zeigen und **nicht** `unavailable`/`unknown` sind. Stimmt etwas nicht → zuerst das Mapping korrigieren, nicht weitergehen.
6. **Adapter-Blueprint importieren** aus [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) und die Eingaben auf deine Entitäten mappen (Modbus-Hub, WR-Status, `input_select.akkusteuerung_modus`, `sensor.opti_charge_power_w`, dazu `battery_capacity_sensor` und `inverter_ok_states` - die Blueprint-Vorschlagswerte prüfen, nicht ungeprüft übernehmen).
7. **Strategie einspielen** aus `automations/opti_strategie.yaml` (zwei Wege - anhängen an `automations.yaml` oder als Package: → [docs/installation.md](docs/installation.md#schritt-für-schritt)).
8. **Erststart-Werte setzen (VOR dem Einschalten):** `input_number`-Helfer ohne `initial:` starten auf ihrem **Minimum** - bei `maxsoc` und den Max-Ladestärken ist das **0**, was jedes Laden/Entladen blockiert. Also `maxsoc` (~95 %), `minsoc` (~10 %) und die Max-Lade-/Entladestärken einmalig über die HA-Oberfläche setzen. Vollständige Startwert-Tabelle: **[docs/installation.md](docs/installation.md#schritt-für-schritt)**.
9. **Erst jetzt einschalten:** Master-Schalter `input_boolean.akku_opti_automatik` auf **an**.

> ⚠️ **Single-Writer-Regel:** Nur **eine** Automation darf den WR via Modbus schreiben - keine zweite Steuer-Automatik parallel aktiv lassen.
>
> ⚠️ **Nur aus einer Quelle:** Helfer und Modbus-Hub entweder aus dem Adapter-Repo **oder** aus diesem Repo - nie beides (Duplicate-Key-Fehler). Details: [docs/installation.md](docs/installation.md#komponenten-und-reihenfolge).

---

## Dateien

| Pfad | Beschreibung |
|---|---|
| `opti_mapping.example.yaml` | Vorlage für das Hardware-Mapping (→ nach `packages/opti_mapping.yaml` kopieren, Platzhalter ersetzen) |
| `packages/opti_mapping.yaml` | **Dein** Hardware-Mapping (gitignored - enthält echte Entitäts-IDs) |
| `packages/opti_derived.yaml` | Abgeleitete Entscheidungs-Sensoren (Score, Ziel-SoC, Preisniveau, …) |
| `packages/sma_modbus.yaml` | Modbus-TCP-Verbindung zum WR |
| `packages/sma_helpers.yaml` | Alle Helfer (input_select, input_number, input_boolean, counter, input_text/input_datetime für Adapter-Write-on-Change ab v1.2.0) |
| `packages/sma_templates.yaml` | Legacy-Template-Sensoren - teils durch `opti_derived.yaml` abgelöst, teils noch ohne Canonical-Äquivalent (Sollkurve/P-Regler, Abregelung) |
| `packages/sma_statistik.yaml` | Gleitende Mittelwert-Sensoren für Verbrauch & Batterielast |
| `packages/opti_ki_analyse.yaml` | **optional** - täglicher KI-Tagesreport (rein lesend) |
| `packages/byd_bmu.yaml` | **optional** - BYD-Zell-Monitoring via bydlogc→MQTT (→ [docs/byd-bmu-monitoring.md](docs/byd-bmu-monitoring.md)) |
| `packages/byd_modul2_fruehwarnung.yaml` | **optional** - Degradations-Frühwarnung fürs schwächste BYD-Modul, setzt `byd_bmu.yaml` voraus (→ [docs/byd-modul2-fruehwarnung.md](docs/byd-modul2-fruehwarnung.md)) |
| `packages/opti_ev_sperre.yaml` | **optional** - EV-Schnelllade-Entladesperre (Hausakku entlädt nicht ins Auto, wenn evcc im Modus now/minpv lädt); braucht HACS `evcc_intg` + Ladepunkt-Block im Mapping → [docs/strategie-logik.md](docs/strategie-logik.md) (Option 13) |
| `automations/opti_strategie.yaml` | Strategie-Automation (editierbar, kein Blueprint) |

Der frühere manuelle Weg mit Flachdateien liegt zur Referenz unter [`old/README.md`](old/README.md) - für Neuaufbauten nicht empfohlen. Vollständige Datei-Liste und Legacy-Namens-Mapping: **[docs/installation.md](docs/installation.md#legacy-setup-referenz)**.

---

## Strategie-Logik

Die Strategie-Automation entscheidet ausschließlich den **Modus** via `input_select.akkusteuerung_modus` - sie berührt keine Hardware direkt. Eine vollständige, laienverständliche Block-für-Block-Erklärung aller Entscheidungsoptionen, der Preisstufenlogik (`sensor.opti_price_level`), des MinSOC-Schutzes, der Wintermodus-Blöcke und der Bausteine (P10-Sicherheitsnetz, Decision-Trace, Balancing-Watchdog):
**[docs/strategie-logik.md](docs/strategie-logik.md)**

---

## Fehlerbehebung

**Ladestrom springt alle 4 Minuten zurück:**
Die prognosebasierte Akkusteuerung im SMA Home Manager / SunnyPortal überschreibt die Modbus-Werte. Im SunnyPortal unter den WR-Einstellungen deaktivieren.

**Ladeleistung fällt alle 6 Minuten kurz auf 0:**
Shadefix zieht periodisch den Stecker. In den WR-Einstellungen auf 30 Minuten setzen oder deaktivieren, falls Shadefix nicht benötigt wird.

**Automation bleibt mittendrin stecken:**
Unter *Einstellungen → Automationen → [Automation] → Traces* die Ausführung Schritt für Schritt nachvollziehen.

**Modbus-Register-Referenz:** Alle bekannten Registeradressen mit Wertebeschreibungen werden kanonisch im Adapter-Repo gepflegt: → [ha-modbus-akku-adapter/docs/modbus-register-referenz.md](https://github.com/Optic00/ha-modbus-akku-adapter/blob/main/docs/modbus-register-referenz.md) (inoffizielle Community-Sammlung, keine Gewähr).

---

## Danksagung

Dieses Projekt lebt von der Community. Besonderer Dank geht an:

- **[@Skybarks](https://github.com/Skybarks)** – für unzählige hilfreiche Antworten im Issues-Tracker, Recherche zu offiziellen SMA Modbus-Dokumenten und geduldige Hilfe beim Einrichten bei anderen Usern
- **[@mvdberge](https://github.com/mvdberge)** – für den Anstoß zur Modbus-Register-Dokumentation und das Angebot zur Mitarbeit
- **[@steel4me](https://github.com/steel4me)** – für das Aufspüren und Melden des Template-Fehlers (`| int` ohne Default)
- **[@WardinT](https://github.com/WardinT)** – für die genaue Code-Analyse, das Finden des doppelten Automation-Blocks und konstruktive Verbesserungsvorschläge
- **[@CarlosEllan](https://github.com/CarlosEllan)** – für Modbus-Registerforschung bei weiteren WR-Modellen
- **[@Michl09](https://github.com/Michl09)** – für das Testen des Dual-WR-Setups und Feedback
- **ajay123** im Photovoltaikforum – für die Entdeckung der neuen Modbus-Steueradressen (Sep 2025) durch direkten Kontakt mit dem SMA-Support, was die gesamte Steuerlogik stark vereinfacht hat ([Quell-Post](https://www.photovoltaikforum.com/thread/215473-begrenzen-der-lade-entladeleistung-byd-mit-stp-se/?postID=4033278#post4033278))

---

## Changelog

| Datum | Was |
|---|---|
| Jul 2026 | Entlade-Peak-Allokation: berechneter Reserve-SoC, Peak-Leiter, Negativpreis- und Vorladeregel (neuer Adapter-Modus "Akku Netzladen"), Backtest gegen echte Winterdaten, Test-Harness für die Jinja-Templates, Viertelstunden-Preisraster |
| Jun 2026 | Canonical-`opti_*`-Layer: Strategie hardware-agnostisch, prognosebasierter Ziel-SoC mit echter Schmitt-Hysterese, anbieter-agnostisches Preisniveau (Midrank-Perzentil), Vorschau-Sensor für Soll/Ist-Vergleich |
| Sep 2025 | Neue Modbus-Adressen für direkte Lade-/Entladeleistungssteuerung - Steuerlogik stark vereinfacht, dynamischer Ziel-SoC und Prognose-Bewertung |
| Jul 2024 | Modbus-Direktsteuerung ohne Grid Guard Code mit aktuellem Firmware-Stand möglich |

## Roadmap

**Strategie**
- [ ] Netzladen zu Off-Peak-Zeiten / Paragraf-14a-Fenster (pauschal günstigere Nachtstunden)
- [ ] Mindestentladepreis als echte Entlade-Bedingung nutzen (bisher nur informativ)
- [x] Akku regelmäßig automatisch auf 100 % balancen - erledigt als saison-übergreifender **Balancing-/Deep-Charge-Watchdog** (`sensor.opti_balancing_watchdog`). Details: [`docs/strategie-logik.md`](docs/strategie-logik.md#balancing-deep-charge-watchdog)
- [x] Hysterese-Band für die PV-Überschuss-Grenzen - erledigt als entprellte Binärsensoren `opti_ueberschuss_70_aktiv`/`opti_ueberschuss_ac_aktiv`
- [ ] `opti_peak_verbrauch_kw` statistisch aus der Verbrauchshistorie ableiten statt fix zu konfigurieren
- [ ] Wirkleistungsbegrenzung bei negativen Strompreisen über Modbus (Register 41255) - experimentell

**Weitere Geräte & Versionen**
- [ ] SBS-Unterstützung (suche Tester → [Issue öffnen](https://github.com/Optic00/ha-opti-akkusteuerung/issues))
- [ ] English version?

---

## Lizenz

[MIT](LICENSE) - frei nutzbar, anpassbar und weiterverteilbar, solange der Copyright- und Lizenzhinweis erhalten bleibt. Nutzung auf eigene Gefahr, ohne Gewähr (siehe Disclaimer oben).
