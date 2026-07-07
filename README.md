# ha-opti-akkusteuerung

Prognosebasierte Akku-Ladesteuerung für Home Assistant - die Strategie ist hardware-agnostisch (Canonical-`opti_*`-Layer), als Referenz-Adapter dient der **SMA STP SE Hybrid-Wechselrichter** (direkt über Modbus, ohne Grid Guard Code).

> ⚠️ **Disclaimer:** Dieses Projekt wird nicht von SMA begleitet oder supportet. Nutzung auf eigene Gefahr. Kein persönlicher Support, aber die Community hilft gerne über [Issues](https://github.com/Optic00/ha-opti-akkusteuerung/issues).

---

## Was macht das hier?

Prognosebasierte Akku-Ladesteuerung für Home Assistant — **hardware-agnostisch** über einen
separaten Modbus-Adapter, komplett als HA-Packages paketiert.

**Prognosebasierter Ziel-SoC** (Kernfeature, `sensor.opti_target_soc`)  
Lädt den Akku morgens **nicht** stumpf auf 100 %, sondern nur so weit, dass die erwartete
Rest-PV des Tages ihn bis zum Abend von selbst voll macht — schont die Zellen und maximiert
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
MinSOC-Grenzen. Schreibt primär `input_select.akkusteuerung_modus` — keine direkte
Hardware-Ansteuerung.

**Hardware-Adapter** (separates Repo: [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter))  
Liest den Modus aus `input_select.akkusteuerung_modus` und steuert den WR via Modbus TCP.
Läuft als eigenständiger Blueprint-Adapter — Strategie und Hardware-Ansteuerung sind
bewusst getrennt. Single-Writer-Regel: immer nur ein Adapter aktiv.

**Canonical-Layer** (`opti_mapping.example.yaml` → `packages/opti_mapping.yaml`)  
Bildet hardware-spezifische Entitäten (SMA, Huawei oder andere WR) auf 13 kanonische
`sensor.opti_*`-Sensoren ab. Strategie und abgeleitete Sensoren konsumieren nur diese
kanonischen Namen — keine Seriennummern im Code. → **[docs/canonical-layer.md](docs/canonical-layer.md)**

**Packages** (`packages/`) — per `!include_dir_named packages/` in `configuration.yaml`:  
Liefert alle Helfer, Template-Sensoren, abgeleitete Opti-Sensoren, Statistik-Sensoren und
die Modbus-Konfiguration gebündelt mit.

### Wer liefert was — und in welcher Reihenfolge?

| Kommt aus | Was | GUI oder YAML |
|---|---|---|
| Adapter-Repo | Modbus-Hub zum WR | YAML (`configuration.yaml`/Package) |
| Adapter-Repo (**oder** Opti-Repo, siehe unten) | Modus-Dropdown + 6 Leistungs-Helfer | GUI oder YAML (Package) |
| Adapter-Repo (**oder** Opti-Repo, siehe unten) | 2 Write-on-Change-Helfer (`input_text`/`input_datetime`) | GUI oder YAML (Package) |
| Adapter-Repo | Blueprint (übersetzt Modus → Modbus) | Blueprint-Import |
| Opti-Repo | `opti_mapping.yaml` (Hardware → kanonische Sensoren) | YAML, von dir ausgefüllt |
| Opti-Repo | `opti_derived.yaml` (Score, Ziel-SoC, Preisniveau) | YAML (Package) |
| Opti-Repo | Strategie-Automation (setzt den Modus) | YAML (editierbar, kein Blueprint) |

**Verbindliche Reihenfolge, wenn du beide Repos zusammen nutzt:**

1. Modbus-Verbindung anlegen (Adapter-Repo, Schritt 1). Helfer NICHT hier anlegen, wenn du Schritt 2 nutzt — siehe Hinweis unten.
2. Opti-Packages aktivieren + `opti_mapping.yaml` ausfüllen (Opti-Repo)
3. Home Assistant neu starten — `sensor.opti_*` prüfen
4. Adapter-Blueprint importieren, Inputs prüfen: `dynamic_charge_strength_sensor` auf
   `sensor.opti_charge_power_w` setzen, dazu `battery_capacity_sensor`,
   `inverter_status_sensor` und `inverter_ok_states` auf deine echten Entitäten bzw.
   Status-Codes (nicht ungeprüft die Blueprint-Vorschlagswerte übernehmen, falls sie
   abweichen)
5. Strategie-Automation (`automations/opti_strategie.yaml`) aktivieren

> ⚠️ **Helfer nur aus einer Quelle:** Bei kombinierter Nutzung liefert
> `ha-opti-akkusteuerung/packages/sma_helpers.yaml` bereits alle Helfer (Modus-Dropdown,
> 6 Leistungs-Helfer, 2 Write-on-Change-Helfer). Die Adapter-GUI-Anleitung bzw. das
> Adapter-Package dann NICHT zusätzlich verwenden — zwei Packages mit denselben
> Entity-IDs führen zu einem Duplicate-Key-Fehler im HA-Log. Nutzt du den Adapter
> **ohne** das Opti-Repo (eigene Strategie), gilt die Adapter-Anleitung normal.

> ⚠️ **Modbus-Hub nur aus einer Quelle:** Dieselbe Falle gilt für den Modbus-Hub selbst.
> Wer Schritt 1 bereits im Adapter-Repo erledigt hat (eigener `modbus:`-Block in der
> `configuration.yaml`), lässt `packages/sma_modbus.yaml` aus diesem Repo weg — zwei
> `modbus:`-Blöcke mit demselben Hub-Namen führen zum selben Duplicate-Key-Fehler im
> HA-Log.

### Versions-Kompatibilität

| Strategie-Feature | benötigter Adapter-Stand |
|---|---|
| Peak-Allokation / Modus „Akku Netzladen" | [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) >= v1.5.0 |
| Alle übrigen Modi (Automatisch, Dynamisch, Pause, nur Laden, nur Entladen, schnell Laden, schnell Entladen, 0.2C Laden) | [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) >= v1.2.0 (Write-on-Change-Helfer) |

```
Strategie  →  input_select.akkusteuerung_modus  →  [ ADAPTER-BLUEPRINT ]  →  Modbus-Register  →  WR
(setzt Modus)        (+ input_number.* in W)              übersetzt
```

---

## Voraussetzungen

- Home Assistant mit **SMA-Integration** (für SoC, PV-Leistung, etc.)
- **Solcast-Integration** für PV-Prognosen
- Ein dynamischer Stromtarif mit stündlicher `today`/`tomorrow`-Preisliste (z. B. Tibber, Nordpool, EPEX)
- **Home Assistant 2025.1 oder neuer** (getestet mit 2026.6; technische Untergrenze ist 2024.10, weil die abgeleiteten Sensoren trigger-basierte Template-Sensoren mit `variables:` nutzen)
- Aktuelle WR-Firmware – **kein Beta-Firmware und kein Grid Guard Code nötig**
- Modbus TCP am WR erreichbar (Standard-Port 502)

> 💡 **Wichtig:** Die prognosebasierte Akkusteuerung im SMA Home Manager / SunnyPortal muss deaktiviert sein, sonst überschreibt sie die Modbus-Werte regelmäßig wieder.

---

## Dateien

**Neue Struktur (empfohlen):**

| Pfad | Beschreibung |
|---|---|
| `opti_mapping.example.yaml` | Vorlage für das Hardware-Mapping (→ nach `packages/opti_mapping.yaml` kopieren, Platzhalter ersetzen) |
| `packages/opti_mapping.yaml` | **Dein** Hardware-Mapping (gitignored — enthält echte Entitäts-IDs) |
| `packages/opti_derived.yaml` | Abgeleitete Entscheidungs-Sensoren (Score, Ziel-SoC, Preisniveau, …) |
| `packages/sma_modbus.yaml` | Modbus-TCP-Verbindung zum WR |
| `packages/sma_helpers.yaml` | Alle Helfer (input_select, input_number, input_boolean, counter, input_text/input_datetime für Adapter-Write-on-Change ab v1.2.0) |
| `packages/sma_templates.yaml` | Legacy-Template-Sensoren — teils durch `opti_derived.yaml` abgelöst (Ziel-SoC, Ladestärke, Prognose-Score, Preisniveau, Laufzeit), teils noch ohne Canonical-Äquivalent (Sollkurve/P-Regler, Abregelung) |
| `packages/sma_statistik.yaml` | Gleitende Mittelwert-Sensoren für Verbrauch & Batterielast |
| `automations/opti_strategie.yaml` | Strategie-Automation (editierbar, kein Blueprint) |

**Legacy (zur Referenz, nicht mehr empfohlen):**
Die alten Flachdateien liegen mit Dateiübersicht unter [`old/`](old/README.md).
Noch ältere Stände aus der Grid-Guard-Code-Ära wurden entfernt und sind über die Git-Historie abrufbar.

---

## Strategie-Logik

Die Strategie-Automation entscheidet ausschließlich den **Modus** via
`input_select.akkusteuerung_modus` — sie berührt keine Hardware direkt. Was der Modus
am Wechselrichter auslöst, übernimmt der Hardware-Adapter (Single-Writer-Regel). Das macht
die Strategie unabhängig vom konkreten Speicherfabrikat.

Eine vollständige laienverständliche Block-für-Block-Erklärung aller Entscheidungsoptionen,
der Preisstufenlogik (`sensor.opti_price_level`, anbieter-agnostisches Perzentil-Enum),
des MinSOC-Schutzes, der Wintermodus-Blöcke und der Bausteine (P10-Sicherheitsnetz,
Decision-Trace) findet sich unter:
**[docs/strategie-logik.md](docs/strategie-logik.md)**

> **Adapter-Repo:** Die Modbus-/Hardware-Ansteuerung lebt in einem separaten Repository
> [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter).
> Die Strategie hier ist bewusst hardware-agnostisch gehalten.

---

## Schnell-Nachbau über Packages (empfohlen)

> 🆕 Neue, paketbasierte Installation – liefert **alle Helfer, Templates, Statistik-
> Sensoren und die Modbus-Konfiguration mit** (kein manuelles Anlegen nötig). Der
> Canonical-Layer (`opti_mapping.yaml`) macht die Strategie hardware-agnostisch —
> funktioniert mit SMA, Huawei und anderen Wechselrichtern. Die hardwareseitige
> Modbus-Ansteuerung läuft als separater Blueprint-Adapter.

**1. Packages aktivieren** (einmalig) in deiner `configuration.yaml`:
```yaml
homeassistant:
  packages: !include_dir_named packages/
```

**2. Hardware-Mapping anlegen:** `opti_mapping.example.yaml` nach `packages/opti_mapping.yaml`
kopieren und alle `DEIN_*`-Platzhalter durch echte Entitäts-IDs ersetzen.
→ Ausführliche Anleitung: **[docs/canonical-layer.md](docs/canonical-layer.md)**

**3. Package-Dateien** aus dem Ordner [`packages/`](packages/) in dein HA-`packages/`-
Verzeichnis kopieren:

| Datei | Inhalt |
|---|---|
| `opti_derived.yaml` | Abgeleitete Entscheidungs-Sensoren (Score, Ziel-SoC, Preisniveau, …) |
| `sma_modbus.yaml` | ⚠️ **nur falls nicht bereits über Adapter-Repo Schritt 1 angelegt** — Modbus-TCP-Verbindung zum WR (nur **IP** anpassen) |
| `sma_helpers.yaml` | alle `input_select`/`input_number`/`input_boolean`/`counter` (Modus, Sollwerte, SoC-Grenzen …) |
| `sma_templates.yaml` | **optional** — nur für Sollkurve-/Abregelungs-Anzeige (Legacy), enthält Platzhalter-Entity-IDs: ersetzen oder ganz weglassen |
| `sma_statistik.yaml` | gleitende Mittelwerte (Verbrauch, Batterielast) |

**4. Home Assistant neu starten** → Helfer, Templates, Statistik & Modbus sind da.

**5. Hardware-Adapter importieren:** Blueprint aus
[`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) per Raw-URL
importieren (*Einstellungen → Automatisierungen & Szenen → Blueprints → importieren*) und
beim Anlegen der Automation die Eingaben auf deine Entitäten mappen
(Modbus-Hub, WR-Status-Sensor, Modus-Select `input_select.akkusteuerung_modus`,
dyn. Ladestärke `sensor.opti_charge_power_w`).

**6. Strategie einspielen:** die Opti-Automatik (steuert *welcher Modus wann*) liegt in
`automations/opti_strategie.yaml` — als Top-Level-Liste im Format von `automations.yaml`,
**nicht** als fertiges Package. Sie ist bewusst **editierbar** (kein Blueprint), damit du
sie an deine Anlage/Strategie anpassen kannst. Zwei Wege, sie einzuspielen:

- **(a) An `automations.yaml` anhängen** (einfachster Weg): den kompletten Inhalt von
  `automations/opti_strategie.yaml` ans Ende deiner `automations.yaml` kopieren. HA
  erkennt sie danach als normale Automation, editierbar über die UI.
- **(b) Als eigenes Package speichern:** die Datei nach `packages/opti_strategie.yaml`
  kopieren und mit dem Schlüssel `automation:` wrappen — dann lassen sich zusätzlich
  Optionen wie `initial_state` ergänzen:

  ```yaml
  automation:
    - id: "opti_canonical_strategie"
      alias: "Akku Opti Strategie"
      initial_state: true
      # ... restlicher Inhalt aus automations/opti_strategie.yaml unverändert ...
  ```

  Auf demselben Weg gehört die zweite Automation `automations/opti_balancing_counter.yaml`
  eingespielt (zählt `counter.tage_seit_akku100` täglich um 23:59 hoch und speist den
  Balancing-Watchdog). Ersetzt eine ggf. bereits live vorhandene, gleichnamige
  Increment-Automation.

**7. Einschalten:** die Strategie-Automation bleibt wirkungslos, solange ihr Master-Schalter
aus ist — und frisch angelegte `input_boolean`-Helfer starten **aus** (kein `initial:`, siehe
Warnung unten). Über die HA-Oberfläche auf **an** stellen:

| Helfer | Wirkung |
|---|---|
| `input_boolean.akku_opti_automatik` | Master-Schalter — ohne „an" tut die gesamte Strategie-Automation nichts |
| `input_boolean.opti_prognose_netzladen` | Gate für die prognosebasierten „Akku nur Laden"-Blöcke (Reserve halten bei schlechter PV-Prognose) |
| `input_boolean.opti_pv_ueberschuss_ladung` | Gate für die PV-/AC-Überschussblöcke (Akku Dynamisch bei Einspeise-Überschuss) |

**8. Erststart-Werte setzen:** `input_number`-Helfer ohne `initial:` starten beim
allerersten Anlegen auf ihrem **Minimum** — bei `maxsoc` und den beiden
Max-Ladestärke-Helfern ist das **0**, was jedes Laden/Entladen blockiert. Nach dem ersten
Anlegen (Schritt 4, HA-Neustart) einmalig über die HA-Oberfläche setzen — danach übersteht
der Wert jeden weiteren Neustart:

| Helfer | Erststart-Wert (Minimum) | Empfohlener Startwert |
|---|---|---|
| `input_number.minsoc` | 0 % | 10 % |
| `input_number.maxsoc` | 0 % | 95 % |
| `input_number.akkusteuerung_max_ladestaerke` | 0 W | 3000 W |
| `input_number.akkusteuerung_max_entladestaerke` | 0 W | 5000 W |
| `input_number.akkusteuerung_ladestaerke_soll` | 100 W | 2000 W |
| `input_number.akkusteuerung_entladestaerke_soll` | 100 W | 2000 W |
| `input_number.akkusteuerung_min_ladestaerke` | 0 W | 0 W |
| `input_number.akkusteuerung_min_entladestaerke` | 0 W | 0 W |
| `input_number.opti_peak_verbrauch_kw` | 0.1 kW | 0.8 kW |
| `input_number.opti_einspeiseverguetung_ct` | 0 ct/kWh | dein eigener EEG-Satz |
| `input_number.opti_netzlade_spread_ct` | 0 ct/kWh | 10 ct/kWh |
| `input_number.opti_peak_min_aufschlag_ct` | 0 ct/kWh | 5 ct/kWh |
| `input_number.opti_halte_spread_ct` | 0 ct/kWh | 5 ct/kWh |
| `input_number.opti_balancing_intervall_tage` | 0 Tage (= Watchdog aus) | 14 Tage |
| `input_number.opti_balancing_karenz_tage` | 0 Tage | 3 Tage |
| `input_number.opti_balancing_max_ct` | 0 ct/kWh (= kein bezahltes Netzladen) | 25 ct/kWh |

Die vier `opti_balancing_*`-Helfer steuern den **Balancing-/Deep-Charge-Watchdog**
(`sensor.opti_balancing_watchdog`): `intervall_tage` = Tage ohne Voll-/Done-Ladung bis
der Watchdog fällig wird (0 = aus), `karenz_tage` = zusätzliche Wartezeit vor dem
bezahlten Netz-Fallback, `max_ct` = absoluter Brutto-Preisdeckel fürs bezahlte
Balancing-Netzladen (0 = fail-safe aus). Der vierte Helfer
`input_number.opti_balancing_done_soc` hat als einziger ein `initial:` (98.5 %) und muss
nicht von Hand gesetzt werden — er definiert die „Akku ~voll"-Schwelle für Counter-Reset
und tägliches Increment.

**9. Feinjustieren:** SoC-Grenzen, Lade-/Entladegrenzen, Prognose-Schwellen über die
HA-Oberfläche weiter an die eigene Anlage anpassen (alle als Helfer vorhanden).

> ⚠️ **Single-Writer-Regel:** Nur **eine** Automation darf den WR via Modbus schreiben.
> Wenn du den Adapter-Blueprint nutzt, keine zweite Steuer-Automatik gleichzeitig aktiv lassen.

---

## Legacy-Setup (Referenz)

Der frühere manuelle Weg mit Flachdateien (Modbus-Config, Sensoren, Helfer-Tabelle, Dashboard-Karte) ist umgezogen nach [`old/README.md`](old/README.md).
Empfohlen bleibt die Package-Struktur oben.

---

> 💡 Nutzt du noch die alten Sensor-Namen (`akkusteuerung_dynamische_ladestaerke`,
> `akku_target_soc_intelligent`)? Erklärung und Alt↔Neu-Mapping:
> **[old/README.md#konzepte-legacy-namen](old/README.md#konzepte-legacy-namen-oldtemplatesyaml)**

---

## Modbus-Register Referenz

Alle bekannten Registeradressen mit Wertebeschreibungen werden kanonisch im Adapter-Repo gepflegt: → **[ha-modbus-akku-adapter/docs/modbus-register-referenz.md](https://github.com/Optic00/ha-modbus-akku-adapter/blob/main/docs/modbus-register-referenz.md)**

> ⚠️ Inoffizielle Community-Sammlung, keine Gewähr, Nutzung auf eigene Gefahr.

---

## Fehlerbehebung

**Ladestrom springt alle 4 Minuten zurück:**  
Die prognosebasierte Akkusteuerung im SMA Home Manager / SunnyPortal überschreibt die Modbus-Werte. Im SunnyPortal unter den WR-Einstellungen deaktivieren.

**Ladeleistung fällt alle 6 Minuten kurz auf 0:**  
Shadefix zieht periodisch den Stecker. In den WR-Einstellungen auf 30 Minuten setzen oder deaktivieren, falls Shadefix nicht benötigt wird.

**Automation bleibt mittendrin stecken:**  
Unter *Einstellungen → Automationen → [Automation] → Traces* die Ausführung Schritt für Schritt nachvollziehen.

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

---

## Roadmap

**Strategie**
- [ ] Netzladen zu Off-Peak-Zeiten / Paragraf-14a-Fenster (pauschal günstigere Nachtstunden)
- [ ] Mindestentladepreis als echte Entlade-Bedingung nutzen (bisher nur informativ)
- [x] Akku regelmäßig automatisch auf 100 % balancen — erledigt als saison-übergreifender **Balancing-/Deep-Charge-Watchdog** (`sensor.opti_balancing_watchdog`): erzwingt einen Voll-Zyklus fürs BMS, wenn der Akku länger als `opti_balancing_intervall_tage` (Default 14) nicht mehr ~voll war. Staffelt PV-Vollladung (tagsüber) → Gratis-/Negativ-Netz → bezahltes Netz erst nach `opti_balancing_karenz_tage` und nur unter dem Deckel `opti_balancing_max_ct`. Rein abgeleitet aus `counter.tage_seit_akku100` → restart-durabel. Details: [`docs/strategie-logik.md`](docs/strategie-logik.md#balancing-deep-charge-watchdog)
- [x] Hysterese-Band für die PV-Überschuss-Grenzen — erledigt als entprellte Binärsensoren `opti_ueberschuss_70_aktiv`/`opti_ueberschuss_ac_aktiv` (akkuunabhängiges Signal, 30 s beidseitig, Hysterese)
- [ ] `opti_peak_verbrauch_kw` statistisch aus der Verbrauchshistorie ableiten statt fix zu konfigurieren
- [ ] Wirkleistungsbegrenzung bei negativen Strompreisen über Modbus (Register 41255) - experimentell, kein aktiver Support

**Weitere Geräte & Versionen**
- [ ] SBS-Unterstützung (suche Tester -> [Issue öffnen](https://github.com/Optic00/ha-opti-akkusteuerung/issues))
- [ ] English version?
