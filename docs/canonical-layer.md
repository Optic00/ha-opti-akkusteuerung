# Canonical-Layer: opti_*-Sensoren — Nutzerleitfaden

## Überblick

Der Canonical-`opti_*`-Layer entkoppelt die Strategie von der konkreten Hardware.
Eine einzige Mapping-Datei (`packages/opti_mapping.yaml`) übersetzt die eigenen
Wechselrichter- und Preis-Entitäten auf 13 kanonische `sensor.opti_*`-Sensoren.
Alle nachgelagerten Pakete (`packages/opti_derived.yaml`, `automations/opti_strategie.yaml`)
konsumieren ausschließlich diese kanonischen Sensoren — keine Seriennummern, keine
Anbieter-spezifischen IDs tauchen dort auf.

```
opti_mapping.yaml  →  sensor.opti_*  →  opti_derived.yaml  →  opti_strategie.yaml
(deine Hardware)       (Canonical)       (Score/Preis/…)       (Modus-Entscheidung)
```

> **Gitignored:** `packages/opti_mapping.yaml` ist in `.gitignore` eingetragen.
> Deine echten Entitäts-IDs werden nie ins öffentliche Repo hochgeladen.

---

## Mapping-Tabelle: alle 13 opti_*-Sensoren

| opti-Sensor | Einheit | Wertebereich / Vorzeichen | SMA-Beispielquelle | Huawei-Beispielquelle | Umrechnung |
|---|---|---|---|---|---|
| `sensor.opti_soc` | % | 0–100 | `sensor.sma_battery_soc` | `sensor.huawei_battery_soc` | Direkt (`float(0)`) |
| `sensor.opti_battery_temp` | °C | beliebig; **optional** (Default 20 °C) | `sensor.sma_battery_temp` | `sensor.huawei_battery_temp` | Direkt; kein Sensor → `float(20)` |
| `sensor.opti_battery_capacity_kwh` | kWh | > 0 | `sensor.sma_battery_capacity` (Wh) | `sensor.huawei_battery_capacity` (kWh) | SMA: `float(0) / 1000`; Huawei: direkt |
| `sensor.opti_pv_power_w` | W | ≥ 0 (AC-Ausgangsleistung) | `sensor.sma_pv_power` | `sensor.huawei_pv_power` | Direkt |
| `sensor.opti_pv_generation_w` | W | ≥ 0 (DC-Eingangsleistung) | `sensor.sma_pv_generation` | `sensor.huawei_pv_generation` | Direkt |
| `sensor.opti_grid_export_w` | W | **≥ 0** (positiv = Einspeisung) | `sensor.sma_grid_export_power` | `sensor.huawei_grid_export_power` | Einspeisung positiv: `[0, float(0)] \| max`; Einspeisung negativ: `[0, float(0) * -1] \| max` |
| `sensor.opti_house_consumption_w` | W | ≥ 0 | `sensor.sma_house_consumption` | `sensor.huawei_house_consumption` | Direkt |
| `sensor.opti_price_current_ct_kwh` | ct/kWh | beliebig | `sensor.tibber_electricity_price` (EUR/kWh) | `sensor.nordpool_electricity_price` (EUR/kWh) | `float(0) * 100` |
| `sensor.opti_price_series` | ct/kWh | Attribut `today`/`tomorrow` = Listen in ct/kWh | `sensor.tibber_electricity_price` | `sensor.nordpool_electricity_price` | EUR-Attribut-Listen × 100 (im Mapping normalisiert) |
| `sensor.opti_forecast_today_kwh` | kWh | ≥ 0; Attribut `estimate10` (P10) | `sensor.solcast_pv_forecast_forecast_today` | `sensor.solcast_pv_forecast_forecast_today` | Direkt |
| `sensor.opti_forecast_tomorrow_kwh` | kWh | ≥ 0; Attribut `estimate10` (P10) | `sensor.solcast_pv_forecast_forecast_tomorrow` | `sensor.solcast_pv_forecast_forecast_tomorrow` | Direkt |
| `sensor.opti_forecast_remaining_today_kwh` | kWh | ≥ 0 | `sensor.solcast_pv_forecast_forecast_remaining_today` | `sensor.solcast_pv_forecast_forecast_remaining_today` | Direkt |
| `sensor.opti_battery_power_w` | W | **+= laden** (positiv = Batterie lädt) | Zwei Sensoren: `sensor.sma_battery_charge` und `sensor.sma_battery_discharge` (beide ≥ 0) | `sensor.huawei_battery_power` (signierter Sensor) | SMA: `charge - discharge`; Huawei: direkt (oder `* -1` wenn Quelle positiv = Entladen) |

### Konventionen im Detail

**Preise:** Die EUR→ct-Umrechnung (`×100`) findet **ausschließlich** in `opti_mapping.yaml` statt.
Alle nachgelagerten Sensoren arbeiten intern mit ct/kWh.

**Leistung:** Alle Leistungssensoren in Watt (W), alle Energie­größen in kWh.

**`opti_grid_export_w`:** Immer ≥ 0. Positiv = Einspeisung ins Netz. Je nach Wechselrichter
das Vorzeichen im Mapping korrigieren (Kommentare in `opti_mapping.example.yaml` unter
Sensor 6 beschreiben beide Varianten).

**`opti_battery_power_w`:** Positiv = Batterie lädt (`+=`-Konvention).
- SMA: Zwei separate Sensoren für Laden/Entladen (beide ≥ 0) → `charge - discharge`
- Huawei: Ein signierter Sensor → direkt übernehmen. Falls Quelle „positiv = Entladen": `* -1`

**`opti_battery_temp` (optional):** Fehlt ein Temperatur-Sensor, einfach keinen Sensor
angeben — das Mapping liefert dann `float(20)` als neutralen Default (20 °C). Es gibt keine
Abregelung wegen fehlender Temperatur.

---

## Anbieter-Rezepte: Strompreis

### Tibber

Tibber liefert zwei relevante Sensoren:
- **Aktueller Preis (Skalar):** `sensor.tibber_electricity_price` — Einheit EUR/kWh
- **Preis-Reihe:** derselbe Sensor mit Attributen `today`/`tomorrow` als Listen von Dicts
  `{total: …, startsAt: …}` (Schlüssel `total` = EUR/kWh-Wert)

```yaml
# opti_mapping.yaml — Tibber-Beispiel
# Sensor 8 (aktueller Preis, EUR → ct):
state: "{{ states('sensor.tibber_electricity_price') | float(0) * 100 }}"

# Sensor 9 (Preis-Reihe, gleiche Quelle):
availability: "{{ has_value('sensor.tibber_electricity_price') }}"
state: "{{ states('sensor.tibber_electricity_price') | float(0) * 100 }}"
# Attribute today/tomorrow werden automatisch normalisiert (Dict-Schlüssel
# 'total', 'price' oder 'value' sowie skalare Einträge werden erkannt).
```

### Nordpool

`sensor.nordpool_electricity_price` liefert den aktuellen Preis als Zustand (EUR/kWh)
sowie `today`/`tomorrow` als Listen von Floats (Rohpreise in EUR/kWh).

```yaml
# opti_mapping.yaml — Nordpool-Beispiel
state: "{{ states('sensor.nordpool_electricity_price') | float(0) * 100 }}"
# today/tomorrow: skalare Listen → das Mapping-Template verarbeitet sie direkt.
```

### EPEX / aWATTar

EPEX/aWATTar-Integrationen liefern typischerweise den aktuellen Preis als Skalarzustand.
Die Preis-Reihe kann als `today`/`tomorrow`-Attribut mit Floats oder Dicts vorliegen.

```yaml
# opti_mapping.yaml — aWATTar-Beispiel (EUR/kWh-Quelle)
state: "{{ states('sensor.awattar_current_price') | float(0) * 100 }}"
```

> **Hinweis:** Falls die Quelle bereits ct/kWh liefert, `* 100` weglassen und
> den Availability-Check entsprechend anpassen.

---

## Solcast-Anbindung

| opti-Sensor | Typischer Solcast-Sensor |
|---|---|
| `opti_forecast_today_kwh` | `sensor.solcast_pv_forecast_forecast_today` |
| `opti_forecast_tomorrow_kwh` | `sensor.solcast_pv_forecast_forecast_tomorrow` |
| `opti_forecast_remaining_today_kwh` | `sensor.solcast_pv_forecast_forecast_remaining_today` |

**`estimate10`-Attribut (P10-Sicherheitsnetz):** Die `opti_forecast_*_kwh`-Sensoren reichen das
Attribut `estimate10` durch (10. Perzentil der Prognose — pessimistischer Worst-Case-Wert).
Der `opti_forecast_score` nutzt diesen Wert als konservative Untergrenze für die PV-Fit-Bewertung.

```yaml
# Im Mapping (Beispiel für heute):
attributes:
  estimate10: "{{ state_attr('sensor.solcast_pv_forecast_forecast_today', 'estimate10') | float(0) }}"
```

Fehlt `estimate10` im Quell-Sensor, greift `float(0)` als sicherer Fallback — der Score
wird dann etwas konservativer, aber die Automation läuft fehlerfrei.

---

## Mindest-HA-Version

Der Canonical-Layer verwendet das Template-Keyword `has_value()`, das ab
**Home Assistant 2022.9** verfügbar ist. Mit älteren Versionen werden
Availability-Checks als `false` ausgewertet, und alle `opti_*`-Sensoren erscheinen
als `unavailable`.

**Empfehlung:** Aktuelle HA-Release-Version verwenden. Falls `sensor.opti_soc` nach dem
Einrichten auf `unavailable` bleibt, in den Entwicklerwerkzeugen → Template prüfen,
ob `has_value('sensor.DEINE_QUELLE')` korrekt ausgewertet wird.

---

## Installation: Schritt-für-Schritt

### 1. Mapping-Datei anlegen

```bash
cp opti_mapping.example.yaml packages/opti_mapping.yaml
```

### 2. Platzhalter ersetzen

In `packages/opti_mapping.yaml` alle `DEIN_*`-Platzhalter durch die echten Entitäts-IDs ersetzen:

| Platzhalter | SMA-Beispiel | Huawei-Beispiel |
|---|---|---|
| `sensor.DEIN_SOC` | `sensor.sma_battery_soc` | `sensor.huawei_battery_soc` |
| `sensor.DEIN_BATTERIE_TEMP` | `sensor.sma_battery_temp` | `sensor.huawei_battery_temp` |
| `sensor.DEIN_BATTERIE_KAPAZITAET` | `sensor.sma_battery_capacity` (Wh → `/1000`) | `sensor.huawei_battery_capacity` (kWh) |
| `sensor.DEIN_PV_POWER` | `sensor.sma_pv_power` | `sensor.huawei_pv_power` |
| `sensor.DEIN_PV_GENERATION` | `sensor.sma_pv_generation` | `sensor.huawei_pv_generation` |
| `sensor.DEIN_GRID_EXPORT` | `sensor.sma_grid_export_power` | `sensor.huawei_grid_export_power` |
| `sensor.DEIN_HAUSVERBRAUCH` | `sensor.sma_house_consumption` | `sensor.huawei_house_consumption` |
| `sensor.DEIN_STROMPREIS` | `sensor.tibber_electricity_price` | `sensor.nordpool_electricity_price` |
| `sensor.DEIN_PREIS_REIHE` | `sensor.tibber_electricity_price` | `sensor.nordpool_electricity_price` |
| `sensor.DEIN_SOLCAST_HEUTE` | `sensor.solcast_pv_forecast_forecast_today` | ← gleich |
| `sensor.DEIN_SOLCAST_MORGEN` | `sensor.solcast_pv_forecast_forecast_tomorrow` | ← gleich |
| `sensor.DEIN_SOLCAST_VERBLEIBEND` | `sensor.solcast_pv_forecast_forecast_remaining_today` | ← gleich |
| `sensor.DEIN_CHARGE_W` | `sensor.sma_battery_charge` | — (Huawei nutzt Einzel-Sensor) |
| `sensor.DEIN_DISCHARGE_W` | `sensor.sma_battery_discharge` | — |

**Huawei (Einzel-Sensor für Batterieleistung):** Den SMA-Block für `opti_battery_power_w`
auskommentieren und den Huawei-Alternativ-Block einkommentieren. Kommentare in
`opti_mapping.example.yaml` unter Sensor 13 beschreiben beide Varianten.

### 3. Packages aktivieren

In `configuration.yaml` einmalig eintragen (falls noch nicht geschehen):

```yaml
homeassistant:
  packages: !include_dir_named packages/
```

### 4. HA neu starten

*Einstellungen → System → Neustart* (oder `ha core restart`).

### 5. Sensoren prüfen

In den **Entwicklerwerkzeugen → Zustände** alle 13 `sensor.opti_*`-Sensoren suchen:

```
sensor.opti_soc
sensor.opti_battery_temp
sensor.opti_battery_capacity_kwh
sensor.opti_pv_power_w
sensor.opti_pv_generation_w
sensor.opti_grid_export_w
sensor.opti_house_consumption_w
sensor.opti_price_current_ct_kwh
sensor.opti_price_series
sensor.opti_forecast_today_kwh
sensor.opti_forecast_tomorrow_kwh
sensor.opti_forecast_remaining_today_kwh
sensor.opti_battery_power_w
```

Jeder Sensor sollte einen numerischen Wert liefern (nicht `unavailable` / `unknown`).
Bei `unavailable`: unter *Entwicklerwerkzeuge → Template* die `availability`-Formel
des betreffenden Sensors testen — häufig ist der Quell-Sensor noch falsch benannt.

> **Gitignored:** `packages/opti_mapping.yaml` ist in `.gitignore` eingetragen —
> deine echten Entitäts-IDs bleiben lokal und kommen nie ins öffentliche Repo.

---

## Abgeleitete Sensoren (opti_derived.yaml)

`packages/opti_derived.yaml` erzeugt aus den Mapping-Sensoren folgende Entscheidungs-Sensoren
(nicht bearbeiten — nur `opti_mapping.yaml` anpassen):

| Sensor | Beschreibung |
|---|---|
| `sensor.opti_forecast_score` | PV-Fit heute (0–10); nutzt `estimate10` als P10-Sicherheitsnetz |
| `sensor.opti_forecast_score_tomorrow` | PV-Fit morgen (0–10) |
| `sensor.opti_target_soc` | Ziel-SoC (%) basierend auf Restprognose und Hausverbrauch |
| `sensor.opti_charge_power_w` | Dynamische Ladestärke (W) nach SoC-Stufe und Forecast-Score |
| `sensor.opti_price_level` | Preisniveau-Enum (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE / VERY_EXPENSIVE) |
| `sensor.opti_mindestentladepreis_ct_kwh` | Mindestentladepreis = Ladepreis + Preisdifferenz (ct/kWh) |
| `sensor.opti_runtime_h` | Geschätzte Akku-Restlaufzeit (Stunden) |
| `binary_sensor.opti_winter_charging_allowed` | Netzladefreigabe-Gate (Standard: `true`, fail-open) |

> **Warnung — `opti_winter_charging_allowed`:** Dieses Gate ist **fail-open** (`{{ true }}`).
> Solange kein realer Saisonal-Sensor gemappt ist, ist es immer `on`.
> Wenn du einen echten Winter-/Sommer-Sensor anschließt, beachte:
> Das Gate ist **ausschließlich** für die SOC<20- und SOC<80-Ladeblöcke vorgesehen.
> **Keinesfalls** darf es den SOC<15-Notfall-Netzladeblock gaten — dieser muss auch
> im Sommer jederzeit eingreifen können (tiefe Entladung vermeiden).
> Die SOC<75- und SOC<45-Blöcke werden ebenfalls nicht gegated (opportunistische Blöcke,
> sollen ganzjährig greifen).

Diese Sensoren werden von `automations/opti_strategie.yaml` direkt konsumiert —
du beeinflusst sie ausschließlich über dein Mapping und die Helfer-Werte.

---

## Testfälle — Erwartetes Verhalten

| Szenario | Relevante Eingaben | Erwarteter Modus | Hinweise |
|---|---|---|---|
| **Nacht / Netzladen** | `opti_forecast_score` ≤ 2, SoC < 30 %, `opti_price_level` CHEAP | **Akku nur Laden** | Gate `input_boolean.opti_prognose_netzladen` muss `on` sein |
| **Nacht / kein Ladegrund** | Score ≤ 2, SoC > 60 %, Preis EXPENSIVE | **Akku Dynamisch** (Default) | Kein Ladeblock greift → Default-Pfad |
| **Sonne / PV-Überschuss** | `opti_grid_export_w` > 70%-Grenze, SoC < 100 % | **Akku Dynamisch** | Gate `input_boolean.opti_pv_ueberschuss_ladung` muss `on` sein |
| **Tagsüber unter Ziel-SoC** | SoC < `opti_target_soc`, nach Sonnenaufgang | **Akku Dynamisch** | Option „Dynamisch laden wenn SOC < ZielSoC" |
| **Entladen über Ziel-SoC** | SoC > `opti_target_soc` | **Akku nur Entladen** | Option „Nur Entladen wenn SOC > DynZielSoC" |
| **MinSOC-Schutz** | SoC < `input_number.minsoc` | **Akku nur Laden** | Höchste Priorität, überstimmt alle anderen Blöcke |
| **Preis-morgen fehlt** | `opti_price_series`-Attribut `tomorrow` leer / < 4 Gesamtwerte | Preisniveau bleibt **NORMAL** | Fail-safe: `< 4 Preise gesamt → NORMAL` |
| **Forecast fehlt** | `opti_forecast_remaining_today_kwh` → `unavailable` | Prognose-Blöcke inaktiv; MinSOC-Schutz und Cleanup laufen weiter | Default → **Akku Dynamisch** (nur wenn `opti_soc` + `opti_battery_capacity_kwh` verfügbar) |
| **Voller Akku** | SoC > 99 % | **Akku Dynamisch**; Netzlade-Booster wird automatisch deaktiviert | Option „Bei vollem Akku auf Dynamisch" + Cleanup-Block |

**Fail-safe-Verhalten:** Sind `sensor.opti_soc` oder `sensor.opti_battery_capacity_kwh`
`unavailable`, setzt die Strategie keinen Modus — der bisherige Modus bleibt aktiv.
