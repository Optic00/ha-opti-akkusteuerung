# Prognosebasierte & Manuelle Steuerung für Homeassistant von SMA STP SE Hybrid-Wechselrichter #

**DISCLAMER: Work in Progress, die Software ist noch nicht fertig und vollständig! Alles auf eigene Gefahr! Ich übernehme keine Verantwortung für Schäden oder Probleme die hiermit entstehen.**
Dieses Projekt wird in keinster Weise von der Firma SMA begleitet oder supported.

Dieses Projekt ist als Baukasten zu sehen, nicht als "schlüsselfertige Lösung". Leider kann ich keinen persönlichen Support leisten, es gibt aber den einen oder anderen User der schon mal ausgeholfen hat übers Forum oder heir im Issues Tracker. Vielen Dank hierfür!

**September 2025 - Neue Modbusadressen bekannt um besser steuern zu können***

Dank eines Users im Photovoltaikforum (https://www.photovoltaikforum.com/thread/215473-begrenzen-der-lade-entladeleistung-byd-mit-stp-se/?postID=4033278#post4033278) gibt es von SMA jetzt eine bisher unbekannte Möglichkeit direkt die maximale Lade- und Entladestärke direkt zu steuern. So Konnte die Steuerung extrem aufgeräumt und reduziert werden. Es ist nun möglich den Akku z.B. auf "nur Laden" zu stellen und einen maximalen Ladewert anzugeben. Ich habe außerdem Templates für einen dynamischen SoC und Ladesoll erstellt die anhang der Akkukapazität und Resterzeugung (PV-Sol) die besten Zeitpunkte errechnen den Akku von 50 auf 60, auf 70, auf 80% usw. zu Laden. Außerdem habe ich in der Akkusteuerung neue Helfer für min/max SoC erstellt. 

Es ist noch Luft nach oben um das Ganze etwas abzurunden. Hier erstmal die erste Version. Den Teil mit dynamischen Strompreisen habe ich für diesen Herbst/Winter vorgenommen zumindest in einer groben Version einzubauen. 

**Neue Betafirma seit 16.07.2024 stellt wieder die alte Funktionalität her, dass der Wechselrichter direkt über Modbus gesteurt werden kann**
Grid Guard Code usw. nicht mehr notwendig!

# Anleitung # 

Was macht das hier eigentlich?

Akkuladesteuerung über den WR selbst (wenn man die Updates früh genug deaktiviert hat ODER die neue Betafirmware für den SHM 2.0 hat)

Ein Part ist die Reine Akku Lade-/Entladesteuerung die man manuell auswählen kann, der andere Part die Opti-Automatik welche die Ladestärke auf 0.2C (oder einen gewünschte Ladestärke) begrenzt, den Akku morgens erstmal auf 50% lädt und dann pausiert bis die gewünschte Restproduktionsprognose erreicht ist. Dann wird der Akku bis 90% weiter mit 0.2C beladen, danach mit 1kW bis 100%.

Es sollte die SMA Integration von HA eingerichtet werde um den SoC des Akkus auslesen zu können sowie ein Solcast Account für die Prognose der PV-Erträge!

# Komponenten #

**opti-automatik.yaml** - Hiermit wird über den SHM 2.0 und freigeschaltetem GGC der Akku mittels der weiteren Automation gezielt geladen, pausiert und zuende geladen mit 0.2C bzw. 1kW. 

**sma-se-akku-steuerung.yaml** - Falls man den WR noch direkt ansteuern kann und die letzten Updates nicht hat / die neue Beta Firmware (siehe oben) kann man diese Steuer-Automatik nutzen.

**configuration.yaml** - Eintrag zum Wechselrichter. Den Sensor für die Temperatur bitte mitnehmen, ohne Sensor funktioniert die Modbus Integration nicht zuverlässig.

Wer erstmal nur die reine Akkusteuerung möchte, braucht nur die "sma-se-akku-steuerung.yaml" als Automation anlegen und u.g. Helfer und Überschuss Akkuladung anlegen.

# Einrichtung #

Man benötigt einen Sensor der den möglichen Überschuss für den Akku berechnet und einen für den aktuellen Hausverbrauch. 

**Zum Beispiel für den möglichen Akku-Ladeüberschuss:**

```yaml
    - unique_id: maximaler_ueberschuss_akkuladung
      device_class: power
      state_class: measurement
      name: Maximaler Ueberschuss fuer Akkuladung Watt
      unit_of_measurement: W
      state: "{{ (states('sensor.pv_generation_komplett_watt') | float) - (states('sensor.home_energy_usage_watt') | float) - (states('sensor.sn_xxxxxxx_metering_power_absorbed') | float) + ((states('sensor.goecharger_wallbox_hinten_p_all')  | float )* 1000)  }}"
```

**Zum Beispiel für PV Überschuss warp3_2asq_powernow ist meine Wallbox**

```yaml
    - unique_id: maximaler_ueberschuss_akkuladung
      device_class: power
      state_class: measurement
      name: Maximaler Ueberschuss fuer Akkuladung Watt
      unit_of_measurement: W
      state: "{{ (states('sensor.pv_generation_komplett_watt') | float) - (states('sensor.home_energy_usage_watt') | float) - (states('sensor.sn_3015*****_battery_power_charge_total') | float) + (states('sensor.sn_3015*****_metering_power_absorbed') | float) + ((states('sensor.warp3_2asq_powernow')  | float ))  }}"
```

**Zum Beispiel für den PV-Überschuss um ggf. die 70% Kappung zu erkennen**

```yaml
    - unique_id: akkusteuerung_ueberschuss_pv
      device_class: power
      state_class: measurement
      name: Ueberschuss PV Watt
      unit_of_measurement: W
      state: "{{ (states('sensor.pv_generation_komplett_watt') | float(0) - (states('sensor.home_energy_usage_watt') | float) - (states('sensor.sn_3017XXXXXX_metering_power_absorbed') | float) )  }}"
```

**Zum Beispiel für den Hausverbrauch:**

```yaml
    - unique_id: home_energy_usage_w
      device_class: power
      state_class: measurement
      name: Home Energy Usage Watt
      unit_of_measurement: W
      state: "{{ (states('sensor.sn_3017XXXXXX_metering_power_absorbed') | float) + (states('sensor.sn_3017XXXXXX_grid_power') | float) - (states('sensor.sn_3017XXXXXX_metering_power_supplied') | float)}}"
```

**Hier als Extra zwei Sensoren die Wirkungsgrad und Akku_Zyklen in HA trackien**

```yaml
    - unique_id: byd_akku_wirkungsgrad_lade_entlade
      name: BYD Akku Wirkungsgrad Ladung und Entladung
      unit_of_measurement: factor
      state: "{{ ((states('sensor.sn_3017XXXXXX_battery_discharge_total') | float) / (states('sensor.sn_3017XXXXXX_battery_charge_total') | float) * 100) | round(2) }}"

    - unique_id: byd_akku_zyklen
      name: BYD Akku Zyklen
      unit_of_measurement: factor
      state: "{{ (((((states('sensor.sn_3017XXXXXX_battery_discharge_total') | float) + (states('sensor.sn_3017XXXXXX_battery_charge_total') | float)) / 100 ) * (states('sensor.sn_3017XXXXXX_battery_capacity_total')) | float) / (2*10.2) ) | round(1) }}"
```

**Hier Restlaufzeit berechnen, ich weiss nicht von wem das herkam aber:**

```yaml
    - unique_id: "house_battery_runtime_raw"
      name: "House Battery Runtime Raw"
      unit_of_measurement: "hours"
      state: >
        {% set battery_load = -1 * float(states('sensor.house_battery_load_30_mins')) if states('sensor.house_battery_load_30_mins')|is_number else 0 %}
        {% if battery_load == 0 %}
        {% set runtime = 0 %}
        {% else %}
        {% set solis_remaining_capacity = states('sensor.sn_30156*****_battery_soc_total') %}
        {% set remaining_capacity = 12.8 - (0.1280 * (100.0 - float(solis_remaining_capacity) if solis_remaining_capacity != 'unknown' and solis_remaining_capacity|float >= 0 else 0)) %}
        {% set runtime = remaining_capacity / (battery_load/1000) %}
        {% endif %}
        {{ runtime }}

** zum vorherigen braucht man noch einen Statistik Sensor, bei mir unter sensor/statistik.yaml **

```yaml
    - platform: statistics
      name: "House Battery Load 30 mins"
      entity_id: sensor.sn_3015*****_battery_power_discharge_total
      state_characteristic: mean
      max_age:
        minutes: 30
```

dieser HA-Helfer zur Auswahl des Akku-Modus muss angelegt werden:

<img width="322" alt="image" src="https://github.com/user-attachments/assets/be8c061f-681d-4acb-bbc4-47f3ca9a9a49">

Dann noch einen Schalter **akku_opti_automatik** ob diese Ladeoptimerung überhaupt laufen darf anlegen:

Vier Input Numbers anlegen, Minimaler Wert 100, maximaler Wert 10000 (Watt) für:

- input_number.akkusteuerung_ladestaerke_soll und 
- input_number.akkusteuerung_entladestaerke_soll
- input_number.akkusteuerung_02c_ladestaerke

Dieser Helfer sollte den Wert oder einen leicht niedrigeren Wert enthalten der die 70% Abregelung enthält. (z.B. 7000 Watt bei einer 10kW Watt Anlage oder leicht drunter, etwa 6800 Watt)

- input_number.akkusteuerung_wr_70proz_ueberschuss_grenze

Dieser Helfer bekommt den Wert der maximalen AC Einspeiseleistung des Wechselrichters. Bei einem SMA STP SE 10.0 also 10000 Watt bzw. leicht drunter etwa 9900. Die Soll-Ladestärke wird dann auf 100 Watt gesetzt und so autoamtisch der AC-Überschuss in den Akku geladen (falls dieser noch nicht voll ist)

- input_number.akkusteuerung_wr_ac_ueberschuss_grenze

für den letzten z.B. 1-100kWh, dies steuert die Schwelle ab wann der Akku von 50% aufwärts geladen wird.

- input_number.akkusteuerung_ab_welchem_restertrag_vollladen

<img width="500" alt="image" src="https://github.com/Optic00/ha-smase-akkusteuerung/assets/20187253/6a1ae098-817a-4029-b732-442eeee4ae6d">

So schaut es im HA aktuell aus. Hab noch einen Dummy-Schalter für den Fall das die Module/Anlage mal ausfällt. Da ist aber noch nichts aktives in der Automation enthalten.

<img width="505" alt="image" src="https://github.com/user-attachments/assets/82cfe4d3-9034-4cf5-953c-65b624f5250e">

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
              content: "{{( states(entity) | float / 1000) | round(2) }}  kW "
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
            content: "{{ states(entity) | round (1)}}% η"
            icon: mdi:vector-difference
            icon_color: orange
          - type: template
            entity: sensor.byd_12_8_akku_zyklen
            content: "{{ states(entity)}}"
            icon: mdi:counter
            icon_color: yellow
          - type: entity
            entity: sensor.sn_301XXXXXXX_battery_temp_a
            icon: mdi:battery-charging-wireless-outline
          - type: entity
            entity: sensor.sma_stp_se_temperatur
            icon: mdi:power-socket-it
      - type: entities
        entities:
          - entity: sensor.akkusteuerung_dynamische_ladestaerke
          - entity: sensor.pv_forecast_bewertung_heute
          - entity: sensor.pv_forecast_bewertung_morgen
          - entity: sensor.house_battery_runtime_raw
      - type: custom:mushroom-select-card
        entity: input_select.akkusteuerung_sma_wr
        name: Akkusteuerung
        primary_info: name
        secondary_info: last-changed
      - type: tile
        entity: input_boolean.akku_opti_automatik
        features_position: bottom
        vertical: false
      - type: tile
        entity: input_boolean.akku_nach_preis_laden
        features_position: bottom
        vertical: false
      - type: tile
        entity: input_boolean.pv_module_nicht_verfugbar
        name: PV-Module NA (z.B. Schnee bedeckt)
        features_position: bottom
        vertical: false
      - type: horizontal-stack
        cards:
          - type: tile
            entity: sensor.sn_301XXXXXXX_battery_discharge_total
            name: Entladen Watt
            features_position: bottom
            vertical: false
          - type: tile
            entity: sensor.sn_301XXXXXXX_battery_charge_total
            name: Laden Watt
      - type: entities
        entities:
          - entity: sensor.betriebsstatus_sma_stp_se_10_0
            name: Betriebsstatus
      - type: entities
        entities:
          - entity: input_number.akkusteuerung_ladestaerke_soll
          - entity: input_number.akkusteuerung_entladestaerke_soll
            name: Entladestärke
          - entity: input_number.akkusteuerung_ab_welchem_restertrag_vollladen
            name: Restladeschwelle
          - entity: input_number.akkusteuerung_02c_ladestaerke
            name: Ladestärke 0.2C
          - entity: input_number.akkusteuerung_wr_ac_ueberschuss_grenze
            name: WR AC-Grenze
          - entity: input_number.akkusteuerung_wr_70proz_ueberschuss_grenze
            name: 70% Grenze
          - entity: sensor.house_battery_runtime_raw
            name: Akkulaufzeit
          - entity: sensor.ueberschuss_pv_watt
          - entity: switch.0x54ef441000ac86e9
            name: WR Lüfter
            secondary_info: last-changed
          - entity: input_number.akkusteuerung_max_ladestaerke
            name: Akku max Ladestärke
          - entity: input_number.akkusteuerung_min_ladestaerke
            name: Akku min Ladestärke
          - entity: input_number.akkusteuerung_max_entladestaerke
            name: Akku max Entadestärke
          - entity: input_number.akkusteuerung_min_entladestaerke
            name: Akku min Entladestärke
          - entity: input_number.minsoc
          - entity: input_number.maxsoc
      - type: heading
        icon: ""
        heading: Debugging
        heading_style: title
      - type: entities
        entities:
          - entity: sensor.akku_target_soc_intelligent
          - entity: sensor.akku_net_verfugbare_energie
          - entity: sensor.verbleibende_sonnenstunden
```

# ToDo #

- [x] Akku im Winter mindestens 1x die Woche automatisch auf 100% Laden
- [x] Evtl. Ladegeschwindigkeit ab 95-98% auf 500 Watt begrenzen
- [ ] SBS Version
- [ ] HACS Version für die reine Akkusteuerung
- [ ] English Version of this?

# Fehlerbehebungen

Stelle zuerst sicher, dass du alle nötigen Komponenten und Helfer in Home Assistant installiert hast.
Nun schaue die beiden Automatisierungen an, ob sie korrekt durch laufen und nicht irgendwo 'mittendrin' stecken bleiben. Das geht am besten über die 'Traces' Ansicht.

Manchmal passiert es, dass der maximale Ladestrom des Akkus alle 4 Minuten gesetzt wird, aber nach jeweils einer Minute für 3 Minuten wieder auf einen beliebigen anderen Wert springt. Prüfe die Konfiguration des SMA Home Managers im SunnyPortal. Die  prognosebasierte Akkusteuerung muss ausgeschaltet sein muss, sonst wird die maximale Ladeleistung regelmäßig neu gesetzt.

Wenn die Ladestärke periodisch immer wieder auf 0 fällt, prüfe die Shadefix-Einstellungen. Diese zieht offenbar alle 6 Minuten 'kurz den Stecker' und die Ladeleistung geht auf 0. Das lässt sich in Maßen einstellen - je nach Setup auf 30 Minuten - oder sogar ganz ausschalten, falls es nicht benötigt wird.