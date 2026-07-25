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
| `sensor.opti_grid_import_w` | W | **≥ 0** (positiv = Bezug) | `sensor.sma_metering_power_absorbed` | `sensor.huawei_grid_consumption_power` | Bezug positiv: `[0, float(0)] \| max`; Bezug negativ: `[0, float(0) * -1] \| max` |
| `sensor.opti_house_consumption_w` | W | ≥ 0 | `sensor.sma_house_consumption` | `sensor.huawei_house_consumption` | Direkt |
| `sensor.opti_price_current_ct_kwh` | ct/kWh | beliebig | `sensor.tibber_electricity_price` (EUR/kWh) | `sensor.nordpool_electricity_price` (EUR/kWh) | `float(0) * 100` |
| `sensor.opti_price_series` | ct/kWh | Attribut `today`/`tomorrow` = Listen in ct/kWh | `sensor.tibber_electricity_price` | `sensor.nordpool_electricity_price` | EUR-Attribut-Listen × 100 (im Mapping normalisiert) |
| `sensor.opti_forecast_today_kwh` | kWh | ≥ 0; Attribut `estimate10` (P10) | `sensor.solcast_pv_forecast_forecast_today` | `sensor.solcast_pv_forecast_forecast_today` | Direkt |
| `sensor.opti_forecast_tomorrow_kwh` | kWh | ≥ 0; Attribut `estimate10` (P10) | `sensor.solcast_pv_forecast_forecast_tomorrow` | `sensor.solcast_pv_forecast_forecast_tomorrow` | Direkt |
| `sensor.opti_forecast_remaining_today_kwh` | kWh | ≥ 0; Attribut `estimate10` (P10, optional) | `sensor.solcast_pv_forecast_forecast_remaining_today` | `sensor.solcast_pv_forecast_forecast_remaining_today` | Direkt |
| `sensor.opti_battery_power_w` | W | **+= laden** (positiv = Batterie lädt) | Zwei Sensoren: `sensor.sma_battery_charge` und `sensor.sma_battery_discharge` (beide ≥ 0) | `sensor.huawei_battery_power` (signierter Sensor) | SMA: `charge - discharge`; Huawei: direkt (oder `* -1` wenn Quelle positiv = Entladen) |

### Konventionen im Detail

**Preise:** Die EUR→ct-Umrechnung (`×100`) findet **ausschließlich** in `opti_mapping.yaml` statt.
Alle nachgelagerten Sensoren arbeiten intern mit ct/kWh.

**Leistung:** Alle Leistungssensoren in Watt (W), alle Energie­größen in kWh.

**`opti_grid_export_w`:** Immer ≥ 0. Positiv = Einspeisung ins Netz. Je nach Wechselrichter
das Vorzeichen im Mapping korrigieren (Kommentare in `opti_mapping.example.yaml` unter
Sensor 6 beschreiben beide Varianten).

**`opti_grid_import_w` (optional):** Immer ≥ 0. Positiv = Bezug aus dem Netz. Symmetrisch zu
`opti_grid_export_w`, nur mit umgekehrtem Vorzeichen des Quellzählers (Sensor 6b in
`opti_mapping.example.yaml`). Wird von der Strategie **nicht** konsumiert - er dient nur der
Anzeige (z. B. der `power-flow-card-plus` im Übersichts-Dashboard, die Bezug UND Einspeisung als
Leistung braucht). Bei SMA typischerweise `..._metering_power_absorbed` (positiv = Bezug), bei
einem signierten Netz-Zähler das Vorzeichen entsprechend drehen. Fehlt eine Bezugsquelle, den
Sensor einfach ungemappt lassen.

**`opti_battery_power_w`:** Positiv = Batterie lädt (`+=`-Konvention).
- SMA: Zwei separate Sensoren für Laden/Entladen (beide ≥ 0) → `charge - discharge`
- Huawei: Ein signierter Sensor → direkt übernehmen. Falls Quelle „positiv = Entladen": `* -1`

**`opti_battery_temp` (optional):** Fehlt ein Temperatur-Sensor, einfach keinen Sensor
angeben — das Mapping liefert dann `float(20)` als neutralen Default (20 °C). Es gibt keine
Abregelung wegen fehlender Temperatur.

---

## Anbieter-Rezepte: Strompreis

### Tibber

**Vorsicht, Entity-Name kann abweichen:** Die Core-Tibber-Integration benennt ihren
Preis-Sensor nach dem Namen deiner Zählstelle/Wohnung
(`sensor.electricity_price_<dein_home_name>`), nicht garantiert
`sensor.tibber_electricity_price`. Prüfe den tatsächlichen Namen in den
**Entwicklerwerkzeugen → Zustände** (nach `tibber` filtern), bevor du das Mapping ausfüllst.

Tibber liefert dort typischerweise zwei relevante Dinge:
- **Aktueller Preis (Skalar):** der Sensor-Zustand selbst — Einheit EUR/kWh
- **Preis-Reihe:** Attribute `today`/`tomorrow` als Listen von Dicts
  `{total: …, startsAt: …}` (Schlüssel `total` = EUR/kWh-Wert)

**Vor dem Mapping prüfen, ob `today`/`tomorrow` überhaupt vorhanden sind:**
Entwicklerwerkzeuge → Vorlage, dann:

```jinja
{{ state_attr('sensor.DEIN_TIBBER_SENSOR', 'today') }}
{{ state_attr('sensor.DEIN_TIBBER_SENSOR', 'tomorrow') }}
```

Liefert das `none` oder eine leere Liste, fehlt die Preis-Reihe auf dem Sensor - dann hilft
nur der Weg über den Service unten.

```yaml
# opti_mapping.yaml — Tibber-Beispiel (Sensor liefert today/tomorrow direkt)
# Sensor 8 (aktueller Preis, EUR → ct):
state: "{{ states('sensor.DEIN_TIBBER_SENSOR') | float(0) * 100 }}"

# Sensor 9 (Preis-Reihe, gleiche Quelle):
availability: "{{ has_value('sensor.DEIN_TIBBER_SENSOR') }}"
state: "{{ states('sensor.DEIN_TIBBER_SENSOR') | float(0) * 100 }}"
# Attribute today/tomorrow werden automatisch normalisiert (Dict-Schlüssel
# 'total', 'price' oder 'value' sowie skalare Einträge werden erkannt).
```

**Alternative: trigger-basiertes Rezept über den Service `tibber.get_prices`.**
Liefert dein Preis-Sensor kein `today`/`tomorrow`-Attribut, kannst du die Preis-Reihe
stattdessen stündlich per Service abrufen und in einen eigenen Template-Sensor schreiben:

```yaml
# packages/opti_mapping.yaml — Tibber-Preisreihe per Service (Alternativ-Rezept)
template:
  - trigger:
      - trigger: time_pattern
        minutes: 5
    action:
      - action: tibber.get_prices
        data:
          start: "{{ now().replace(hour=0, minute=0, second=0).isoformat() }}"
          end: "{{ (now() + timedelta(days=1)).replace(hour=23, minute=59).isoformat() }}"
        response_variable: preise
    sensor:
      - name: "Opti Preis-Reihe Tibber"
        unique_id: opti_price_series_tibber_service
        state: "{{ states('sensor.DEIN_TIBBER_SENSOR') | float(0) * 100 }}"
        attributes:
          today: >-
            {% set tag = now().strftime('%Y-%m-%d') %}
            {% set ns = namespace(preise=[]) %}
            {% for p in preise.prices.values() | first if p.start_time.startswith(tag) %}
              {% set ns.preise = ns.preise + [ (p.price | float(0)) * 100 ] %}
            {% endfor %}
            {{ ns.preise }}
          tomorrow: >-
            {% set morgen = (now() + timedelta(days=1)).strftime('%Y-%m-%d') %}
            {% set ns = namespace(preise=[]) %}
            {% for p in preise.prices.values() | first if p.start_time.startswith(morgen) %}
              {% set ns.preise = ns.preise + [ (p.price | float(0)) * 100 ] %}
            {% endfor %}
            {{ ns.preise }}
```

`preise` ist die `response_variable` des `tibber.get_prices`-Service-Aufrufs. Laut
aktueller Service-Doku liefert er `prices: {<home_id>: [{start_time, price}, ...]}` -
das Rezept oben geht von genau dieser Struktur aus (`start_time`, nicht `startsAt`;
`price`, nicht `total` - anders als die Attribut-Struktur des normalen Preis-Sensors
weiter oben).

> ⚠️ **Ungetestet gegen alle Tibber-Versionen** — die Struktur der `tibber.get_prices`-Antwort
> kann sich zwischen Integrations-Versionen unterscheiden. Nach dem Einrichten in den
> Entwicklerwerkzeugen → Vorlage die `response_variable`-Struktur prüfen (`{{ preise }}`)
> und die `today`/`tomorrow`-Attribute des neuen Sensors gegen die erwarteten
> ct/kWh-Listen abgleichen, bevor du dich darauf verlässt.

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

> **Raster-Kontrakt:** `today`/`tomorrow` müssen Stundenlisten (Länge 20-27) oder Viertelstundenlisten (Länge 80-108) sein - die Peak-Reserve (`sensor.opti_peak_reserve_soc`) leitet die Slot-Länge je Liste aus der Listenlänge ab und deaktiviert sich bei anderen Listenlängen automatisch (`gueltig: false`).

---

## Setup ohne dynamische Strompreise

Kein dynamischer Tarif (Tibber, Nordpool, aWATTar …)?
Die Steuerung läuft trotzdem - sie fällt dann auf reine Eigenverbrauchsoptimierung nach Sonnenstand, Prognose und Hausverbrauch zurück.
Die Preis-Sensoren (Sensor 8 `opti_price_current_ct_kwh`, Sensor 9 `opti_price_series`) lässt du in diesem Fall einfach ungemappt.

**Was du trotzdem mappen musst:** alle übrigen Pflicht-Sensoren - Solcast-Prognose, SoC, Batteriekapazität, Hausverbrauch, PV/Einspeisung und Batterieleistung.
Ohne die läuft die Strategie gar nicht (siehe [Fail-safe-Verhalten](#testfälle--erwartetes-verhalten): fehlt `opti_soc` oder `opti_battery_capacity_kwh`, wird kein Modus gesetzt).

### Was ohne Preis-Sensoren aktiv bleibt

- **Intelligenter Ziel-SoC** (`sensor.opti_target_soc`): rein Solcast-Prognose- und Verbrauchs-basiert, enthält keine Preis-Referenz.
- **PV-Überschuss-Laden tagsüber:** greift, solange `input_boolean.opti_pv_ueberschuss_ladung` an ist (rein leistungsbasiert über die Überschuss-Binärsensoren).
- **MinSOC-Absicherung:** `SoC < input_number.minsoc` erzwingt „Akku nur Laden" - höchste Priorität, preisunabhängig.
- **Voller-Akku-Cleanup** und der Ziel-SoC-basierte Dynamisch/Entladen-Default.

### Was automatisch wegfällt

Fehlt `opti_price_current_ct_kwh` **oder** die Preisreihe (< 4 verwertbare Werte, ohne Halter-Cache vom heutigen Tag), wird `sensor.opti_price_level` über seinen Availability-Check `unavailable` (siehe [Abgeleitete Sensoren](#abgeleitete-sensoren-opti_derivedyaml)).
Damit greift **kein** Ladeblock mehr, der ein Preisniveau prüft:

- **Preis- und Winter-Ladeblöcke** (Entladesperre „Akku nur Laden" nach Preisniveau `VERY_CHEAP`/`CHEAP`/…): inaktiv, weil `opti_price_level` `unavailable` ist.
- **Entlade-Peak-Allokation (Leiter L1-L4):** inaktiv - dieselbe Ursache.
- **Echtes erzwungenes Netzladen** (Modus „Akku Netzladen", inkl. Vorladen unter Einspeisevergütung / bei Peak-Spread): fällt weg, weil alle diese Zweige zusätzlich `has_value('sensor.opti_price_current_ct_kwh')` voraussetzen.

> **Wichtige Klarstellung:** Die Preis-/Winter-Ladeblöcke setzen „Akku nur Laden" - das ist eine reine **Entladesperre**, kein Netzbezug.
> Echten Netzbezug gibt es nur im separaten Modus „Akku Netzladen", und der hängt exakt an den Zweigen, die ohne Preis-Sensor ohnehin wegfallen.

### Harte Garantie gegen jedes Netzladen

Willst du sicherstellen, dass die Steuerung **niemals** aus dem Netz lädt (etwa in der Testphase, bevor die Hardware dranhängt), lässt du diese Toggles bewusst aus:

- `input_boolean.opti_prognose_netzladen` - Gate für die prognosebasierten Netzladen-Zweige und die alten Reserve-Blöcke.
- `input_boolean.hausakku_aus_netz_laden` - manueller Booster, unabhängig davon: der zieht den Ziel-SoC sonst direkt auf `maxsoc`.
- `input_boolean.opti_balancing_netzladen` - eigener Schalter für den **Netz-Zweig des Balancing-Watchdogs** (`sensor.opti_balancing_watchdog` = `netz`). **Default aus** - ohne ihn balancet der Watchdog rein per PV (`pv`, tagsüber, zieht keinen Netzstrom). Nur wenn du ihn bewusst anschaltest, darf der Watchdog fürs BMS-Balancing auch nachts günstig/gratis aus dem Netz laden. Bewusst entkoppelt von `opti_prognose_netzladen`, damit du Balancing-Netzladen erlauben kannst, ohne das allgemeine Prognose-Netzladen zu öffnen (und umgekehrt).

### Vorher gefahrlos durchspielen

`sensor.opti_strategie_vorschau` zeigt dir für eine gegebene Situation, welchen Modus die Strategie wählen würde - ein guter Weg, das Setup ohne Akku-Zugriff zu testen.
Das Attribut `grund` desselben Sensors nennt den ausschlaggebenden Block.

---

## SMA-Rezept: Hausverbrauch (`opti_house_consumption_w`)

Die SMA-Integration liefert keinen direkten Hausverbrauchs-Sensor - er muss aus den
Metering- und Netz-Sensoren berechnet werden. Copy-Paste-Rezept für `opti_mapping.yaml`:

```yaml
# opti_mapping.yaml — Hausverbrauch aus SMA-Sensoren
state: >
  {{ (states('sensor.sn_XXXX_metering_power_absorbed') | float(0))
     + (states('sensor.sn_XXXX_grid_power') | float(0))
     - (states('sensor.sn_XXXX_metering_power_supplied') | float(0)) }}
```

`sn_XXXX` ist die Seriennummer deines SMA-Geräts, wie sie die Integration in die
Entity-IDs einbaut. Eigene Seriennummer ermitteln: Entwicklerwerkzeuge → Zustände →
nach `sn_` filtern, oder Einstellungen → Geräte & Dienste → SMA-Integration → Gerät
öffnen (Seriennummer steht dort im Geräte-Info-Block). Alle drei `sn_XXXX`-Platzhalter
oben mit derselben Seriennummer ersetzen.

---

## Solcast-Anbindung

| opti-Sensor | Typischer Solcast-Sensor |
|---|---|
| `opti_forecast_today_kwh` | `sensor.solcast_pv_forecast_forecast_today` |
| `opti_forecast_tomorrow_kwh` | `sensor.solcast_pv_forecast_forecast_tomorrow` |
| `opti_forecast_remaining_today_kwh` | `sensor.solcast_pv_forecast_forecast_remaining_today` |

**`estimate10`-Attribut (P10-Sicherheitsnetz):**
Die `opti_forecast_*_kwh`-Sensoren reichen das Attribut `estimate10` durch (10. Perzentil der Prognose - pessimistischer Worst-Case-Wert).
Der `opti_forecast_score` und der intelligente Ziel-SoC (`opti_target_soc`, siehe
[strategie-logik.md](strategie-logik.md#der-intelligente-ziel-soc--herzstück-der-akkuschonung))
nutzen diesen Wert als - per Default konservative - Referenz; der Optimismus-Grad ist einstellbar (siehe unten).
Beide lesen das P10 vom Rest-Tag-Sensor (`opti_forecast_remaining_today_kwh`), nicht vom Ganztags-Sensor:
das Ganztags-P10 enthält die bereits gelaufene Vormittagsproduktion und wäre ab dem Nachmittag praktisch immer größer als der Rest-Median - als Sicherheitsnetz damit wirkungslos.

**Kontrakt:** Das Attribut wird nur gesetzt, wenn die Quelle tatsächlich ein P10 liefert - fehlt es, bleibt `estimate10` weg (bzw. `none`), es wird NICHT auf `0` normalisiert.
Grund: `0` ist von "keine Schätzung vorhanden" nicht unterscheidbar, würde aber als echter P10-Wert von 0 kWh interpretiert und die Restproduktion fälschlich auf 0 drücken.

```yaml
# Im Mapping (Beispiel für heute):
attributes:
  estimate10: "{{ state_attr('sensor.solcast_pv_forecast_forecast_today', 'estimate10') if state_attr('sensor.solcast_pv_forecast_forecast_today', 'estimate10') is not none else none }}"
```

Auf der Konsumenten-Seite bündelt der zentrale Sensor `sensor.opti_forecast_effective_remaining_kwh` diese Logik an genau einer Stelle: `estimate10 <= 0` (oder komplett fehlend) gilt als "keine P10-Schätzung" und fällt auf den Median zurück, statt remaining auf 0 zu drücken.
`opti_forecast_score` und `opti_target_soc` lesen ausschließlich diesen Sensor (die `min(median, P10)`-Rechnung war früher an neun Stellen dupliziert).

**Einstellbarer Optimismus (`input_number.opti_forecast_optimismus`, 0-100 %):**
Der Sensor mischt Median und P10: `P_eff = min(median, α·median + (1-α)·P10)` mit α = Regler / 100.
α=0 (Default) = konservatives `min(median, P10)` wie bisher; α=50 = `(median+P10)/2`; α=100 = reiner Median.
Höheres α lässt die Ziel-SoC-Treppe später steigen (der Akku wird weniger früh voll gefahren) - sinnvoll in ertragssicheren Monaten/Regionen, in denen der reale Tagesertrag fast immer über P10 liegt.
Die äußere `min(median, …)`-Klammer sorgt dafür, dass der Wert nie optimistischer als der Median wird (greift nur beim seltenen Solcast-Fall P10 > Median).

---

## Mindest-HA-Version

Der Canonical-Layer verwendet das Template-Keyword `has_value()`, das bereits ab
**Home Assistant 2022.9** verfügbar ist. Das reicht für sich genommen aber nicht:
`packages/opti_derived.yaml` nutzt für die Peak-Reserve zusätzlich trigger-basierte
Template-Sensoren mit `variables:`, wofür **Home Assistant >= 2024.10** vorausgesetzt wird.
Mit älteren Versionen werden Availability-Checks als `false` ausgewertet bzw. die
trigger-basierten Sensoren laden gar nicht, und die betroffenen `opti_*`-Sensoren
erscheinen als `unavailable`.

**Empfehlung:** Aktuelle HA-Release-Version verwenden, mindestens 2025.1 (getestet mit 2026.6).
Falls `sensor.opti_soc` nach dem Einrichten auf `unavailable` bleibt, in den Entwicklerwerkzeugen → Template prüfen, ob `has_value('sensor.DEINE_QUELLE')` korrekt ausgewertet wird.

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
| `sensor.DEIN_GRID_IMPORT` (optional) | `sensor.sma_metering_power_absorbed` | `sensor.huawei_grid_consumption_power` |
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
| `sensor.opti_forecast_score` | PV-Fit heute (0–10); nutzt `estimate10` als P10-Sicherheitsnetz; nach dem heutigen Sonnenuntergang Fallback auf `opti_forecast_score_tomorrow`, falls verfügbar (sonst alte Formel) |
| `sensor.opti_forecast_score_tomorrow` | PV-Fit morgen (0–10) |
| `sensor.opti_forecast_effective_remaining_kwh` | Effektive Rest-Prognose (kWh): Blend aus Median und P10 über `input_number.opti_forecast_optimismus` (0–100 %, Default 0 = `min(median, P10)`). Einzige Quelle für Score und Ziel-SoC. |
| `sensor.opti_target_soc` | Ziel-SoC (%) basierend auf Restprognose und geglättetem Hausverbrauch |
| `sensor.opti_charge_power_w` | Dynamische Ladestärke (W) nach SoC-Stufe und Forecast-Score |
| `sensor.opti_price_series_stable` | Preisreihen-Halter (trigger-basiert): spiegelt `today`/`tomorrow` aus `opti_price_series` und hält bei einem Quellausfall die letzte gültige Reihe **desselben Kalendertags**. State = `frisch` / `gehalten` / `unsicher` / `leer`; `gehalten` deckt auch den Teilausfall ab, bei dem nur die Morgen-Liste wegfällt (Attribut `gehalten_teil` = `reihe` / `morgen` / `nichts`), `unsicher` = ausgeliefert, aber nicht haltbar. Einzige Preisreihen-Quelle für Preisniveau und Peak-Reserve |
| `sensor.opti_price_level` | Preisniveau-Enum (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE / VERY_EXPENSIVE); Midrank-Perzentil (Gleichstände zählen halb) - flache Preistage (viele identische Werte) landen dadurch bei NORMAL statt fälschlich bei VERY_EXPENSIVE. Fail-closed: < 4 verwertbare Preise → `unavailable` |
| `sensor.opti_mindestentladepreis_ct_kwh` | Mindestentladepreis = Ladepreis + Preisdifferenz (ct/kWh) |
| `sensor.opti_runtime_h` | Geschätzte Akku-Restlaufzeit (Stunden) |
| `binary_sensor.opti_winter_charging_allowed` | Saisonales Lade-Gate (Standard: `true`, fail-open) |
| `sensor.opti_peak_reserve_soc` | Reserve-SoC für kommende Preisspitzen (trigger-basiert, 36h-Horizont) |
| `binary_sensor.opti_peak_reserve_aktiv` | Gate: Peaks im Wiederauflade-Horizont vorhanden |
| `sensor.opti_balancing_watchdog` | Balancing-/Deep-Charge-Watchdog (`aus`/`pv`/`netz`): erzwingt einen Voll-Zyklus fürs BMS, wenn `counter.tage_seit_akku100` ≥ `input_number.opti_balancing_intervall_tage` (Default 14; 0 = aus). Staffelt PV (tagsüber) → Gratis-/Negativ-Netz → bezahltes Netz erst nach `opti_balancing_karenz_tage` und nur ≤ `opti_balancing_max_ct`. Beide `netz`-Zweige hängen am eigenen Schalter `input_boolean.opti_balancing_netzladen` (Default aus, PV ungegatet). Die Fälligkeit bleibt bis zu 30 bestätigten Minuten über dem Done-SoC aktiv; persistente Minuten-, Zeitstempel- und Gültigkeits-Helfer machen den Ablauf restartfest, unterscheiden HA-Erstwerte von echten Abschlüssen und begrenzen ihn auf einen Abschluss pro Tag. |

**Baustein `sensor.opti_house_consumption_60min_w` (`packages/sma_statistik.yaml`):**
Gleitender 60-Minuten-Mittelwert von `sensor.opti_house_consumption_w` (Legacy-Muster, `state_characteristic: mean`).
`opti_forecast_score`, `opti_forecast_score_tomorrow` und `opti_target_soc` lesen bevorzugt diesen geglätteten Wert statt des Momentanverbrauchs, damit kurze Lastspitzen (z. B. ein Wasserkocher) den Score nicht minütlich kippen lassen.
Fehlt der Statistik-Sensor noch (z. B. direkt nach einem HA-Neustart), fällt die Formel auf den Momentanwert zurück.
`sensor.opti_runtime_h` bleibt bewusst beim Momentanwert - die Restlaufzeit soll den aktuellen Verbrauchszug zeigen, keinen Mittelwert.

> **Warnung — `opti_winter_charging_allowed`:** Dieses Gate ist **fail-open** (`{{ true }}`).
> Solange kein realer Saisonal-Sensor gemappt ist, ist es immer `on`.
> Wenn du einen echten Winter-/Sommer-Sensor anschließt, beachte:
> Das Gate ist **ausschließlich** für die SOC<20- und SOC<80-Ladeblöcke vorgesehen.
> **Keinesfalls** darf es den SOC<15-Notfall-Ladeblock (Entladesperre) gaten — dieser muss auch
> im Sommer jederzeit eingreifen können (tiefe Entladung vermeiden).
> Die SOC<75- und SOC<45-Blöcke werden ebenfalls nicht gegated (opportunistische Blöcke,
> sollen ganzjährig greifen).

Diese Sensoren werden von `automations/opti_strategie.yaml` direkt konsumiert —
du beeinflusst sie ausschließlich über dein Mapping und die Helfer-Werte.

---

## KI-Analyse-Schicht (optional, Phase 1)

`packages/opti_ki_analyse.yaml` + `automations/opti_ki_analyse.yaml` erzeugen einen täglichen KI-Tagesreport (21:00) über `ai_task.generate_data`.
Die Schicht liest ausschließlich - der Regelkreis funktioniert ohne sie vollständig.
Datenzugriff läuft über die optionalen Mapping-Einträge (Abschnitt 12 der Example-Datei); fehlende Quellen werden im Report als "nicht verfügbar" markiert.
Ein deterministischer Watchdog (22:00) meldet, wenn die Analyse 3 Tage keinen Erfolg hatte.
Details: `docs/superpowers/specs/2026-07-10-ki-analyse-schicht-design.md` (lokal).

---

## Testfälle — Erwartetes Verhalten

| Szenario | Relevante Eingaben | Erwarteter Modus | Hinweise |
|---|---|---|---|
| **Nacht / Reserve halten (Entladesperre)** | `opti_forecast_score` ≤ 2, SoC < 30 %, `opti_price_level` CHEAP | **Akku nur Laden** | Gate `input_boolean.opti_prognose_netzladen` muss `on` sein |
| **Nacht / kein Ladegrund** | Score ≤ 2, SoC > 60 %, Preis EXPENSIVE | **Akku Dynamisch** (Default) | Kein Ladeblock greift → Default-Pfad |
| **Sonne / PV-Überschuss** | `binary_sensor.opti_ueberschuss_70_aktiv` = on (Export + Batterieleistung > 70%-Grenze, 30 s entprellt, 1 kW Hysterese), SoC < 100 % | **Akku Dynamisch** | Gate `input_boolean.opti_pv_ueberschuss_ladung` muss `on` sein |
| **Tagsüber unter Ziel-SoC** | SoC < `opti_target_soc` **− 3 %**, nach Sonnenaufgang | **Akku Dynamisch** | Option „Dynamisch laden wenn SOC < ZielSoC"; das ±3 %-Band verhindert Modus-Pendeln direkt an der Ziel-Kante — siehe [strategie-logik.md](strategie-logik.md#der-intelligente-ziel-soc--herzstück-der-akkuschonung) |
| **Entladen über Ziel-SoC** | SoC > `opti_target_soc` **+ 3 %** | **Akku nur Entladen** | Option „Nur Entladen wenn SOC > DynZielSoC" |
| **MinSOC-Schutz** | SoC < `input_number.minsoc` | **Akku nur Laden** | Höchste Priorität, überstimmt alle anderen Blöcke |
| **Preisreihe fällt kurz aus** | Quelle liefert `today`/`tomorrow` leer (z. B. Provider-Timeout) | `sensor.opti_price_series_stable` **hält** die letzte gültige Reihe desselben Kalendertags (State `gehalten`); Preisniveau und Peak-Leiter arbeiten unverändert weiter | Zulässig, weil die Tagesreihe ein Fahrplan ist und sich innerhalb des Tages nicht ändert - siehe [strategie-logik.md](strategie-logik.md#fail-safes-und-bekannte-grenzen) |
| **Preisreihe fehlt ganz** | < 4 verwertbare Werte und kein Halter-Cache vom heutigen Tag (auch: Ausfall über Mitternacht) | Preisniveau wird **`unavailable`**, `sensor.opti_peak_reserve_soc` ebenfalls | Fail-closed: alle preisabhängigen Zweige (Preis-/Winterladeblöcke, Peak-Leiter L1-L4) sind inaktiv. Kein `NORMAL`-Fallback mehr - der täuschte der Strategie bei einem Datenausfall ein gültiges Mittelpreis-Signal vor |
| **Forecast fehlt** | `opti_forecast_remaining_today_kwh` → `unavailable` | Prognose-Blöcke inaktiv; MinSOC-Schutz und Cleanup laufen weiter | Default → **Akku Dynamisch** (nur wenn `opti_soc` + `opti_battery_capacity_kwh` verfügbar) |
| **Voller Akku** | SoC > 99 % | **Akku Dynamisch**; Lade-Booster (Legacy) wird deaktiviert | Option „Bei vollem Akku auf Dynamisch" + Cleanup-Block |
| **Balancing fällig, tagsüber** | `counter.tage_seit_akku100` ≥ `opti_balancing_intervall_tage`, Tag | **Akku nur Laden** | `sensor.opti_balancing_watchdog` = `pv`; bleibt bis zum bestätigten Abschluss aktiv und steht hinter der Peak-Leiter L1/L2, vor den Prognoseblöcken — siehe [strategie-logik.md](strategie-logik.md#balancing-deep-charge-watchdog) |
| **Balancing fällig, nachts günstig** | wie oben, Nacht, `opti_balancing_netzladen` = on, Preis gratis/negativ oder (nach Karenz) VERY_CHEAP/CHEAP ≤ `opti_balancing_max_ct` | **Akku Netzladen** | `sensor.opti_balancing_watchdog` = `netz`; eigener Schalter `opti_balancing_netzladen` off → `aus` (kein Netzladen, Default); `opti_balancing_max_ct` = 0 lässt den bezahlten Fallback aus (fail-safe) |

**Fail-safe-Verhalten:** Die Hauptstrategie entscheidet nur mit numerisch plausiblem
`sensor.opti_soc` und positiver `sensor.opti_battery_capacity_kwh`. Werden diese Kerndaten
im Betrieb länger als 10 Sekunden ungültig, setzt der separate Safety-Layer
**Akku Pause**; bei ausgeschalteter Automatik oder ungültigen Startdaten geschieht das
sofort. **Akku Automatisch** wird nie als Fail-safe verwendet.

---

## Bekannte Lücken: Legacy-Sensoren (`sma_templates.yaml`) vs. Canonical-Layer

`packages/sma_templates.yaml` ist der Vorläufer von `packages/opti_derived.yaml` (vor dem
Canonical-Layer-Umbau) und läuft auf produktiven Installationen teils **parallel** zu den neuen
`opti_*`-Sensoren weiter — er wurde nie aufgeräumt. Beim Abgleich von Dashboards gegen den neuen
Stand (2026-07) hat sich gezeigt, dass nicht jeder alte Sensor ein sauberes `opti_*`-Äquivalent
hat. Bereits durchgeführte Dashboard-Swaps (sicher, geprüft):

| Alt (`sma_templates.yaml`) | Neu (`opti_derived.yaml`) | Status |
|---|---|---|
| `sensor.akkusteuerung_dynamische_ladestaerke` | `sensor.opti_charge_power_w` | Swap OK — SoC-/Temp-Staffelung 1:1 übernommen, nur die Tages-Güte-Klassifikation wurde bewusst von 5 Text-Kategorien auf 3 Score-Bänder umgebaut (nicht byte-identisch bei jedem Prognosewert, aber gleiche Kennlinie) |
| `sensor.ueberschuss_pv_watt` | `binary_sensor.opti_ueberschuss_70_aktiv` | Nachkorrektur 2026-07-03: der erste Swap auf `opti_grid_export_w` war eine Regression. Das Alt-Signal war PV minus Haus (akkuunabhängig), der Export ist über den Akku rückgekoppelt (Laden drückt Export unter die Grenze -> Modus-Flattern, live beobachtet). Jetzt: Export + Batterieleistung (= Export ohne Akku-Eingriff) + 30-s-Entprellung + Hysterese im Binärsensor |
| `sensor.pv_forecast_bewertung_heute` | `sensor.opti_forecast_score` | Swap vertretbar — andere Formel (PV-Fit statt Kapazitäts-Multiples), aber `opti_forecast_score` ist der Sensor, den die Strategie tatsächlich konsumiert |
| `sensor.akku_net_verfugbare_energie` | Attribut `net_available_kwh` von `sensor.opti_target_soc` | Swap OK — Formel äquivalent |

**Noch offen (bewusst NICHT geswapt, brauchen eine Entscheidung):**

- **`sensor.house_battery_runtime_raw` → `sensor.opti_runtime_h`:** unterschiedlicher Nenner —
  Alt teilt durch `sensor.house_battery_load_30_mins` (gemessene Akku-Entladeleistung), Neu durch
  `sensor.opti_house_consumption_w` (Hausverbrauch). Bei Netzbezug oder wenn Hausverbrauch ≠
  Akku-Entladeleistung (z. B. gleichzeitige PV-Einspeisung) weichen beide Werte ab. Noch nicht an
  einem Zeitpunkt mit tatsächlicher Akku-Entladung gegengecheckt.
- **`sensor.akku_remaining_sun_hours` (Live-Name `sensor.verbleibende_sonnenstunden`) → Attribut
  `remaining_hours` von `sensor.opti_target_soc`:** **nicht äquivalent** — Alt clampt auf
  `[0, 12]` mit Fallback `0` (nachts also `0`), Neu clampt auf `[0.5, 12]` mit Fallback `6.0`
  (nachts also `6.0`). Ein blinder Swap hätte nachts eine sichtbar falsche Anzeige erzeugt.
- **`sensor.pv_forecast_bewertung_morgen` → `sensor.opti_forecast_score_tomorrow`:** siehe
  eigener Abschnitt unten — unterschiedliche Formel UND steuerungsrelevant, keine reine
  Anzeigefrage.

## Inkonsistenz: `opti_forecast_score` vs. `opti_forecast_score_tomorrow`

`opti_forecast_score` (heute, `packages/opti_derived.yaml` Sensor 1) nutzt einen "PV-Fit"-Ansatz:
PV-Überschuss (Restprognose minus Hausverbrauch bis Sonnenuntergang) wird gegen die tatsächlich
fehlende Energie bis Akku voll (`cap * (1 - soc/100)`) verglichen — SoC- und Sonnenstunden-bewusst.

`opti_forecast_score_tomorrow` (Sensor 2) nutzt eine deutlich simplere Formel: Verhältnis
morgiger Gesamtprognose zu einem geschätzten vollen Verbrauchstag
(`hausverbrauch_w * 24h`) — ohne SoC-Bezug, ohne Sonnenstunden. Beide liefern zwar denselben
Wertebereich (0–10), sind aber konzeptuell unterschiedliche Metriken (nicht nur unterschiedlich
skaliert). Grund: die PV-Fit-Formel von "heute" braucht aktuellen SoC und Rest-Sonnenstunden,
die für "morgen" schlicht noch nicht feststehen.

**Praktische Auswirkung:** `opti_forecast_score_tomorrow` fließt live in Strategie-Bedingungen
ein (`st < 3` in mehreren choose-Optionen, `automations/opti_strategie.yaml` bzw. der
Sollmodus-Vorschau in `packages/opti_derived.yaml`). Eine Formel-Angleichung an "heute" wäre
also eine **Steuerungsänderung**, keine reine Aufräumarbeit — braucht eine bewusste, separate
Entscheidung (nicht nebenbei mitziehen).

---

## Deployment als HA-Package & der „Migrieren"-Hinweis

Sensoren und Helfer werden als **HA-Packages** geladen
(`packages: !include_dir_named packages/` unter `homeassistant:`). Das hält alles
versioniert und an einem Ort.

Die Strategie-Automation (`automations/opti_strategie.yaml`) liegt dagegen als
Top-Level-Liste im Format von `automations.yaml` vor - sie ist **kein** fertiges Package.
Zwei Wege, sie einzuspielen: an `automations.yaml` anhängen, oder nach `packages/`
kopieren und mit dem Schlüssel `automation:` wrappen (Details in der README, Abschnitt
„Schnell-Nachbau über Packages", Schritt 6). Nur im zweiten Fall - Package-Variante -
gilt der folgende „Migrieren"-Hinweis.

Eine als Package geladene Automation zeigt im UI-Automationseditor die Warnung:

> ⚠️ *„Diese Automation kann nicht über die Benutzeroberfläche bearbeitet werden,
> da sie nicht in der Datei automations.yaml gespeichert ist oder keine ID hat."*
> — mit einem **„Migrieren"**-Button.

**Das ist normal und harmlos.** Die Automation läuft einwandfrei (sie *hat* eine
`id`; die Meldung heißt nur „liegt nicht in `automations.yaml`"). **Ansehen und
`Traces` funktionieren** — nur der *visuelle* Editor ist für Package-Automationen
schreibgeschützt.

**NICHT auf „Migrieren" klicken** — außer du willst die Automation **bewusst** aus
dem Package herauslösen und vom Git-/Package-Stand entkoppeln, um sie künftig nur
noch in der UI zu pflegen. „Migrieren" kopiert sie nach `.storage`; solange das
Package sie weiter lädt, entsteht ein **Duplikat (zwei Automationen mit derselben
`id`)**. Vorgesehener Weg: **Package-Datei bearbeiten** + HA neu laden/neu starten.
