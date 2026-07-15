# BYD Modul-2 Frühwarnung - Implementierungsplan

> **Für agentische Worker:** Dies ist ein Home-Assistant-YAML-Feature, kein pytest-Projekt. Statt Unit-Tests gilt pro Schritt: (a) jinja2-Parse-Check der Templates (echter jinja2, rekursiv - siehe Repo-Memory "HA-YAML Jinja-Parse-Check"), (b) `ha_eval_template` des Ausdrucks gegen Live-States mit erwartetem Wert, (c) Reload + `ha_get_state` der neuen Entität. Schritte nutzen `- [ ]`.

**Goal:** Zwei belastbare Frühwarn-Größen für das schwächste BYD-Modul (Modul 2) dauerhaft loggen: relative Zell-Absackung (mV) und Nettoenergie bis Knie (kWh), gehärtet gegen Last, SoC-Drift, MQTT-Aussetzer und Zähler-Artefakte.

**Architecture:** Erweiterung des bestehenden Packages `packages/byd_bmu.yaml`. Helfer (YAML) + zwei utility_meter + Template-/Trigger-Sensoren + vier Automationen bilden einen Zyklus-Zustandsautomaten (idle→armed→latched, jederzeit→invalid). Rein beobachtend, keine Steuerwirkung.

**Tech Stack:** Home Assistant Packages, `template:` (state- und trigger-basiert), `utility_meter`, `input_*`-Helfer, Automationen. Deploy in-place live + Repo (PR gegen `feat/byd-bmu-monitoring`).

## Global Constraints

- Spec: `docs/byd-modul2-fruehwarnung-design.md` (verbindlich).
- Vorzeichen `sensor.byd_leistung`: **positiv = Entladen, negativ = Laden** (verifiziert 2026-07-15).
- Lastband: `sensor.byd_leistung` in **+500…+1500 W**.
- Referenzspannung Default **3,20 V** (`input_number`, live tunable, greift erst am nächsten Anker).
- Kein `float(0)`-Fallback für Energie/Spannung - stattdessen `has_value`-Gate.
- Kein Alarm/Schwellwert in dieser Stufe (erst nach 4-8 gültigen Zyklen, Folge-PR).
- MQTT-Rohsensoren bleiben in `mqtt/mqtt.yaml` (top-level `mqtt: !include`-Kollision beachten); neue Entitäten in `packages/byd_bmu.yaml`.
- Öffentliches Repo: Privacy-Scan aller getrackten Dateien vor Push.
- Entity-IDs exakt wie unten (spätere Tasks referenzieren sie).

## Datei-Struktur

- Modify: `packages/byd_bmu.yaml` - alle neuen Entitäten (Helfer, utility_meter, template, automation) ans Ende der jeweiligen Top-Level-Blöcke.
- Modify: `docs/byd-bmu-monitoring.md` - Abschnitt "Modul-2 Frühwarnung" (Betrieb/Interpretation).
- Create (nach erstem realen Zyklus): `docs/livetest-byd-modul2-fruehwarnung-2026-07.md` - Livetest-Protokoll.

Deploy-Ziel live: `/config/packages/byd_bmu.yaml` (in-place), Reload via `ha_reload_core` (bzw. YAML-Reload der betroffenen Domains).

---

### Task 1: Helfer (Zustands-Entitäten)

**Files:**
- Modify: `packages/byd_bmu.yaml` (neue Top-Level-Blöcke `input_number:`, `input_boolean:`, `input_select:`, `input_text:`, `input_datetime:`)

**Interfaces:**
- Produces: `input_number.byd_knie_referenzspannung`, `input_number.byd_knie_ref_frozen`, `input_boolean.byd_knie_armed`, `input_boolean.byd_knie_ueberschwelle_gesehen`, `input_boolean.byd_top_erreicht`, `input_select.byd_knie_zyklus_status` (Optionen idle/armed/latched/invalid), `input_text.byd_knie_cycle_id`, `input_text.byd_knie_invalid_grund`, `input_datetime.byd_voll_anker_zeit`.

- [ ] **Step 1: YAML einfügen**

```yaml
input_number:
  byd_knie_referenzspannung:
    name: "BYD Knie-Referenzspannung"
    min: 3.05
    max: 3.35
    step: 0.005
    initial: 3.20          # nur beim allerersten Start; danach restore
    unit_of_measurement: "V"
    mode: box
    icon: mdi:sine-wave
  byd_knie_ref_frozen:
    name: "BYD Knie-Referenz (eingefroren)"
    min: 3.05
    max: 3.35
    step: 0.001
    initial: 3.20
    unit_of_measurement: "V"
    mode: box
    icon: mdi:snowflake

input_boolean:
  byd_knie_armed:
    name: "BYD Knie scharf"
    icon: mdi:target
  byd_knie_ueberschwelle_gesehen:
    name: "BYD Knie Ueberschwelle gesehen"
    icon: mdi:arrow-up-bold
  byd_top_erreicht:
    name: "BYD Ladeschluss erreicht"
    icon: mdi:arrow-collapse-up

input_select:
  byd_knie_zyklus_status:
    name: "BYD Knie Zyklus-Status"
    options: ["idle", "armed", "latched", "invalid"]
    initial: idle
    icon: mdi:state-machine

input_text:
  byd_knie_cycle_id:
    name: "BYD Knie Cycle-ID"
    max: 40
  byd_knie_invalid_grund:
    name: "BYD Knie Invalid-Grund"
    max: 120

input_datetime:
  byd_voll_anker_zeit:
    name: "BYD Voll-Anker Zeit"
    has_date: true
    has_time: true
```

- [ ] **Step 2: Reload + Verifikation**

Reload (live): Helfer-Domains bzw. `ha_reload_core`. Dann:
```
ha_get_state(["input_number.byd_knie_referenzspannung","input_select.byd_knie_zyklus_status"])
```
Erwartet: `byd_knie_referenzspannung = 3.2`, `byd_knie_zyklus_status = idle`. Keine Config-Fehler im Log.

- [ ] **Step 3: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): Helfer fuer Modul-2 Knie-Zyklus-Statemachine"
```

---

### Task 2: Frische- und Entladeband-Sensoren

**Files:**
- Modify: `packages/byd_bmu.yaml` (unter `template:` einen `- binary_sensor:`-Block ergänzen)

**Interfaces:**
- Consumes: MQTT-Rohsensoren (`sensor.byd_soc`, `sensor.byd_leistung`, `sensor.byd_modul_1..5_zellspannung_min`, plus utility_meter aus Task 3 - bis dahin liefert `has_value` dafür `false`, unkritisch).
- Produces: `binary_sensor.byd_daten_frisch`, `binary_sensor.byd_entladeband`.

- [ ] **Step 1: eval_template als Vorab-Check (erwartet plausibles Ergebnis)**

`ha_eval_template`:
```
{% set p = states('sensor.byd_leistung')|float(0) %}{{ 500 <= p <= 1500 }}
```
Erwartet: `True`/`False` je nach aktueller Last (kein Fehler).

- [ ] **Step 2: YAML einfügen**

```yaml
  - binary_sensor:
      - unique_id: byd_daten_frisch
        name: "BYD Daten frisch"
        device_class: connectivity
        icon: mdi:database-check
        state: >
          {{ has_value('sensor.byd_soc') and has_value('sensor.byd_leistung')
             and has_value('sensor.byd_modul_1_zellspannung_min')
             and has_value('sensor.byd_modul_2_zellspannung_min')
             and has_value('sensor.byd_modul_3_zellspannung_min')
             and has_value('sensor.byd_modul_4_zellspannung_min')
             and has_value('sensor.byd_modul_5_zellspannung_min')
             and has_value('sensor.byd_geladen_seit_voll')
             and has_value('sensor.byd_entladen_seit_voll') }}
      - unique_id: byd_entladeband
        name: "BYD Entladeband"
        icon: mdi:speedometer-slow
        state: >
          {% set p = states('sensor.byd_leistung')|float(0) %}
          {{ has_value('sensor.byd_leistung') and 500 <= p <= 1500 }}
```

Hinweis: `byd_daten_frisch` prüft auch die utility_meter aus Task 3; vor Task 3 ist der Sensor `off` (ok).
Frische = `has_value` (expire_after 300 s der Quellen bündelt die Staleness). Bewusste Vereinfachung ggü. Codex' 120-s-Ideal: eine 1-min-Time-Pattern-Automation nur für die Altersschärfe wäre Overkill.

- [ ] **Step 3: Reload + Verifikation**

```
ha_get_state(["binary_sensor.byd_entladeband","binary_sensor.byd_daten_frisch"])
```
Erwartet: `byd_entladeband` on/off passend zur Last; `byd_daten_frisch` = off (utility_meter fehlen noch).

- [ ] **Step 4: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): Frische- und Entladeband-Binary-Sensoren"
```

---

### Task 3: Utility Meter + Nettoenergie-Sensor

**Files:**
- Modify: `packages/byd_bmu.yaml` (Top-Level `utility_meter:`; unter `template: - sensor:` den Netto-Sensor)

**Interfaces:**
- Consumes: `sensor.byd_geladen_gesamt`, `sensor.byd_entladen_gesamt`.
- Produces: `sensor.byd_geladen_seit_voll`, `sensor.byd_entladen_seit_voll`, `sensor.byd_netto_energie_seit_voll`.

- [ ] **Step 1: YAML einfügen**

```yaml
utility_meter:
  byd_geladen_seit_voll:
    source: sensor.byd_geladen_gesamt
    name: "BYD geladen seit Voll"
    periodically_resetting: false      # Quelle ist total_increasing, kein Zyklus-Reset
  byd_entladen_seit_voll:
    source: sensor.byd_entladen_gesamt
    name: "BYD entladen seit Voll"
    periodically_resetting: false
```

```yaml
      - unique_id: byd_netto_energie_seit_voll
        name: "BYD Nettoenergie seit Voll"
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: measurement          # NICHT total_increasing (darf bei PV-Ladung sinken)
        icon: mdi:battery-minus-outline
        availability: >
          {{ has_value('sensor.byd_entladen_seit_voll') and has_value('sensor.byd_geladen_seit_voll') }}
        state: >
          {{ (states('sensor.byd_entladen_seit_voll')|float
              - states('sensor.byd_geladen_seit_voll')|float) | round(3) }}
```

- [ ] **Step 2: Reload + Verifikation**

```
ha_get_state(["sensor.byd_geladen_seit_voll","sensor.byd_entladen_seit_voll","sensor.byd_netto_energie_seit_voll","binary_sensor.byd_daten_frisch"])
```
Erwartet: beide utility_meter numerisch (starten bei 0 bzw. akkumulieren ab jetzt); `byd_netto_energie_seit_voll` = entladen − geladen; `byd_daten_frisch` jetzt on (bei frischen Quellen).

- [ ] **Step 3: Reset-Service prüfen**

```
ha_call_service(utility_meter.reset, target: [sensor.byd_geladen_seit_voll, sensor.byd_entladen_seit_voll])
```
Erwartet: beide → 0. (Dieser Service wird später vom Anker genutzt.)

- [ ] **Step 4: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): utility_meter Paar seit Voll + Nettoenergie-Sensor"
```

---

### Task 4: Sensor A - Relative Zell-Absackung

**Files:**
- Modify: `packages/byd_bmu.yaml` (unter `template: - sensor:`)

**Interfaces:**
- Consumes: `sensor.byd_modul_1..5_zellspannung_min`, `binary_sensor.byd_daten_frisch`.
- Produces: `sensor.byd_modul2_absackung` (mV).

- [ ] **Step 1: eval_template Vorab-Check (Peer-Median-Formel)**

`ha_eval_template`:
```
{% set peers = [states('sensor.byd_modul_1_zellspannung_min')|float,
                states('sensor.byd_modul_3_zellspannung_min')|float,
                states('sensor.byd_modul_4_zellspannung_min')|float,
                states('sensor.byd_modul_5_zellspannung_min')|float] | sort %}
{% set peer_median = (peers[1] + peers[2]) / 2 %}
{% set m2 = states('sensor.byd_modul_2_zellspannung_min')|float %}
{{ ((peer_median - m2) * 1000) | round(1) }}
```
Erwartet: kleiner mV-Wert (im Plateau ~1-3, positiv wenn Modul 2 tiefer).

- [ ] **Step 2: YAML einfügen**

```yaml
      - unique_id: byd_modul2_absackung
        name: "BYD Modul-2 Absackung"
        unit_of_measurement: "mV"
        state_class: measurement
        icon: mdi:arrow-down-bold-box
        availability: "{{ is_state('binary_sensor.byd_daten_frisch','on') }}"
        state: >
          {% set peers = [states('sensor.byd_modul_1_zellspannung_min')|float,
                          states('sensor.byd_modul_3_zellspannung_min')|float,
                          states('sensor.byd_modul_4_zellspannung_min')|float,
                          states('sensor.byd_modul_5_zellspannung_min')|float] | sort %}
          {% set peer_median = (peers[1] + peers[2]) / 2 %}
          {% set m2 = states('sensor.byd_modul_2_zellspannung_min')|float %}
          {{ ((peer_median - m2) * 1000) | round(1) }}
        attributes:
          soc: "{{ states('sensor.byd_soc') }}"
          leistung_w: "{{ states('sensor.byd_leistung') }}"
          modul2_volt: "{{ states('sensor.byd_modul_2_zellspannung_min') }}"
          peer_median_volt: >
            {% set peers = [states('sensor.byd_modul_1_zellspannung_min')|float,
                            states('sensor.byd_modul_3_zellspannung_min')|float,
                            states('sensor.byd_modul_4_zellspannung_min')|float,
                            states('sensor.byd_modul_5_zellspannung_min')|float] | sort %}
            {{ ((peers[1] + peers[2]) / 2) | round(3) }}
          schwaechstes_modul: >
            {% set ns = namespace(min_v=99, idx='?') %}
            {% for i in [1,2,3,4,5] %}
              {% set v = states('sensor.byd_modul_' ~ i ~ '_zellspannung_min')|float(99) %}
              {% if v < ns.min_v %}{% set ns.min_v = v %}{% set ns.idx = i|string %}{% endif %}
            {% endfor %}
            {{ ns.idx }}
```

- [ ] **Step 3: Reload + Verifikation**

```
ha_get_state("sensor.byd_modul2_absackung")
```
Erwartet: numerischer mV-Wert = eval aus Step 1; Attribut `schwaechstes_modul` plausibel (meist "2" am Knie, im Plateau evtl. wechselnd).

- [ ] **Step 4: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): Sensor A relative Modul-2 Absackung (Peer-Median)"
```

---

### Task 5: Latch-Sensor (Nettoenergie bis Knie)

**Files:**
- Modify: `packages/byd_bmu.yaml` (neuer trigger-basierter `template:`-Block)

**Interfaces:**
- Consumes: `input_select.byd_knie_zyklus_status` (Transition nach `latched`), `sensor.byd_netto_energie_seit_voll`, `sensor.byd_geladen_seit_voll`, `sensor.byd_entladen_seit_voll`, `sensor.byd_modul2_absackung`.
- Produces: `sensor.byd_modul2_netto_bis_knie` (kWh, RestoreEntity).

- [ ] **Step 1: YAML einfügen**

```yaml
  - triggers:
      - trigger: state
        entity_id: input_select.byd_knie_zyklus_status
        to: "latched"
    sensor:
      - unique_id: byd_modul2_netto_bis_knie
        name: "BYD Modul-2 Nettoenergie bis Knie"
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: measurement
        icon: mdi:battery-alert-variant-outline
        state: "{{ states('sensor.byd_netto_energie_seit_voll') }}"
        attributes:
          netto_kwh: "{{ states('sensor.byd_netto_energie_seit_voll') }}"
          geladen_inkrement_kwh: "{{ states('sensor.byd_geladen_seit_voll') }}"
          entladen_inkrement_kwh: "{{ states('sensor.byd_entladen_seit_voll') }}"
          a_absackung_mv: "{{ states('sensor.byd_modul2_absackung') }}"
          soc: "{{ states('sensor.byd_soc') }}"
          leistung_w: "{{ states('sensor.byd_leistung') }}"
          modul2_volt: "{{ states('sensor.byd_modul_2_zellspannung_min') }}"
          modul_temp_c: "{{ states('sensor.byd_modul_2_temp_max') }}"
          ref_verwendet_v: "{{ states('input_number.byd_knie_ref_frozen') }}"
          cycle_id: "{{ states('input_text.byd_knie_cycle_id') }}"
          sauberer_zyklus: "{{ states('sensor.byd_geladen_seit_voll')|float(0) < 0.5 }}"
          gemessen: "{{ now().isoformat() }}"
```

Hinweis: Last ist durch das Entladeband (Task 6/7) auf +500…+1500 W beschränkt, deshalb genügt `leistung_w` als Momentanwert (kein separater Fenster-Mittelwert nötig).

- [ ] **Step 2: Reload + Verifikation (manueller Latch-Test)**

```
ha_call_service(input_select.select_option, target: input_select.byd_knie_zyklus_status, data:{option: "latched"})
ha_get_state("sensor.byd_modul2_netto_bis_knie")
```
Erwartet: Sensor übernimmt aktuellen `byd_netto_energie_seit_voll`, Attribute gefüllt. Danach Status zurück auf `idle` setzen (Aufräumen des Tests).

- [ ] **Step 3: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): Latch-Sensor Nettoenergie bis Knie"
```

---

### Task 6: Voll-Anker-Automation

**Files:**
- Modify: `packages/byd_bmu.yaml` (unter `automation:`)

**Interfaces:**
- Consumes: `sensor.byd_zellspannung_max`, `sensor.byd_leistung`, `sensor.byd_soc`, `binary_sensor.byd_daten_frisch`, `sensor.byd_netto_energie_seit_voll`, `input_boolean.byd_top_erreicht`, `input_number.byd_knie_referenzspannung`.
- Produces: setzt utility_meter-Reset, `input_boolean.byd_knie_armed`=on, `input_number.byd_knie_ref_frozen`, `input_select.byd_knie_zyklus_status`=armed, `input_text.byd_knie_cycle_id`, `input_datetime.byd_voll_anker_zeit`, löscht `byd_top_erreicht`/`byd_knie_ueberschwelle_gesehen`.

- [ ] **Step 1: YAML einfügen (zwei Automationen: Top-Merker + Anker)**

```yaml
  - id: byd_top_erreicht_merker
    alias: "BYD | Ladeschluss-Merker setzen"
    description: "Merkt sich, dass im laufenden Ladevorgang die Ladeschluss-Zellspannung erreicht wurde (Voll-Anker-Evidenz)."
    mode: single
    triggers:
      - trigger: numeric_state
        entity_id: sensor.byd_zellspannung_max
        above: 3.55
        for: "00:02:00"
    conditions: []
    actions:
      - action: input_boolean.turn_on
        target: { entity_id: input_boolean.byd_top_erreicht }

  - id: byd_voll_anker
    alias: "BYD | Voll-Anker (Ladeabschluss)"
    description: >-
      Ladeabschluss-Event: Zellmax war >=3,55 V (Merker), danach Ladeende
      (Leistung > -300 W = nicht mehr nennenswert Laden) fuer 5 min. Setzt den
      Knie-Zyklus neu auf. Re-Arm-Sperre: nur wenn idle ODER seit letztem Anker
      >=0,5 kWh netto entnommen (verhindert Mehrfach-Reset im Ladeschluss-Flattern).
    mode: single
    triggers:
      - trigger: numeric_state
        entity_id: sensor.byd_leistung
        above: -300
        for: "00:05:00"
    conditions:
      - condition: state
        entity_id: input_boolean.byd_top_erreicht
        state: "on"
      - condition: numeric_state
        entity_id: sensor.byd_soc
        above: 94
      - condition: state
        entity_id: binary_sensor.byd_daten_frisch
        state: "on"
      - condition: template
        value_template: >
          {{ is_state('input_select.byd_knie_zyklus_status','idle')
             or states('sensor.byd_netto_energie_seit_voll')|float(0) >= 0.5 }}
    actions:
      - action: utility_meter.reset
        target:
          entity_id:
            - sensor.byd_geladen_seit_voll
            - sensor.byd_entladen_seit_voll
      - action: input_number.set_value
        target: { entity_id: input_number.byd_knie_ref_frozen }
        data:
          value: "{{ states('input_number.byd_knie_referenzspannung')|float(3.20) }}"
      - action: input_boolean.turn_off
        target: { entity_id: input_boolean.byd_knie_ueberschwelle_gesehen }
      - action: input_text.set_value
        target: { entity_id: input_text.byd_knie_cycle_id }
        data: { value: "{{ now().isoformat() }}" }
      - action: input_datetime.set_datetime
        target: { entity_id: input_datetime.byd_voll_anker_zeit }
        data: { datetime: "{{ now().isoformat() }}" }
      - action: input_select.select_option
        target: { entity_id: input_select.byd_knie_zyklus_status }
        data: { option: "armed" }
      - action: input_boolean.turn_on
        target: { entity_id: input_boolean.byd_knie_armed }
      - action: input_boolean.turn_off
        target: { entity_id: input_boolean.byd_top_erreicht }
```

- [ ] **Step 2: Reload + Verifikation (Logik, ohne echten Ladeabschluss)**

Reload Automationen. `ha_get_automation_traces` bleibt leer bis zum realen Ereignis - stattdessen Konsistenz prüfen:
```
ha_get_state(["automation.byd_voll_anker","automation.byd_top_erreicht_merker"])
```
Erwartet: beide `on` (aktiv), keine Config-Fehler. Voll-Anker wird an der nächsten realen Vollladung verifiziert (Task 9).

- [ ] **Step 3: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): Voll-Anker Automation (Ladeabschluss-Event, Re-Arm-Sperre)"
```

---

### Task 7: Überschwelle-Guard + Latch-Automation

**Files:**
- Modify: `packages/byd_bmu.yaml` (unter `automation:`)

**Interfaces:**
- Consumes: `sensor.byd_modul_2_zellspannung_min`, `input_number.byd_knie_ref_frozen`, `input_select.byd_knie_zyklus_status`, `input_boolean.byd_knie_armed`, `input_boolean.byd_knie_ueberschwelle_gesehen`, `binary_sensor.byd_entladeband`, `binary_sensor.byd_daten_frisch`, `sensor.byd_netto_energie_seit_voll`.
- Produces: setzt `byd_knie_ueberschwelle_gesehen`=on; setzt `byd_knie_zyklus_status`=latched + `byd_knie_armed`=off.

- [ ] **Step 1: YAML einfügen**

```yaml
  - id: byd_knie_ueberschwelle_gesehen
    alias: "BYD | Knie Ueberschwelle gesehen"
    description: "Setzt das Guard-Flag, sobald Modul-2-min seit dem Anker klar (>= ref+30 mV) oberhalb der Schwelle war."
    mode: single
    triggers:
      - trigger: template
        value_template: >
          {{ states('sensor.byd_modul_2_zellspannung_min')|float(0)
             > states('input_number.byd_knie_ref_frozen')|float(3.20) + 0.03 }}
    conditions:
      - condition: state
        entity_id: input_boolean.byd_knie_armed
        state: "on"
    actions:
      - action: input_boolean.turn_on
        target: { entity_id: input_boolean.byd_knie_ueberschwelle_gesehen }

  - id: byd_knie_latch
    alias: "BYD | Knie-Latch (Nettoenergie festhalten)"
    description: >-
      Latcht, wenn Modul-2-min zum ersten Mal seit Voll die eingefrorene Referenz
      (Default 3,20 V) 3 min lang unterschreitet, im standardisierten Entladeband
      (+500..1500 W ueber die Haltezeit), bei frischen Daten und gesehener Ueberschwelle.
      Die 3-min-Haltezeit ersetzt eine explizite mV-Hysterese (starke Entprellung).
    mode: single
    triggers:
      - trigger: numeric_state
        entity_id: sensor.byd_modul_2_zellspannung_min
        below: input_number.byd_knie_ref_frozen
        for: "00:03:00"
    conditions:
      - condition: state
        entity_id: input_select.byd_knie_zyklus_status
        state: "armed"
      - condition: state
        entity_id: input_boolean.byd_knie_ueberschwelle_gesehen
        state: "on"
      - condition: state
        entity_id: binary_sensor.byd_entladeband
        state: "on"
        for: "00:03:00"
      - condition: state
        entity_id: binary_sensor.byd_daten_frisch
        state: "on"
      - condition: template
        value_template: "{{ has_value('sensor.byd_netto_energie_seit_voll') }}"
    actions:
      - action: input_select.select_option
        target: { entity_id: input_select.byd_knie_zyklus_status }
        data: { option: "latched" }        # der Latch-Sensor (Task 5) snapshottet hierauf
      - action: input_boolean.turn_off
        target: { entity_id: input_boolean.byd_knie_armed }
```

- [ ] **Step 2: Reload + Verifikation**

```
ha_get_state(["automation.byd_knie_latch","automation.byd_knie_ueberschwelle_gesehen"])
```
Erwartet: beide aktiv, keine Fehler. Funktionaler Latch wird am realen tiefen Entladezyklus verifiziert (Task 9).

- [ ] **Step 3: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): Ueberschwelle-Guard + Knie-Latch Automation"
```

---

### Task 8: Zyklus-Gültigkeit (invalid-Marker)

**Files:**
- Modify: `packages/byd_bmu.yaml` (unter `automation:`)

**Interfaces:**
- Consumes: `binary_sensor.byd_daten_frisch`, `sensor.byd_modul_2_zellspannung_min`, `input_number.byd_knie_ref_frozen`, `input_select.byd_knie_zyklus_status`, `sensor.byd_netto_energie_seit_voll`, Home-Assistant-Start.
- Produces: setzt `byd_knie_zyklus_status`=invalid + `input_text.byd_knie_invalid_grund`.

- [ ] **Step 1: YAML einfügen**

```yaml
  - id: byd_knie_invalid
    alias: "BYD | Knie-Zyklus ungueltig markieren"
    description: >-
      Markiert den laufenden armed-Zyklus als invalid bei Mess-Qualitaetsproblemen:
      Datenluecke nahe Knie, Neustart nahe Knie, unplausibler Netto-Sprung.
    mode: queued
    max: 5
    triggers:
      - trigger: state
        id: datenluecke
        entity_id: binary_sensor.byd_daten_frisch
        to: "off"
        for: "00:02:00"
      - trigger: homeassistant
        id: neustart
        event: start
      - trigger: state
        id: zaehlersprung
        entity_id: sensor.byd_netto_energie_seit_voll
    conditions:
      - condition: state
        entity_id: input_select.byd_knie_zyklus_status
        state: "armed"
      - condition: or
        conditions:
          # Datenluecke oder Neustart nur relevant, wenn Modul-2 nahe/unter Schwelle (Knie-Kandidat)
          - condition: and
            conditions:
              - condition: trigger
                id: ["datenluecke", "neustart"]
              - condition: template
                value_template: >
                  {{ states('sensor.byd_modul_2_zellspannung_min')|float(9)
                     <= states('input_number.byd_knie_ref_frozen')|float(3.20) + 0.01 }}
          # Unplausibler Sprung: Netto-Aenderung > 3 kWh in einem Schritt (bei 60 s physikalisch unmoeglich)
          - condition: template
            value_template: >
              {{ trigger.id == 'zaehlersprung'
                 and trigger.from_state is not none and trigger.to_state is not none
                 and trigger.from_state.state not in ['unknown','unavailable']
                 and trigger.to_state.state not in ['unknown','unavailable']
                 and (trigger.to_state.state|float - trigger.from_state.state|float)|abs > 3 }}
    actions:
      - action: input_select.select_option
        target: { entity_id: input_select.byd_knie_zyklus_status }
        data: { option: "invalid" }
      - action: input_text.set_value
        target: { entity_id: input_text.byd_knie_invalid_grund }
        data: { value: "{{ trigger.id }} @ {{ now().strftime('%Y-%m-%d %H:%M') }}" }
```

- [ ] **Step 2: Reload + Verifikation**

```
ha_get_state("automation.byd_knie_invalid")
```
Erwartet: aktiv, keine Fehler. (Funktionale Auslösung nur bei echten Störfällen; nicht künstlich erzwingen.)

- [ ] **Step 3: Commit**

```bash
git add packages/byd_bmu.yaml
git commit -m "feat(byd): Zyklus-Gueltigkeitslogik (invalid-Marker)"
```

---

### Task 9: Live-Verifikation an realem Zyklus + Doku

**Files:**
- Modify: `docs/byd-bmu-monitoring.md` (Abschnitt "Modul-2 Frühwarnung")
- Create: `docs/livetest-byd-modul2-fruehwarnung-2026-07.md`

- [ ] **Step 1: Deploy live sicherstellen**

Package live in `/config/packages/byd_bmu.yaml` (in-place, identisch zum Repo-Stand), Reload ausgeführt, keine Config-Fehler (`ha_get_logs source=system level=ERROR`).

- [ ] **Step 2: Ganzen Zyklus beobachten**

An der nächsten realen Vollladung + tiefen Entladung prüfen (`ha_get_automation_traces`, `ha_get_state`, `ha_get_history`):
- Voll-Anker feuert am Ladeabschluss (Status → armed, utility_meter auf 0, ref_frozen gesetzt, ueberschwelle_gesehen kippt danach on).
- Bei Modul-2-min < 3,20 V (3 min, im Entladeband) latcht `sensor.byd_modul2_netto_bis_knie` mit plausiblen Attributen (netto_kwh, sauberer_zyklus, a_absackung_mv).
- Nach Latch: Status latched, armed off.
Erwartungswert grobe Größenordnung: netto bis Knie ~9-11 kWh (Vollhub bis SoC ~21 %).

- [ ] **Step 3: Protokoll + Doku schreiben**

`docs/livetest-byd-modul2-fruehwarnung-2026-07.md`: beobachtete Anker-/Latch-Zeiten, Rohwerte, ob `sauberer_zyklus`, Abweichungen. Repo-Konvention (Memory "Livetests im Repo dokumentieren").
`docs/byd-bmu-monitoring.md`: kurzer Abschnitt Betrieb/Interpretation (Referenz tunen, Trend lesen, Alarm bewusst offen).

- [ ] **Step 4: Commit**

```bash
git add docs/livetest-byd-modul2-fruehwarnung-2026-07.md docs/byd-bmu-monitoring.md
git commit -m "docs(byd): Modul-2 Fruehwarnung Livetest-Protokoll + Betriebsdoku"
```

---

### Task 10: Dashboard + PR

**Files:**
- Dashboard "Akku-Gesundheit (BMU)" (live via `ha_config_set_dashboard`, kein Repo-YAML sofern Dashboard live-only).

- [ ] **Step 1: Karten ergänzen**

In Sektion "Akku-Gesundheit (BMU)": ApexCharts-Verlauf `sensor.byd_modul2_absackung` (mV) und `sensor.byd_modul2_netto_bis_knie` (kWh, als Punkte/Balken über Zeit), plus Status-Chip `input_select.byd_knie_zyklus_status`. Referenz-Regler `input_number.byd_knie_referenzspannung` als `number`-Card.

- [ ] **Step 2: Verifikation**

Dashboard lädt fehlerfrei; Karten zeigen die Entitäten.

- [ ] **Step 3: Privacy-Scan + PR**

Privacy-Scan aller getrackten Dateien (öffentliches Repo). Dann Branch pushen und PR gegen `feat/byd-bmu-monitoring` öffnen (oder direkt in PR #39 integrieren - mit Ben klären). **Push/PR erst nach expliziter Freigabe durch Ben.**

---

## Self-Review

- **Spec-Abdeckung:** Sensor A (Task 4), Nettoenergie/utility_meter (Task 3), Voll-Anker (Task 6), Lastband/Frische (Task 2), Latch+Guard+Hysterese-via-Hold (Task 7), Latch-Attribute (Task 5), Gültigkeitslogik (Task 8), Helfer (Task 1), Deploy/Doku/Dashboard (Task 9-10). Alle §-Abschnitte der Spec abgedeckt.
- **Bewusste Vereinfachungen ggü. Spec/Codex (dokumentiert):** Frische = `has_value` statt exakter 120-s-Altersprüfung (keine Time-Pattern-Automation); mV-Hysterese über 3-min-Haltezeit statt separatem ±5-mV-Band; Latch-Last = Momentan-`leistung_w` (durch Entladeband-Gate bereits auf 500-1500 W beschränkt) statt Fenster-Mittel. Jede ist im Plan begründet; keine ändert das Messziel.
- **Typ-/Namenskonsistenz:** Entity-IDs durchgängig identisch (byd_knie_ref_frozen, byd_knie_zyklus_status, byd_netto_energie_seit_voll, byd_modul2_absackung, byd_modul2_netto_bis_knie, byd_geladen/entladen_seit_voll). Vorzeichen positiv=Entladen überall gleich.
- **Offen für Live-Kalibrierung (kein Blocker):** exakte Anker-Zellspannung (3,55 vs 3,60), Lastband-Breite, ob 3,20 V oft genug feuert. In Task 9 verifiziert/nachgezogen.
