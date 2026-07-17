# BYD-Monitoring nativ: Zelldaten und Akku-Alarme über Modbus

Dieses optionale Modul liest die BMU/BMS-Daten einer BYD Battery-Box Premium HVS **direkt per Modbus** aus und baut darauf deterministische Gesundheits-Alarme.
Es ist rein beobachtend/alarmierend und hat keinerlei Steuerfunktion.
Das zugehörige HA-Package ist [`packages/byd_monitoring.yaml`](../packages/byd_monitoring.yaml), die Tests liegen in [`tests/test_byd_monitoring.py`](../tests/test_byd_monitoring.py).

Es löst den bisherigen Weg über das Tool `bydlogc` und MQTT ab ([byd-bmu-monitoring.md](byd-bmu-monitoring.md), Package jetzt unter `legacy/`).
Gründe für den Wechsel: keine Binary-Redistribution mehr nötig, kein MQTT-Broker im Pfad, atomare Reads statt ~60 s versetzter Einzel-Topics, echte Pro-Zell-Daten (160 Zellen) und native Fehlerbits.

## Architektur

```
BYD Battery-Box (BMU, WLAN-Modul, Standard-IP 192.168.16.254)
        │  Modbus TCP, NUR EINE Verbindung
        ▼
HACS-Integration byd_battery_box (läuft in HA selbst)
        │
        ▼
packages/byd_monitoring.yaml  →  Template-Layer + Alarme + Watchdog
```

Der **Netzweg zur Box bleibt identisch** zum alten Setup: statische Route oder SNAT-Regel auf `192.168.16.254/32`.
Die Hairpin-Falle (UniFi verwirft nach dem ICMP-Redirect alles außer dem ersten Paket) ist unverändert relevant und in [byd-bmu-monitoring.md](byd-bmu-monitoring.md#netzwerk-die-hairpin-falle) beschrieben.
Ebenso gilt weiter das **Ein-Verbindungs-Limit** der BMU: Be-Connect-App oder BYD-Logger nie parallel laufen lassen.

## Gepinnter Entity-Vertrag

Die Integration liegt **nicht** in diesem Repo, ihre Entity-Namen sind aber die Schnittstelle dieses Packages.
Deshalb ist der Vertrag hier festgeschrieben - weicht eine neue Integrations-Version davon ab, ist das ein Breaking Change für `byd_monitoring.yaml`.

- **Integration:** [`TimWeyand/byd_battery_box`](https://github.com/TimWeyand/byd_battery_box) (Fork), **v0.1.34, Commit `2cc5762`**.
- **Anlage im Snapshot:** 1 Turm, 5 Module, 32 Zellen/Modul = 160 Zellen, 12 Temperaturfühler/Modul, Wechselrichter „SMA STP 5.0-10.0 SE HV".

### Zwei Datenebenen

| Ebene | Entities | Kadenz | Auflösung |
|---|---|---|---|
| **BMU-Poll** | `sensor.bmu_cell_voltage_max`, `sensor.battery_management_unit_bmu_cell_voltage_min`, `..._bmu_cell_temperature_max`/`_min`, `sensor.bmu_power`, `..._state_of_charge`, `..._errors`, `..._connection_health`, `..._updated` | **30 s** | Spannung 10 mV, Temperatur 1 °C, Leistung ~2 W |
| **BMS-Detail** | `sensor.battery_management_system_1_bms_1_*` (`cell_voltage_max`/`_min`, `cells_voltage_delta`, `warnings`, `errors`, `updated`), `sensor.bms_1_cells_average_voltage` (Attribut `cell_voltages`), `sensor.bms_1_cells_average_temperature` (Attribut `cell_temps`), `sensor.bms_1_cells_balancing`, `sensor.bms_1_state_of_health` | **~630 s** (10,5 min) | Spannung 1 mV |

### Namens-Falle (nicht „aufräumen")

Die Integration vergibt die entity_ids **asymmetrisch** - max und min heißen nicht gleich aufgebaut:

| Zweck | entity_id |
|---|---|
| Zellspannung max (BMU) | `sensor.bmu_cell_voltage_max` (kurz) |
| Zellspannung min (BMU) | `sensor.battery_management_unit_bmu_cell_voltage_min` (lang) |
| Leistung | `sensor.bmu_power` (kurz) |
| SoC | `sensor.battery_management_unit_state_of_charge` (lang) |
| Zellspannungs-Delta | `sensor.battery_management_system_1_bms_1_cells_voltage_delta` |
| Balancing-Zähler | `sensor.bms_1_cells_balancing` |

Alle im Package verwendeten IDs sind am 17.7.2026 live gegengeprüft.

### Attribut-Formate

- `cell_voltages` (auf `sensor.bms_1_cells_average_voltage`): `[{"m": 1..5, "v": [32 Ints in mV]}, ...]` - 160 Werte.
- `cell_temps` (auf `sensor.bms_1_cells_average_temperature`): `[{"m": 1..5, "t": [12 Ints in °C]}, ...]`.
- `cell_balancing` (auf `sensor.bms_1_cells_balancing`): `[{"m": 1..5, "b": [16 Bit]}, ...]` - 16 Bit auf 32 Zellen, die Zuordnung ist unklar, deshalb wird nur der Zählwert genutzt.

### Vorzeichen und Timestamps (live verifiziert 17.7.)

- **`sensor.bmu_power`: negativ = Laden.** Gleiche Konvention wie das alte `sensor.byd_leistung`, und identisch zu `sensor.bms_1_charge_total_energy`/`_discharge_total_energy` (relevant für Phase 2).
- Die `*_updated`-Sensoren liefern einen **naiven Lokalzeit-String** (`"2026-07-17 11:45:34.207325"`, `tzinfo` = None).
  `as_timestamp(states('...'), 0)` interpretiert ihn korrekt als Lokalzeit, also ist `now().timestamp() - as_timestamp(states('...'), 0)` das Alter in Sekunden (gegengeprüft: 17,5 s bei 30-s-Poll, 162 s beim BMS).
  Bei Müll oder `unavailable` liefert `as_timestamp(x, 0)` den Default `0` → Alter riesig → „nicht frisch".
  Das ist die gewünschte Fail-Safe-Richtung; eine `as_datetime`-/TZ-Behandlung ist ausdrücklich **nicht** nötig.

### Auflösung der BMU-Ebene (live verifiziert 17.7.)

Das BMU-Register ist **nativ 10-mV-granular**: die Integration rechnet `round(uint16 * 0.01, 2)` (`bydboxclient.py:456`), der `round()` ist also nur kosmetisch.
Die BMS-Detailebene ist mV-granular (`round(int16 * 0.001, 3)`, Zeile 520).

Ob die BYD-Firmware beim Füllen des 10-mV-Registers **rundet oder abschneidet**, ist von außen **nicht bestimmbar**.
Konsequenz für die Alarme: der effektive Auslösepunkt der BMU-Regeln verschiebt sich um **bis zu 10 mV nach oben** (die 3,55-V-Regel feuert praktisch ab 3,56 V).
Bei ~100 mV Abstand zur BMS-Grenze ist das akzeptabel; das kritische Fenster 3,651-3,659 V fängt die präzise BMS-Backstop-Regel ab (siehe Alarm-Tabelle).

### Bekannte Integrations-Mängel (bewusst gemieden)

- `average_latency`/`last_latency`: **Einheiten-Bug** - Sekunden gemessen, als ms deklariert. Nicht für Alarme verwenden.
- `cell_voltages_max_history`/`_min_history` und `sensor.bms_1_max/min_history_cell_voltage`: zeigen live überlappende, zwischen den Modulen verschobene Wertereihen (Sliding-Window-Artefakt). **Nicht verwenden.**
- `sensor.battery_management_unit_cells_per_module` (12) und `..._temperature_sensors_per_module` (32) sind offensichtlich **vertauscht** - die echten Attribute zeigen 32 Zellen und 12 Fühler pro Modul. Nur Anzeige, ohne Wirkung auf dieses Package.
- **Setup-Fehlschlag nach HA-Neustart ohne Retry** (Fork-Issues #14/#15): die Entities starten dann `unavailable` **ohne State-Übergang**, edge-Trigger feuern nie. Das ist das realste Ausfallszenario und der Grund für den zeitbasierten Dead-Man-Watchdog.
- `connection_health` kennt nur `healthy`/`unhealthy` (unhealthy ab 3 aufeinanderfolgenden Ping-Fehlern oder Latenz ≥ 5 s; eigener Ping alle 60 s auf Register `0x0000`). Der Ping ist **unabhängig** vom BMU-/BMS-Poll: er kann gesund sein, während ein Poll hängt. Deshalb reicht er als Ausfall-Erkennung nicht aus.

## Die zentrale Design-Randbedingung: Werte frieren ein

**Die Integration setzt Entities bei Modbus-Abbruch NIE auf `unavailable`** - `sensor.py` hat keine `available`-Property, `hub.data` friert einfach ein.

Daraus folgt beides:

1. Der alte `stale`-Alarm (Trigger auf den `unavailable`-Übergang der MQTT-Sensoren) hat **kein Äquivalent** - es gibt keinen Übergang, auf den man horchen könnte.
2. Ein eingefrorener Wert **erfüllt jede `for:`-Haltezeit per Stagnation**. Ein bei 3,66 V eingefrorener Sensor würde den Grenzwächter auslösen, obwohl seit Stunden nichts gemessen wurde.

Antwort darauf ist dreiteilig:

- **Frische-Binaries** als Bedingung in jedem physikalischen Alarm (`for:` allein beweist keine frischen Samples).
- **Edge-Alarm `verbindung`** für Modbus-/Netzprobleme im laufenden Betrieb.
- **Dead-Man-Watchdog**, zeitbasiert - erkennt auch den Boot-Setup-Fehlschlag ohne State-Übergang.

Bewusste Konsequenz: die Frische-Bedingung **unterdrückt physikalische Alarme während eines Datenausfalls**.
Dann weiß man ohnehin nichts Neues - und `verbindung`/Watchdog melden den Ausfall selbst (fail-notified statt fail-silent).

## Abgeleitete Entities (Template-Layer)

| Entity | unique_id | Quelle | Zweck |
|---|---|---|---|
| `binary_sensor.byd_ruhefenster` | `byd_ruhefenster` | \|`bmu_power`\| < 300 W UND 25 < SoC < 85 | Gate für die Ruhe-Sensoren |
| `binary_sensor.byd_bmu_frisch` | `byd_bmu_frisch` | Alter von `..._updated` < 90 s (= 3 verpasste Polls) | Frische-Bedingung BMU-Alarme, Watchdog |
| `binary_sensor.byd_zelldaten_frisch` | `byd_zelldaten_frisch` | Alter von `..._bms_1_updated` < 15 min (900 s; schmaler als die 21-min-Haltezeit der Präzisionsregel, breiter als der 630-s-Zyklus) | Frische-Bedingung BMS-Regeln, Ruhe-Sensoren, Watchdog |
| `binary_sensor.byd_balancing_aktiv_nativ` | `byd_balancing_aktiv_nativ` | `bms_1_cells_balancing` > 0 | history_stats-Quelle der KI-Analyse |
| `sensor.byd_zellspreizung_ruhe` | `byd_bmu_zellspreizung_ruhe_mv` (**Carry**) | Median-3 des nativen `cells_voltage_delta`, im Ruhefenster | **Bedarfs-Balancing (Steuerwirkung!)**, KI-Analyse, Alarm |
| `sensor.byd_temperatur_spreizung` | `byd_bmu_temp_spreizung_k` (**Carry**) | BMU-Temp max - min (ein Poll) | KI-Analyse, `temp_delta`-Alarm |
| `sensor.byd_zell_ausreisser` | `byd_zell_ausreisser` | max \|Zelle - Median(160)\| aus `cell_voltages` | Diagnose, **kein** Alarm |

### unique_id-Carry: warum die entity_ids gleich bleiben

`byd_bmu_zellspreizung_ruhe_mv` und `byd_bmu_temp_spreizung_k` sind aus dem alten Package **übernommen**, nicht neu vergeben.
Beide bleiben auf derselben Plattform (`template`), damit behalten `sensor.byd_zellspreizung_ruhe` und `sensor.byd_temperatur_spreizung` ihre entity_id, ihre Registry-Einträge und ihre Historie.

Das ist bewusst so und **nicht** kosmetisch: `sensor.byd_zellspreizung_ruhe` wird in [`packages/opti_derived.yaml`](../packages/opti_derived.yaml) **viermal mit Steuerwirkung** gelesen (Bedarfs-Balancing kann eine Vollladung vorziehen).
Durch den Carry musste dort **keine Zeile** geändert werden.

> **Zäsur 17.7.2026 in der Zeitreihe von `sensor.byd_zellspreizung_ruhe`:**
> Die Quelle wechselt von MQTT (`byd_zellspreizung`, aus versetzten Min/Max-Topics gerechnet) auf das native `cells_voltage_delta` (atomarer Read), und das Median-Fenster von 5 auf 3.
> Die physikalische Größe (Ruhe-Zellspreizung in mV) bleibt dieselbe, deshalb wiegt die Kontinuität für Trends und Konsumenten schwerer als die formale Zäsur.
> Beim Lesen von Wochen-Trends über dieses Datum hinweg den Bruch mitdenken.

### Ruhe-Spreizung: warum überhaupt noch ein Median

Die **alte** Begründung ist mit Modbus obsolet: die Min/Max-Topics kamen ~60 s versetzt an, ein Einzelwert konnte dadurch verzerrt oder sogar negativ sein.
Native Reads sind atomar - alle 160 Zellwerte stammen aus einem Detail-Read, das Delta wird integrationsseitig auf demselben Datensatz gerechnet.

Der Median bleibt trotzdem, mit **neuer** Begründung: `sensor.byd_zellspreizung_ruhe` hat Steuerwirkung, und der Alarm hält gelatchte Werte über Stunden.
Ein einzelnes Sample soll weder eine Vollladung noch einen 1-h-Alarm allein tragen.
Fenster **3** statt 5 - klein genug, um schnell zu reagieren, groß genug gegen Einzel-Ausreißer.

Gating (gegen Vor-Ruhe-Samples gehärtet):

- **Trigger:** State-Änderung von `..._bms_1_updated` (feuert jeden Detail-Zyklus, auch wenn das Delta numerisch gleich bleibt) und von `..._cells_voltage_delta`.
- **Bedingung:** `binary_sensor.byd_ruhefenster` seit **≥ 10 min** an, plus `byd_zelldaten_frisch`.
  Damit ist jedes übernommene Sample innerhalb des Ruhefensters gemessen; Relaxations-Transienten nach Lastende sind draußen, und ein Latch des letzten Lastphasen-Werts ist ausgeschlossen.

Das SoC-Gate 25-85 % bleibt unverändert (LFP-Knie-Inflation, unabhängig vom Skew-Thema).

> **Restrisiko (dokumentiert):** Die Schreib-Reihenfolge von `updated` vs. `delta` innerhalb eines Poll-Zyklus ist nicht garantiert.
> Im schlimmsten Fall geht ein Sample mit dem Wert des Vorzyklus ein - genau solche Einzelfälle kappt der 3er-Median.

### Zell-Ausreißer (Diagnose)

`sensor.byd_zell_ausreisser` liefert die größte absolute Abweichung einer Einzelzelle vom Median aller 160 Zellen, mit den Attributen `modul` (1-5), `zelle` (**1-32 innerhalb des Moduls**, nicht die globale 1-160-Zählung der nativen `*_number`-Sensoren), `richtung` (`hoch`/`tief`/`keiner`) und `median_mv`.

Das ist die Zell-Identifikation, die dem Spreizungs-Alarm fehlt, plus Langzeit-Trend pro Zelle.
**Kein eigener Push-Alarm:** `ruhe_spreizung` meldet physikalisch dieselbe Anomalie, ein zweiter Alarm auf derselben Ursache wäre Doppel-Rauschen.
Revisit nach Wochen: treibt immer dieselbe Zelle das Delta, kann eine gezielte Einzelzell-Regel den Spreizungs-Alarm **ersetzen** statt ergänzen.

## Alarm-Tabelle

Automation `byd_akku_alarme_nativ` (`mode: queued`, `max: 5`).
Spalte „Frische" = zusätzliche Bedingung auf das jeweilige Frische-Binary.

| Regel-ID | Entity | Schwelle | `for:` | Gating | Frische | Severity |
|---|---|---|---|---|---|---|
| `zell_hoch` | `sensor.bmu_cell_voltage_max` | > 3,55 V (eff. ab 3,56) | 2 min | SoC < 90 % | bmu | Push |
| `zell_kritisch` | `sensor.bmu_cell_voltage_max` | > 3,60 V | 30 s | SoC < 90 % | bmu | Critical |
| `zell_bms_grenze` | `sensor.bmu_cell_voltage_max` | > 3,65 V | 5 min | - | bmu | Critical |
| `zell_bms_grenze_praezise` | `..._bms_1_cell_voltage_max` | > 3,65 V (mV-genau) | 21 min | - | zelldaten | Critical |
| `zell_niedrig` | `..._bmu_cell_voltage_min` | < 2,90 V | 5 min | - | bmu | Push |
| `temp_hoch` | `..._bmu_cell_temperature_max` | > 45 °C | 5 min | - | bmu | Push |
| `temp_kritisch` | `..._bmu_cell_temperature_max` | > 50 °C | 1 min | - | bmu | Critical |
| `temp_delta` | `sensor.byd_temperatur_spreizung` | > 10 K | 15 min | - | bmu | Push |
| `ruhe_spreizung` | `sensor.byd_zellspreizung_ruhe` | > 50 mV (Median-3) | 1 h | im Sensor | im Sensor | Push |
| `bms_warnung` | `..._bms_1_warnings` | ≠ Normal/unknown/unavailable | 0 | - | - | **Schattenbetrieb** (nur UI) |
| `bms_fehler` | `..._bms_1_errors` | ≠ Normal/unknown/unavailable | 0 | - | - | Critical |
| `bmu_fehler` | `..._battery_management_unit_errors` | ≠ Normal/unknown/unavailable | 0 | - | - | Critical |
| `verbindung` | `..._connection_health` | → `unhealthy` | 10 min | - | - | Push |

Jeder Alarm stößt wie bisher `script.ki_alarm_kontext` an (`continue_on_error`, existiert nur live).

### Push-Kanäle: kein iOS-Critical-Override (bewusster Trade-off, 17.7.)

Die Spalte "Critical" in der Tabelle meint nur den **Titel `🔋🚨`** zur schnellen Triage - alle Alarme senden einen **normalen Push** (`notify.mobile_app_iphone_15_ben`), der Stumm/Fokus/Nachtruhe respektiert. Der frühere iOS-Critical-Override (`sound.critical: 1`, durchdringt DND) wurde auf Nutzerwunsch entfernt: aus der Ferne bzw. nachts ist bei einer Zell-Eskalation ohnehin nichts zu tun, und das BMS kappt selbst bei ~3,65 V vor jeder Handlungsmöglichkeit.

**Bewusster Rest-Trade-off:** Der eine Fall, in dem `zell_bms_grenze` wirklich zählt (BMS kappt NICHT trotz > 3,65 V), tritt bevorzugt nachts am Ladeschluss auf - ohne Critical-Ton kann er in DND untergehen. Falls je gewünscht, lässt sich ein Override per Regel gezielt für genau `zell_bms_grenze`/`bms_fehler`/`bmu_fehler` wieder ergänzen.

### SoC-Gating der Zellspannungs-Alarme

Am Ladeschluss (SoC ≥ 90 %, LFP-Knie, Balancing aktiv) laufen Max-Zellspannung und Spreizung kurzzeitig hoch - beobachtet: **3,659 V Peak bei 0 W Ladeleistung**.
Das ist top-of-charge-normal und würde sonst bei **jeder** Vollladung feuern (der Balancing-Watchdog erzwingt regelmäßige Vollladungen).
Deshalb gelten `zell_hoch`/`zell_kritisch` nur bei SoC < 90 %; darüber alarmiert nur der Grenzwächter.

### Zweistufiger Grenzwächter

Die 10-mV-Auflösung der schnellen Ebene lässt ein Fenster offen:

- **`zell_bms_grenze`** (BMU, 30 s, `for: 5 min`): fängt eindeutige Überschreitungen ab 3,66 V in Minuten.
- **`zell_bms_grenze_praezise`** (BMS, 1 mV, `for: 21 min` = 2 bestätigende Detail-Zyklen): fängt anhaltendes **3,651-3,659 V**, das die 10-mV-Ebene gar nicht sieht.

Der normale Kalibrier-Peak (3,659 V, kurz) übersteht **beide** Haltezeiten nicht - die Kalibrierung bleibt ungestört.

### Native warnings/errors

Das BMS meldet dekodierten Klartext (`"Normal"`, wenn nichts anliegt); unbekannte Bits erscheinen als `bit N undefined` und alarmieren mit (fail-visible, 255-Zeichen-Kappung).
Bewusst **kein `from:`-Filter**: nach einem Restart (`unknown` → aktiver Fehler) muss es feuern.
Jede String-Änderung (`"A"` → `"A,B"`) feuert erneut - gewollt.

**`bms_warnung` läuft in der Kennenlern-Phase im Schattenbetrieb:** nur `persistent_notification` in der HA-UI, **kein** Mobile-Push.
Ob das BMS am Ladeschluss/Balancing routinemäßig Warn-Bits setzt (z. B. „Cells imbalance"), ist unbekannt.
Erst nach **mindestens einer sauberen Vollladung ohne Warn-Rauschen** wird der Push aktiviert - im Package als TODO markiert (`bms_warnung` aus der Schatten-Option entfernen, dann fällt die Regel in den default-Push-Zweig).

Bei `errors` (Hardware-Fehlerbits wie „Main relay failure", „Cells failure") ist ein seltener Fehlalarm das kleinere Übel → sofort Critical.

### Dead-Man-Watchdog

Separate Automation `byd_daten_watchdog`: `time_pattern` alle 30 min, meldet wenn `byd_bmu_frisch` off **oder** `byd_zelldaten_frisch` off **oder** `connection_health` ≠ `healthy` (inkl. unavailable/unknown).
Drossel: höchstens alle 6 h (Reminder statt Spam, `last_triggered`-basiert; nie gelaufen → `as_timestamp(none, 0)` = 0 → meldet, fail-safe).

Zeitbasiert statt edge-basiert, weil er genau die zwei Fälle abdecken muss, die keinen State-Übergang erzeugen: eingefrorene Werte und den Boot-Setup-Fehlschlag.

`consecutive_failures` wird nicht separat alarmiert (redundant zu `connection_health`), Latenz-Sensoren sind tabu (Einheiten-Bug).

## Bekannte Restrisiken

- **`numeric_state`-Nachfeuer-Schwäche:** Ist die Schwelle bereits überschritten und wird erst danach das Gate wahr (oder lädt HA neu, wodurch `for:`-Timer verworfen werden), feuert der Trigger nicht nach.
  War im alten Design identisch, bleibt ein akzeptiertes Restrisiko.
  Option für später: den Watchdog um Schwellen-Checks erweitern.
- **Watchdog-Latenz:** Detail-Staleness wird schlimmstenfalls erst nach ~30-45 min gemeldet (15-min-Fenster + 30-min-Raster).
  Für reine Zelldaten akzeptabel, weil alle zeitkritischen Alarme auf der BMU-Ebene mit 3-min-Frische laufen.
- **Modul-Identifikation im Temperatur-Alarm** ist best-effort aus `cell_temps` und damit bis zu 10,5 min alt (im Alarmtext gekennzeichnet).
  Bewusster Trade-off für 30-s-Alarm-Latenz statt 10,5 min.
- **BMU- vs. BMS-SoC** können ~1 % divergieren; für die 90-%- und 25-85-%-Gates egal.
- Die Integration ist ein **0.1.x-Fork mit 3 Stars** - das realste Ausfallszenario ist der Setup-Fehlschlag nach HA-Neustart ohne Retry (gemeldet als Fork-Issue #14, `ConfigEntryNotReady`). Genau dafür ist der Dead-Man-Watchdog da.
- **HACS-Update-Disziplin (wichtig):** Der cells/temps-Fix (Fork-Issue #16 / PR #17) ist derzeit nur **lokal** in `/config/custom_components/byd_battery_box/bydboxclient.py` gepatcht (Backup `.bak-cellswap-20260717`). Ein HACS-Update auf `byd_battery_box` **überschreibt den Patch** und stellt die Vertauschung wieder her, bis PR #17 gemergt ist. HACS updatet nicht ungefragt (manuell) - also bis zum Merge **kein Update auf diese Integration**, und nach jedem doch erfolgten Update prüfen: `sensor.<...>_cells_per_module` muss **32** sein. (Der Bug betrifft nur die zwei Zähler-Sensoren, nicht die `cell_voltages`-Arrays - die Frühwarnung ist gegen die Vertauschung isoliert, plus Plausibilitäts-Floor `m2min > 2,5 V`.)
- **Live-vor-Merge-Stand:** Alarm-Package (`feat/byd-alarme-nativ` / PR #51) und Frühwarnung (`feat/byd-modul2-nativ`) laufen live, bevor sie auf `main` gemergt sind - plus der Lokalpatch. Drei gleichzeitige Live-vs-Repo-Abweichungen; Fenster klein halten (PR #51 bald mergen). **Schneller Rückfall:** der bydlogc-Container (Docker-VM 192.168.10.6) ist nur gestoppt, nicht entfernt - `docker start bydlogger` reaktiviert die alte MQTT-Datenquelle; HA-Backups `20c04425`/`5075f782`/`668ffd3c` decken die drei Cutover-Stufen ab.
- **BMU spricht unauthentifiziertes Modbus**, und die komplette Alarmkette hängt jetzt an diesem Port. Die VLAN-Isolation der BMU (IoT-VLAN, nur Reader-Host -> BMU:8080 + BMU -> 443/1883) sollte nicht beliebig weit nach hinten geschoben werden.

## Bewusste Auslassungen

- Kein Einzelzell-Push-Alarm (der Diagnose-Sensor reicht, siehe oben).
- Kein Balancing-Bit-Mapping auf Zellen (16 Bit vs. 32 Zellen, Zuordnung unklar - nur Zählwert).
- Keine Tieftemperatur-Regel (< 5 °C): Innenaufstellung, das BMS schützt; bei Bedarf im Winter nachrüsten.
- Keine Alarme auf `_history`-Daten (Datenqualität) und keine Latenz-Alarme (Einheiten-Bug).
- Keine Auto-Clear-/Entwarnungs-Pushes (ein Alarm ist eine Handlungsaufforderung, kein Zustandskanal).
- SoH-/Kapazitäts-Trending bleibt beim `opti_capacity`-Vorhaben bzw. Phase 2.

## Interpretations-Hinweise (LFP)

- Die Zellspreizung ist nur im Ruhezustand und im flachen Kennlinienbereich (ca. 25-85 % SoC) über die Zeit vergleichbar.
  Am Ladeschluss und am unteren Knie läuft sie kennlinienbedingt hoch (beobachtet: 292 mV bei SoC 99 %, 34-38 mV bei SoC ~17 %, 1-4 mV bei Mittel-SoC) - das ist kein Balancing-Befund.
- SoH-Änderungen von Tag zu Tag sind Rauschen - nur der Langzeittrend zählt.
- Das Verhältnis der kWh-Lebenszähler ist KEIN Wirkungsgrad (Standby- und Wandlungsverluste stecken mit drin).
- Die Alarm-Startwerte sind keine Herstellerangaben.
  Die BMS-Schutzgrenzen (2,75/3,65 V) sind Notgrenzen, keine Automationsziele - die Alarme liegen bewusst davor.

## Cutover- und Rollback-Protokoll

Der Live-Cutover ist **atomar in einem Restart** zu fahren.
Grund: alte und neue Definition teilen sich zwei unique_ids - gleichzeitig aktiv erzeugt HA `_2`-Suffixe an den entity_ids, und `sensor.byd_zellspreizung_ruhe` hat Steuerwirkung.

**Vorher notieren** (die Frühwarnung geht mit dem Cutover formell außer Betrieb, ihre Helfer tragen den Trend-Zustand):

- `input_number.byd_knie_referenzspannung`
- `input_number.byd_knie_ref_frozen`
- `input_select.byd_knie_zyklus_status`

**Cutover:**

1. `packages/byd_monitoring.yaml` nach `/config/packages/` kopieren, `notify.notify` durch den echten Mobile-App-Service ersetzen.
2. Gleichzeitig `byd_bmu.yaml` und `byd_modul2_fruehwarnung.yaml` nach `.bak-nativcutover-20260717` umbenennen.
3. `/config/automations.yaml` auf verbliebene `byd`-Referenzen prüfen.
4. HA neu starten.

**Verifikation:** keine `_2`-Entities, Frische-Binaries `on`, beide Automationen geladen, Log sauber.

**Rollback:** Dateien zurücktauschen und den `bydlogc`-Container wieder starten.

**Danach (Abbau-PR nach mehreren Beobachtungstagen):** `legacy/`, `tests/test_byd_bmu.py` und die Doku-Reste löschen, live die `.bak`-Dateien und die MQTT-Registry-Leichen (UI) entfernen.

## Phase 2: Modul-Frühwarnung neu bauen (umgesetzt)

Die Modul-2-Frühwarnung war bewusst **nicht** Teil des Alarm-Umbaus (die Alarm-Lücke war akut, die Frühwarnung ist Trend-Monitoring) und ist seit dem bydlogc-Stopp am 17.7. ohnehin funktionslos gewesen.
Der native Neubau ist inzwischen umgesetzt: Package [`packages/byd_modul2_fruehwarnung.yaml`](../packages/byd_modul2_fruehwarnung.yaml), Doku [byd-modul2-fruehwarnung.md](byd-modul2-fruehwarnung.md), Tests [`tests/test_byd_modul2_fruehwarnung.py`](../tests/test_byd_modul2_fruehwarnung.py).

Kernpunkte der Umsetzung (Details in der Frühwarnungs-Doku):

- **Metrik A präziser:** schwächste Zelle von Modul 2 gegen den Median aller 160 Zellen (aus `cell_voltages`), nur in standardisierten Betriebspunkten gesampelt (Entladeband ≥ 3 min, SoC 25-85, Zelldaten frisch) - identifiziert zusätzlich, ob es noch Zelle 23 ist.
- **Metrik B (Netto-kWh bis Knie):** Zustandsmaschine portiert; `utility_meter` auf `sensor.bms_1_charge_total_energy`/`_discharge_total_energy`, Leistung `sensor.bmu_power` (Vorzeichen identisch).
  Die Knie-Erkennung läuft statt über `for:` (das sich per Stagnation auch auf eingefrorenen Werten erfüllt) über einen **Bestätigungszähler** (2 konsekutive qualifizierende BMS-Zyklen) mit **Mess-Anker** am ersten Unterschreitungs-Sample, damit der Wartezeit-Offset aus der Messung fällt.
- Das Package hat bewusst **keinen Alarm/Push** (Trend-Metriken ohne Baseline haben keinen sinnvollen Schwellwert) und teilt sich Helfer-/Meter-IDs mit `legacy/byd_modul2_fruehwarnung.yaml` - beide nie parallel betreiben, Orphan-Purge vor dem Deploy (Liste im Package-Header).

## Tests

`tests/test_byd_monitoring.py` deckt den Template-Layer ab: Ruhefenster-Grenzen, Frische-Binaries (mit injiziertem `now`), Median-3-Verhalten über mehrere Trigger-Zyklen, Temperatur-Spreizung, Zell-Ausreißer gegen ein realistisches 5x32-Fixture, Balancing-Binary und die beiden unique_id-Carries.

**Grenze der Harness (bewusst):** sie rendert nur Jinja-Templates aus dem YAML.
Die Trigger-/`for:`-Semantik von HA ist damit **nicht** testbar - weder „eingefrorene Werte erfüllen `for:` per Stagnation" noch das Verwerfen von `for:`-Timern beim Reload noch der Gate-Wechsel nach Schwellen-Überschreitung.
Genau für diese Klasse existieren die Frische-Bedingungen, der Watchdog und der E2E-Livetest.

### E2E-Livetest (Protokoll nach `docs/`)

- **Push-Pfad:** `automation.trigger` für den normalen und den Critical-Kanal.
- **Staleness echt:** Modbus zur BMU blockieren (Route/Firewall) → `verbindung`-Alarm + Frische-Binaries kippen.
  Danach die Integration einmal reloaden **und einen HA-Neustart mit blockierter BMU durchspielen** → der Watchdog muss den Boot-Setup-Fehlschlag melden.
  Das ist der Fork-Issue-Pfad; ein Reload allein testet ihn nicht.
- **Negativ-Test:** nächste Vollladung beobachten → **kein** Alarm; `bms_warnung`-Verhalten am Ladeschluss dokumentieren (entscheidet über die Push-Aktivierung der warnings).
