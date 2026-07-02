# Legacy-Flachdateien

Diese Dateien sind die alten „Flachdateien" aus dem Repo-Root — überholt durch die neue
`packages/`-Struktur plus `automations/opti_strategie.yaml`.

Sie dienen ausschließlich als **Referenz** und werden nicht mehr aktiv gepflegt.

## Empfohlener Weg

Siehe [Schnell-Nachbau über Packages](../README.md#schnell-nachbau-über-packages-empfohlen)
im Haupt-README.

## Dateien in diesem Ordner

| Datei | Beschreibung |
|---|---|
| `configuration.yaml` | Modbus-Konfiguration zum WR (ersetzt durch `packages/sma_modbus.yaml`) |
| `opti-automatik.yaml` | Prognosebasierte Opti-Automatik (ersetzt durch `automations/opti_strategie.yaml`) |
| `templates.yaml` | Template-Sensoren (ersetzt durch `packages/sma_templates.yaml`) |
| `statistik.yaml` | Gleitende Mittelwert-Sensoren (ersetzt durch `packages/sma_statistik.yaml`) |
| `sma-se-akku-steuerung.yaml` | Manuelle Steuerautomatik (Hardware-Adapter jetzt im Repo `ha-modbus-akku-adapter`) |

## old_legacy/

Der Unterordner `../old_legacy/` enthält noch ältere, ungenutzte Stände (SMA Grid Guard Code
Ära) — nur für Archivzwecke, nutzt niemand mehr aktiv.

## Konzepte (Legacy-Namen, old/templates.yaml)

Diese Erklärungen gehören zu den Sensoren in `templates.yaml` (Legacy). Die aktuelle,
kanonische Entsprechung findet sich in
[docs/canonical-layer.md](../docs/canonical-layer.md) (Alt↔Neu-Mapping-Tabelle).

### Welcher Sensor ist `sensor.betriebsstatus_sma_stp_se_10_0`?

Das ist der Betriebsstatus-Sensor aus der Modbus-Konfiguration. In der mitgelieferten `configuration.yaml` heißt er `sensor.sma_stp_se_33003_betriebsstatus` (Adresse 33003). Ältere Versionen dieses Repos nutzten noch den anderen Namen – bitte in der Automation entsprechend anpassen.

---

### Woher kommt `sensor.akkusteuerung_dynamische_ladestaerke`?

Dieser Template-Sensor ist in `templates.yaml` definiert und berechnet die optimale Ladestärke anhand von **Akku-SoC** und **Temperatur** – abgestimmt auf BYD LiFePO4-Chemie:

| SoC | C-Rate | Begründung |
|---|---|---|
| < 30 % | 0.5C | Schnell laden bei kritisch niedrigem SoC |
| 30–60 % | 0.3C | Optimale Langlebigkeit |
| 60–85 % | 0.2C | Ausgewogen |
| 85–MaxSoC | 0.1C | Schonend bei hohem SoC |
| > MaxSoC | 0.05C | Minimal |
| > 45 °C oder < 0 °C | reduziert/0 | Temperaturschutz |

---

### Was ist `sensor.akku_target_soc_intelligent`?

Berechnet anhand der **verbleibenden Solcast-Prognose** und dem geschätzten **Hausverbrauch bis Sonnenuntergang**, wie weit der Akku *jetzt* geladen werden sollte. Je weniger PV-Produktion noch zu erwarten ist, desto höher der Ziel-SoC:

| Verh. Restproduktion / Akkukapazität | Ziel-SoC |
|---|---|
| > 3× | 50 % |
| 2–3× | 60 % |
| 1.5–2× | 70 % |
| 1–1.5× | 80 % |
| 0.5–1× | 90 % |
| < 0.5× | MaxSoC |

---

### Was ist der Unterschied zwischen Ladestärke, min/max Ladestärke?

| Helfer | Wann aktiv | Beschreibung |
|---|---|---|
| `akkusteuerung_ladestaerke_soll` | Modus "Schnell Laden" | Feste Ziel-Ladestärke für den manuellen Modus |
| _0.2C Laden_ (kein Helfer) | Modus "0.2C Laden" | Ladeleistung wird vom Hardware-Adapter automatisch aus der Batteriekapazität berechnet (0,2 × Kapazität) |
| `akkusteuerung_min_ladestaerke` | Immer (Dynamisch-Betrieb) | Untere Grenze, die der WR nie unterschreiten soll |
| `akkusteuerung_max_ladestaerke` | Immer (Dynamisch-Betrieb) | Obere Grenze – wird durch dynamische Ladestärke weiter begrenzt |
| `sensor.akkusteuerung_dynamische_ladestaerke` | Immer (Dynamisch-Betrieb) | Automatisch berechneter Sollwert (SoC + Temperatur) |

Empfehlung: Min auf `0`, Max auf z.B. `5000`, dann übernimmt die dynamische Berechnung die Feinsteuerung.

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
| `akkusteuerung_modus` | input_select | 9 Modi | Aktiver Steuermodus |
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
