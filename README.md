# ha-opti-akkusteuerung

Prognosebasierte & manuelle Akku-Ladesteuerung für den **SMA STP SE Hybrid-Wechselrichter** in Home Assistant – direkt über Modbus, ohne Grid Guard Code.

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
4. Adapter-Blueprint importieren, Inputs auf `sensor.opti_charge_power_w` /
   `sensor.opti_target_soc` setzen (nicht ungeprüft die Blueprint-Vorschlagswerte
   übernehmen, falls sie abweichen)
5. Strategie-Automation (`automations/opti_strategie.yaml`) aktivieren

> ⚠️ **Helfer nur aus einer Quelle:** Bei kombinierter Nutzung liefert
> `ha-opti-akkusteuerung/packages/sma_helpers.yaml` bereits alle Helfer (Modus-Dropdown,
> 6 Leistungs-Helfer, 2 Write-on-Change-Helfer). Die Adapter-GUI-Anleitung bzw. das
> Adapter-Package dann NICHT zusätzlich verwenden — zwei Packages mit denselben
> Entity-IDs führen zu einem Duplicate-Key-Fehler im HA-Log. Nutzt du den Adapter
> **ohne** das Opti-Repo (eigene Strategie), gilt die Adapter-Anleitung normal.

```
Strategie  →  input_select.akkusteuerung_modus  →  [ ADAPTER-BLUEPRINT ]  →  Modbus-Register  →  WR
(setzt Modus)        (+ input_number.* in W)              übersetzt
```

> **Legacy-Flachdateien** (`old/`): Die alten Einzeldateien im Repo-Root wurden nach `old/`
> verschoben und werden nicht mehr gepflegt. Der empfohlene Weg ist die Package-Struktur.

---

## Voraussetzungen

- Home Assistant mit **SMA-Integration** (für SoC, PV-Leistung, etc.)
- **Solcast-Integration** für PV-Prognosen
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

| Pfad | Beschreibung |
|---|---|
| `old/configuration.yaml` | Alte Modbus-Konfiguration (Flachdatei) |
| `old/opti-automatik.yaml` | Alte Opti-Automatik |
| `old/templates.yaml` | Alte Template-Sensoren |
| `old/statistik.yaml` | Alte Statistik-Sensoren |
| `old/sma-se-akku-steuerung.yaml` | Alte manuelle Steuerautomatik |
| `old_legacy/` | Noch ältere Stände (Grid Guard Code Ära) — Archiv |

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
| `sma_modbus.yaml` | Modbus-TCP-Verbindung zum WR (nur **IP** anpassen) |
| `sma_helpers.yaml` | alle `input_select`/`input_number`/`input_boolean`/`counter` (Modus, Sollwerte, SoC-Grenzen …) |
| `sma_templates.yaml` | Legacy-Template-Sensoren (nur noch teilweise gebraucht — siehe Hinweis oben in der Dateitabelle) |
| `sma_statistik.yaml` | gleitende Mittelwerte (Verbrauch, Batterielast) |

**4. Home Assistant neu starten** → Helfer, Templates, Statistik & Modbus sind da.

**5. Hardware-Adapter importieren:** Blueprint aus
[`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) per Raw-URL
importieren (*Einstellungen → Automatisierungen & Szenen → Blueprints → importieren*) und
beim Anlegen der Automation die Eingaben auf deine Entitäten mappen
(Modbus-Hub, WR-Status-Sensor, Modus-Select `input_select.akkusteuerung_modus`,
dyn. Ladestärke `sensor.opti_charge_power_w`).

**6. Strategie einspielen:** die Opti-Automatik (steuert *welcher Modus wann*) – siehe
`automations/opti_strategie.yaml`. Sie ist bewusst **editierbar** (kein Blueprint), damit
du sie an deine Anlage/Strategie anpassen kannst.

**7. Feinjustieren:** SoC-Grenzen, Lade-/Entladegrenzen, Prognose-Schwellen über die
HA-Oberfläche (alle als Helfer vorhanden).

> ⚠️ **Single-Writer-Regel:** Nur **eine** Automation darf den WR via Modbus schreiben.
> Wenn du den Adapter-Blueprint nutzt, keine zweite Steuer-Automatik gleichzeitig aktiv lassen.

---

## Einrichtung (manuell – Flachdateien)

> Alternativer, manueller Weg mit den Legacy-Einzeldateien (jetzt unter `old/`). Für den
> Nachbau empfehlen wir die Package-Variante oben; dieser Abschnitt bleibt nur als Referenz.

### 1. Modbus-Verbindung (`configuration.yaml`)

Den Inhalt der mitgelieferten `configuration.yaml` in deine eigene `configuration.yaml` eintragen, nur die IP-Adresse anpassen:

```yaml
modbus:
  - name: sma-sr_wr
    type: tcp
    host: 192.168.x.x   # ← IP des Wechselrichters anpassen
    port: 502
    ...
```

> ⚠️ Den Temperatur-Sensor (`SMA-STP-SE_Temperatur`, Adresse 30953) unbedingt mit einbinden – er ist bereits in der mitgelieferten `configuration.yaml` enthalten. Ohne diesen Sensor startet die Modbus-Integration nicht zuverlässig.

---

### 2. Eigene Sensoren anlegen

Diese Sensoren müssen auf deine Entity-IDs angepasst werden (Seriennummer im Sensornamen ersetzen).

**PV-Überschuss für Akkuladung** (berücksichtigt Wallbox, falls vorhanden):

```yaml
- unique_id: maximaler_ueberschuss_akkuladung
  device_class: power
  state_class: measurement
  name: Maximaler Ueberschuss fuer Akkuladung Watt
  unit_of_measurement: W
  state: >
    {{ (states('sensor.pv_generation_komplett_watt') | float)
       - (states('sensor.home_energy_usage_watt') | float)
       - (states('sensor.sn_3015XXXXX_battery_power_charge_total') | float)
       + (states('sensor.sn_3015XXXXX_metering_power_absorbed') | float)
       + (states('sensor.DEINE_WALLBOX_powernow') | float) }}
```

**PV-Überschuss für 70%-Kappungserkennung:**

```yaml
- unique_id: akkusteuerung_ueberschuss_pv
  device_class: power
  state_class: measurement
  name: Ueberschuss PV Watt
  unit_of_measurement: W
  state: >
    {{ (states('sensor.pv_generation_komplett_watt') | float(0))
       - (states('sensor.home_energy_usage_watt') | float)
       - (states('sensor.sn_3017XXXXXX_metering_power_absorbed') | float) }}
```

**Hausverbrauch:**

```yaml
- unique_id: home_energy_usage_w
  device_class: power
  state_class: measurement
  name: Home Energy Usage Watt
  unit_of_measurement: W
  state: >
    {{ (states('sensor.sn_3017XXXXXX_metering_power_absorbed') | float)
       + (states('sensor.sn_3017XXXXXX_grid_power') | float)
       - (states('sensor.sn_3017XXXXXX_metering_power_supplied') | float) }}
```

**Wirkungsgrad & Zyklen (optional):**

```yaml
- unique_id: byd_akku_wirkungsgrad_lade_entlade
  name: BYD Akku Wirkungsgrad Ladung und Entladung
  unit_of_measurement: "%"
  state: >
    {{ ((states('sensor.sn_3017XXXXXX_battery_discharge_total') | float)
        / (states('sensor.sn_3017XXXXXX_battery_charge_total') | float) * 100)
       | round(2) }}

- unique_id: byd_akku_zyklen
  name: BYD Akku Zyklen
  unit_of_measurement: Zyklen
  state: >
    {{ (((states('sensor.sn_3017XXXXXX_battery_discharge_total') | float)
         + (states('sensor.sn_3017XXXXXX_battery_charge_total') | float)) / 100
        * (states('sensor.sn_3017XXXXXX_battery_capacity_total') | float)
        / (2 * 10.2)) | round(1) }}
```

**Statistik-Sensor für Laufzeitberechnung** (`statistik.yaml`):

```yaml
- platform: statistics
  name: "House Battery Load 30 mins"
  entity_id: sensor.sn_3015XXXXX_battery_power_discharge_total
  state_characteristic: mean
  max_age:
    minutes: 30
```

---

### 3. Input-Helfer anlegen

Entweder manuell über die HA-Oberfläche oder per YAML. Alle Helfer auf einen Blick:

| Helfer | Typ | Bereich | Beschreibung |
|---|---|---|---|
| `akkusteuerung_modus` | input_select | 8 Modi | Aktiver Steuermodus |
| `akku_opti_automatik` | input_boolean | – | Opti-Automatik Ein/Aus |
| `akkusteuerung_ladestaerke_soll` | input_number | 100–10000 W | Ladestärke (manuell) |
| `akkusteuerung_entladestaerke_soll` | input_number | 100–10000 W | Entladestärke (manuell) |
| `akkusteuerung_min_ladestaerke` | input_number | 0–2000 W | Minimale Ladestärke |
| `akkusteuerung_max_ladestaerke` | input_number | 0–10000 W | Maximale Ladestärke |
| `akkusteuerung_min_entladestaerke` | input_number | 0–2000 W | Minimale Entladestärke |
| `akkusteuerung_max_entladestaerke` | input_number | 0–10000 W | Maximale Entladestärke |
| `akkusteuerung_wr_ac_ueberschuss_grenze` | input_number | 0–15000 W | WR AC-Nennleistung (z.B. 9900 bei 10kW-Anlage) |
| `akkusteuerung_wr_70proz_ueberschuss_grenze` | input_number | 0–15000 W | 70%-Kappungsgrenze (z.B. 6800 bei 10kW) |
| `minsoc` | input_number | 0–100 % | Minimaler SoC |
| `maxsoc` | input_number | 0–100 % | Maximaler SoC |

> 💡 **Nur manuelle Steuerung gewünscht?** Dann reichen `akkusteuerung_modus` + die Ladehelfer + die Sensor-Definitionen.

---

### 4. Dashboard-Karte

```yaml
type: vertical-stack
cards:
  - type: custom:mushroom-title-card
    title: Akkusteuerung SMA STP-SE
  - type: custom:mushroom-chips-card
    chips:
      - type: conditional
        conditions:
          - condition: numeric_state
            entity: sensor.sn_301XXXXXXX_battery_power_discharge_total
            above: 0.001
        chip:
          type: template
          entity: sensor.sn_301XXXXXXX_battery_power_discharge_total
          content: "{{ (states(entity) | float / 1000) | round(2) }} kW"
          icon: mdi:battery-minus
          icon_color: red
      - type: conditional
        conditions:
          - condition: numeric_state
            entity: sensor.sn_301XXXXXXX_battery_power_charge_total
            above: 0.001
        chip:
          type: entity
          entity: sensor.sn_301XXXXXXX_battery_power_charge_total
          icon: mdi:battery-positive
          icon_color: green
      - type: entity
        entity: sensor.sn_301XXXXXXX_battery_soc_total
        icon_color: blue
      - type: template
        entity: sensor.byd_12_8_akku_wirkungsgrad_ladung_und_entladung
        content: "{{ states(entity) | round(1) }}% η"
        icon: mdi:vector-difference
        icon_color: orange
      - type: template
        entity: sensor.byd_12_8_akku_zyklen
        content: "{{ states(entity) }}"
        icon: mdi:counter
        icon_color: yellow
      - type: entity
        entity: sensor.sn_301XXXXXXX_battery_temp_a
      - type: entity
        entity: sensor.sma_stp_se_temperatur
  - type: entities
    entities:
      - entity: sensor.akkusteuerung_dynamische_ladestaerke
      - entity: sensor.pv_forecast_bewertung_heute
      - entity: sensor.pv_forecast_bewertung_morgen
      - entity: sensor.house_battery_runtime_raw
  - type: custom:mushroom-select-card
    entity: input_select.akkusteuerung_modus
    name: Akkusteuerung
    primary_info: name
    secondary_info: last-changed
  - type: tile
    entity: input_boolean.akku_opti_automatik
  - type: tile
    entity: input_boolean.akku_nach_preis_laden
  - type: horizontal-stack
    cards:
      - type: tile
        entity: sensor.sn_301XXXXXXX_battery_discharge_total
        name: Entladen Watt
      - type: tile
        entity: sensor.sn_301XXXXXXX_battery_charge_total
        name: Laden Watt
  - type: entities
    entities:
      - entity: input_number.akkusteuerung_ladestaerke_soll
      - entity: input_number.akkusteuerung_entladestaerke_soll
        name: Entladestärke
      - entity: input_number.akkusteuerung_wr_ac_ueberschuss_grenze
        name: WR AC-Grenze
      - entity: input_number.akkusteuerung_wr_70proz_ueberschuss_grenze
        name: 70% Grenze
      - entity: input_number.akkusteuerung_max_ladestaerke
        name: Akku max Ladestärke
      - entity: input_number.akkusteuerung_min_ladestaerke
        name: Akku min Ladestärke
      - entity: input_number.akkusteuerung_max_entladestaerke
        name: Akku max Entladestärke
      - entity: input_number.akkusteuerung_min_entladestaerke
        name: Akku min Entladestärke
      - entity: input_number.minsoc
      - entity: input_number.maxsoc
  - type: heading
    heading: Debugging
    heading_style: title
  - type: entities
    entities:
      - entity: sensor.akku_target_soc_intelligent
      - entity: sensor.akku_net_verfugbare_energie
      - entity: sensor.verbleibende_sonnenstunden
```

---

> 💡 Nutzt du noch die alten Sensor-Namen (`akkusteuerung_dynamische_ladestaerke`,
> `akku_target_soc_intelligent`)? Erklärung und Alt↔Neu-Mapping:
> **[old/README.md#konzepte-legacy-namen](old/README.md#konzepte-legacy-namen-oldtemplatesyaml)**

### Modbus-Register Referenz

Alle bekannten Registeradressen mit Wertebeschreibungen: → **[docs/modbus-register-referenz.md](docs/modbus-register-referenz.md)**

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
| Sep 2025 | Neue Modbus-Adressen für direkte Lade-/Entladeleistungssteuerung – Steuerlogik stark vereinfacht, dynamischer Ziel-SoC und Prognose-Bewertung |
| Jul 2024 | Modbus-Direktsteuerung ohne Grid Guard Code mit aktuellem Firmware-Stand möglich |

---

## Roadmap

**Code & Aufräumen**
- [x] Doppelten deaktivierten `Akku nur Entladen`-Block in `sma-se-akku-steuerung.yaml` entfernen
- [x] `sensor.ueberschuss_pv_watt` in Templates aufnehmen
- [x] Akkukapazitäts-Fallback auf `sensor.sma_stp_se_40187_batterie_nennkapazitaet` – funktioniert jetzt automatisch für alle Akkugrößen
- [x] Trigger auf `sensor.akkusteuerung_dynamische_ladestaerke` in Steuerungs-Automation ergänzt
- [x] Forecast-abhängige C-Raten: schlechter Tag aggressiv laden, guter Tag schonend (3 Profile)
- [x] Hysterese-Rounding (0,25-Schritte) für Ziel-SoC, verhindert Flattern an Stufengrenzen

**Features**
- [ ] Akku im Winter mindestens 1× pro Woche automatisch auf 100% laden
- [ ] Ladegeschwindigkeit ab 95–98% auf 500 W begrenzen
- [ ] Tibber-Preisladen finalisieren
- [ ] Wirkleistungsbegrenzung bei negativen Strompreisen über Modbus (einstellbare Preisschwelle, Register 41255) – experimentell, kein aktiver Support

**Weitere Geräte & Versionen**
- [ ] SBS-Unterstützung (suche Tester → [Issue öffnen](https://github.com/Optic00/ha-opti-akkusteuerung/issues))
- [ ] Blueprint-Version für einfachere Installation (in Arbeit)
- [ ] English version?
