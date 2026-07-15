# BYD Modul-2 Frühwarnung - Design

Stand: 2026-07-15.
Branch: `feat/byd-modul2-fruehwarnung` (abgezweigt von `feat/byd-bmu-monitoring`, PR #39).
Reviewer: Codex (adversarial Design-Review am 2026-07-15 eingearbeitet).

## 1. Kontext und Ziel

Das schwächste Modul der BYD Battery-Box Premium HVS 12.8 (Modul 2) sackt am unteren LFP-Entlade-Knie reproduzierbar unter die anderen vier Module.
Diagnose (Codex + Photovoltaikforum-Vergleich): das ist die klassische Signatur des schwächsten Glieds, kein Defekt - der Pack streut sogar enger als vergleichbare HVS im Feld.
Entscheidend für die Defekt-Früherkennung ist nicht ein einzelner mV-Wert, sondern der **Trend über Wochen**.

Ziel dieses Features: zwei abgeleitete Größen dauerhaft und belastbar loggen, sodass eine echte Degradation von normaler Streuung unterscheidbar wird, ohne dass Last, SoC-Drift, MQTT-Aussetzer oder Zähler-Artefakte den Trend verfälschen.

Zwei Kennzahlen (Ben hat "beides, voll gehärtet" gewählt):

- **Sensor A - Relative Zell-Absackung** (mV, Alltags-Diagnose, immer an): wie weit Modul 2 unter dem Feld liegt.
- **Sensor B - Nettoenergie bis Knie** (kWh, Wochen-Metrik): wie viel Nettoenergie ab Ladeabschluss entnommen wurde, bis Modul 2 unter definierter Last eine Referenzspannung erreicht.

Die Metrik heißt bewusst **Nettoenergie**, nicht Kapazität: ohne Ah-/Strom-Zählung wird nutzbare Energie gemessen, nicht Zellkapazität in Ah.

## 2. Nicht-Ziele (YAGNI)

- **Kein Alarm/Schwellwert** in dieser Ausbaustufe. Erst nach 4-8 gültigen, vergleichbaren Zyklen Baseline bilden, dann in einem Folge-PR eine Warnschwelle festlegen.
- Kein Wirkungsgrad-Korrekturfaktor. Saubere und gemischte Zyklen werden getrennt ausgewertet, nicht rechnerisch korrigiert.
- Keine parallelen Hilfssensoren für Median/Max/Ruhe über mehrere SoC-Bänder. Der am Latch festgehaltene Wert normiert bereits.
- Keine Zelleinzel-Auflösung (die liefert nur Be Connect Plus, manuell). Wir arbeiten mit dem Modul-Minimum.

## 3. Datenquellen und verifizierte Fakten

Quelle: `bydlogc` via MQTT, Abtastung ~60 s, gemappt in `packages/byd_bmu.yaml`.

- Pro Modul 1-5: `sensor.byd_modul_N_zellspannung_min` / `_max`, `_temp_min` / `_temp_max` (V bzw. °C, `expire_after: 300`).
- Gesamt: `sensor.byd_soc` (%), `sensor.byd_leistung` (W), `sensor.byd_zellspannung_max`, `binary_sensor.byd_balancing_aktiv`.
- Zähler: `sensor.byd_geladen_gesamt`, `sensor.byd_entladen_gesamt` (kWh, `total_increasing`).

Verifizierte Fakten:

- **Zähler-Auflösung 0,001 kWh** (beobachtete Rohwerte z. B. `6106.158` / `4768.690`).
  Codex' Sorge einer 0,1-kWh-Quantisierung entfällt damit - die Auflösung trägt den Trend.
- Die Zähler sind als **Absolutpaar inkonsistent** (unterschiedliche Epochen, Residuum ~1300 kWh), ihre **Inkremente über Stunden** sind aber brauchbar (Befund 12.7.).
- **Kein direkter Stromsensor (A).** Deshalb Energie statt Ah; Strom ließe sich nur als `Leistung / Packspannung` schätzen (nicht Teil dieses Designs).

Empirisch verifiziert (2026-07-15, Live-Historie):

- **Vorzeichen von `sensor.byd_leistung`: positiv = Entladen, negativ = Laden.**
  Belegt: bei SoC 90 %, `zellmax` steigend, Balancing on war die Leistung −1030 W (= Laden); nachts konstant +700 W (= Hausentladung), tagsüber negativ (= PV-Ladung).
  Das Lastband (§6.3) ist damit `sensor.byd_leistung` im Bereich **+500…+1500 W**.
- **3,20-V-Trigger-Frequenz: sparsam.** Modul-2-min erreichte 3,20 V nur am tiefen Tag (SoC 21 %, min 3,178 V), an flachen Tagen ~3,24 V.
  Der Latch feuert also nur an Tagen mit tiefer Entladung (~1 von 3 beim aktuellen Zyklen-Niveau). Valide, aber selten; bei zu wenigen Datenpunkten Referenz auf 3,24 V anheben (Regler vorhanden).

## 4. Architektur-Überblick

Alles rein beobachtend, keine Steuerwirkung. Erweitert `packages/byd_bmu.yaml`.

Entitäten:

- **Helfer**
  - `input_number.byd_knie_referenzspannung` - vom Nutzer einstellbare Referenz (Default 3,20 V, 3,05-3,35, Schritt 0,005).
  - `input_number.byd_knie_ref_frozen` - beim Scharfschalten eingefrorene Referenz des laufenden Zyklus (verhindert, dass eine Schieberei am Regler mitten im Zyklus selbst eine Kreuzung erzeugt).
  - `input_select.byd_knie_zyklus_status` - `idle` / `armed` / `latched` / `invalid`.
  - `input_text.byd_knie_invalid_grund` - Klartext-Grund bei `invalid`.
  - `input_text.byd_knie_cycle_id` - ID des laufenden Zyklus (ISO-Zeit des Voll-Ankers).
  - `input_datetime.byd_voll_anker_zeit` - Zeit des letzten Ladeabschluss-Ankers.
  - `input_boolean.byd_knie_ueberschwelle_gesehen` - seit Anker mind. einmal ein gültiger Wert klar oberhalb der Schwelle gesehen (Neustart-Fehl-Latch-Schutz).
- **Utility Meter** (persistent, Reset nur am Voll-Anker)
  - `utility_meter` auf `sensor.byd_geladen_gesamt` -> `sensor.byd_geladen_seit_voll`.
  - `utility_meter` auf `sensor.byd_entladen_gesamt` -> `sensor.byd_entladen_seit_voll`.
- **Template-/Binary-Sensoren**
  - `binary_sensor.byd_daten_frisch` - alle benötigten Quellen numerisch und ≤120 s alt.
  - `binary_sensor.byd_entladeband` - entlädt UND Leistung im standardisierten Band (s. §6.3).
  - `sensor.byd_netto_energie_seit_voll` - laufende Nettoenergie (§6.2).
  - `sensor.byd_modul2_absackung` - Sensor A, roh (§5).
  - `sensor.byd_modul2_netto_bis_knie` - Sensor B, am Latch festgehalten (§6.4).
- **Automationen**
  - `byd_voll_anker` - erkennt Ladeabschluss, setzt Zyklus auf (§6.1).
  - `byd_ueberschwelle_gesehen` - Arm-Guard (§6.4).
  - `byd_knie_latch` - Latch beim qualifizierten Knie-Ereignis (§6.4).
  - `byd_zyklus_invalid` - markiert Zyklus bei Mess-Qualitätsproblemen ungültig (§7).

## 5. Sensor A - Relative Zell-Absackung (Diagnose)

Immer-an-Template-Sensor, Einheit mV.

```
state = (median(Modul-1/3/4/5-min) − Modul-2-min) × 1000
```

- **Median der vier Peers**, nicht `min`: ein einzelnes fehlerhaftes Vergleichsmodul würde `min` nach unten ziehen und die Absackung verschleiern.
- Positiv = Modul 2 ist das schwächste Glied. Negativ = ein anderes Modul liegt tiefer (wird nicht auf 0 geklemmt, damit ein Wandern der Schwäche sichtbar bleibt).
- **Availability-Gate**: nur rechnen, wenn alle fünf Modul-Minima numerisch und ≤120 s alt sind (`binary_sensor.byd_daten_frisch`). Kein `float(0)`-Fallback.
- Attribute: `soc`, `leistung_w`, `modul2_volt`, `peer_median_volt`, `schwaechstes_modul`.

Für den Wochen-Trend wird **nicht** das rohe Maximum von A genutzt (empfindlich gegen einen einzigen Last-/MQTT-Ausreißer), sondern der A-Wert **am qualifizierten B2-Latch** (§6.4) - dort sind Zellspannung, Lastklasse und Haltezeit automatisch gleich. Sensor A roh dient der laufenden Sichtprüfung und der Historie.

## 6. Sensor B - Nettoenergie bis Knie (Wochen-Metrik)

### 6.1 Voll-Anker (Ladeabschluss-Event)

`SoC ≥ 99 %` allein ist untauglich (LFP-SoC driftet, springt verzögert, flattert).
Anker ist ein **Ladeabschluss-Ereignis**, hysteretisch als Episode (genau ein Reset pro Ladeabschluss):

Bedingungen (alle):

- `sensor.byd_zellspannung_max` ≥ 3,55-3,60 V (Primärkriterium; exakten Wert an realen Logs kalibrieren, 3,65 V wäre bei 60-s-Abtastung evtl. zu kurz sichtbar).
- In den Minuten davor wurde tatsächlich geladen.
- Danach Ladeleistung < 300-500 W für 5-10 min (Ladeende).
- Optional, wenn `binary_sensor.byd_balancing_aktiv` zuverlässig: Balancing war aktiv und ist beendet.
- `sensor.byd_soc` ≥ 94-95 % nur als Plausibilität, nicht als Trigger.
- `binary_sensor.byd_daten_frisch` = on und beide Zähler zeitlich kohärent.

Aktion am Anker:

- Beide Utility Meter zurücksetzen (`utility_meter.reset`).
- `byd_knie_ref_frozen` := aktueller `byd_knie_referenzspannung`.
- `byd_knie_ueberschwelle_gesehen` := off.
- `byd_knie_cycle_id` := jetzt (ISO), `byd_voll_anker_zeit` := jetzt.
- `byd_knie_zyklus_status` := `armed`.

**Re-Arm-Sperre**: ein neuer Anker wird erst wieder zugelassen, nachdem der Speicher seit dem letzten Anker unter ~90-95 % gefallen ist ODER ≥ 0,5-1,0 kWh netto entnommen wurden. Verhindert mehrfaches Zurücksetzen im Ladeschluss-Flattern.
Fällt der physische Ladeabschluss in einen MQTT-/HA-Ausfall, gilt der Zyklus als ungültig - kein nachträglich geschätzter Vollzeitpunkt (falsche Präzision).

### 6.2 Laufende Nettoenergie

`sensor.byd_netto_energie_seit_voll` (kWh):

```
state = entladen_seit_voll − geladen_seit_voll   (aus den beiden Utility Metern)
```

- Utility Meter statt Rohsubtraktion: persistent über HA-Neustarts, mit eigener Reset-Behandlung bei Quell-Reset/Rollover; `delta_values` aus (Quellen sind Absolutwerte). Falls die Loggerzähler real zurückgesetzt werden, `periodically_resetting` passend konfigurieren.
- **Nicht `total_increasing`**: B1 darf bei PV-Zwischenladung real sinken. Nicht auf 0 klemmen.
- **Availability**: nur gültig, wenn beide Utility Meter `has_value`. Kein `float(0)`.
- Negative **Quell**-Inkremente werden vom Utility Meter als Reset behandelt, nicht in B1 als Energie eingerechnet.
- **Plausibilitätswächter**: ein positives Netto-Inkrement größer als ~`15 kW × Δt seit letztem gültigen Wert` (plus Reserve) markiert den Zyklus `invalid` (§7), statt still "repariert" zu werden.
- Ehrlich als **Nettoenergie-Näherung** deklariert (Lade-/Entladeverluste; ≠ gespeicherte Zellenergie).

### 6.3 Standardisiertes Lastband (Kern gegen Lastabhängigkeit)

Roh gelatcht würde der erste Hochlast-Dip irreversibel eingefroren (30-40 mV Lastabsenkung verschieben die 3,20-V-Kreuzung um mehrere SoC-Prozent bzw. Zehntel-kWh) - der Wochenvergleich wäre unbrauchbar.
Ein reines Ruhefenster (<300 W) taugt am Knie ebenfalls nicht (tritt evtl. stundenlang nicht auf; die Zelle relaxiert danach wieder über die Schwelle).

`binary_sensor.byd_entladeband` = on, wenn:

- Vorzeichen eindeutig **Entladen** (`sensor.byd_leistung` > 0; verifiziert: positiv = Entladen), UND
- Entladeleistung im Band **+500…+1500 W** (≈ 1-3 A ≈ 0,04-0,12 C bei ~512 V).

Der Latch verlangt, dass dieses Band über das gesamte Bestätigungsfenster (§6.4) gehalten wurde (Leistung nicht um mehr als ~300-500 W springt).
Falls das Band real zu selten vorkommt, auf 0,5-2,0 kW erweitern - dann aber Messungen verschiedener Lastklassen **nicht** ungekennzeichnet vergleichen (Lastklasse wird am Latch mitgespeichert).

### 6.4 Latch (Knie-Ereignis)

Arm-Guard `byd_ueberschwelle_gesehen`: sobald seit dem Anker ein gültiger `Modul-2-min` klar oberhalb der Schwelle (≥ `ref_frozen` + 0,03 V) gesehen wird, wird das Flag on gesetzt.
Nach einem Ladeabschluss ist Modul 2 hoch, das Flag kippt sofort on; nach einem Neustart mitten in niedriger Spannung bleibt der zuvor gesetzte Zustand erhalten (`input_boolean` restored) - ein Fehl-Latch bei schon niedriger Spannung nach Neustart wird verhindert.

`byd_knie_latch` - Trigger: `sensor.byd_modul_2_zellspannung_min` **unter** `input_number.byd_knie_ref_frozen` (numeric_state `below` referenziert die Helfer-Entität) mit `for: 00:03:00` (120-180 s; bei 60-s-Telemetrie ~2-3 gültige Samples).

Bedingungen (alle, und über die Haltezeit gültig):

- `byd_knie_zyklus_status` = `armed` und `byd_ueberschwelle_gesehen` = on.
- `binary_sensor.byd_entladeband` = on `for: 00:03:00` (Lastband über das Fenster gehalten).
- `binary_sensor.byd_daten_frisch` = on (alle fünf Modul-Spannungen + SoC + Leistung + beide Zähler frisch/kohärent).
- `sensor.byd_netto_energie_seit_voll` gültig.

Hysterese ±5 mV: Eintritt effektiv ~3,195 V, Rückkehr erst > 3,205 V (verhindert Prellen an der Schwelle). Kein gleitender Mittelwert als Default (verschiebt die Kreuzung/unterdrückt echten Knieabfall); ein Drei-Sample-Median nur, falls reale Einzelsample-Ausreißer auftreten.

Aktion (Latch schreiben, Automation-Modus `single`):

- `sensor.byd_modul2_netto_bis_knie` := aktueller `netto_energie_seit_voll` (trigger-basierter Template-Sensor, RestoreEntity).
- `byd_knie_zyklus_status` := `latched`; Arm-Flag erst **nach** erfolgreichem Schreiben aus.

`for:` überlebt keinen HA-Neustart/Reload (laufender Countdown verworfen) - bei Neustart nahe am Knie gilt der Zyklus als "nicht sicher gemessen" (`invalid`), was für eine Wochenmetrik vertretbar ist.

### 6.5 Was am Latch gespeichert wird (Attribute)

Damit spätere Auswertung Zwischenladungen und Lastklassen beurteilen kann - nicht nur Netto:

- `netto_kwh`, `geladen_inkrement_kwh`, `entladen_inkrement_kwh` (getrennt).
- `a_absackung_mv` (Sensor A relativ zum Peer-**Median** zum Latch-Zeitpunkt).
- `soc`, `last_mittel_w`, `last_min_w`, `last_max_w` über das Bestätigungsfenster.
- `modul2_volt`, `modul_temp_c` (und Zelltemp, falls verfügbar), `ref_verwendet_v`.
- `cycle_id`, `gemessen` (Zeit).
- `sauberer_zyklus` (bool): `geladen_inkrement_kwh` < 0,2-0,5 kWh -> reine Top-nach-Knie-Entladung; sonst getrennt betrachten (pfadabhängig durch Ladeverluste/Hysterese).

## 7. Zyklus-Zustandsautomat und Gültigkeit

`byd_knie_zyklus_status`: `idle` -> (Anker) `armed` -> (Latch) `latched`; jederzeit -> `invalid`.

`byd_zyklus_invalid` setzt `invalid` + `byd_knie_invalid_grund` bei:

- Zähler-Reset/Rollover oder unplausiblem positiven Sprung (§6.2).
- Datenlücke (`byd_daten_frisch` = off) nahe der Kreuzung.
- Fehlende zeitliche Kohärenz der beiden Zähler beim Ankern/Latchen.
- HA-Neustart während eines Knie-Kandidaten (armed und Modul-2-min nahe/unter Schwelle).

Bei `invalid` wird **nicht** gelatcht und **nicht** entwaffnet; der Zyklus wird für die Trendauswertung verworfen. Kollision Anker/Latch durch einen einzigen Zustandsablauf und `single`-Modus vermeiden.

## 8. Auswertung und Trend

- Wochen-Trend = Verlauf von `sensor.byd_modul2_netto_bis_knie` über die `latched`-Ereignisse, gefiltert auf `sauberer_zyklus = true` und vergleichbare Lastklasse/Temperatur (±3 K).
- Ergänzend der am Latch festgehaltene `a_absackung_mv`.
- **Degradations-Signal** (Codex): systematisch sinkende Nettoenergie bis Knie über Wochen, aussagekräftiger als roher mV-Abstand.
- Ticket-würdig (späterer Alarm-PR): deutliche Verschiebung des Knies innerhalb weniger Wochen, nutzbare Energie reproduzierbar > 5 % gesunken, echte Ruhedivergenz > 80-100 mV, oder EC107/EC110/BMS-Leistungsbegrenzung.

Dashboard: `byd_modul2_netto_bis_knie` und `byd_modul2_absackung` (roh) als ApexCharts-Verlauf in die bestehende Sektion "Akku-Gesundheit (BMU)".

## 9. Stellschrauben

- `input_number.byd_knie_referenzspannung` (Default 3,20 V) - live einstellbar; greift erst am **nächsten** Anker (Einfrieren pro Zyklus).
- Lastband, Voll-Anker-Zellspannung und Re-Arm-Schwelle sind zunächst als Konstanten im Package dokumentiert; werden nach den ersten realen Zyklen kalibriert.

## 10. Fehlerfälle (Zusammenfassung)

| Fall | Behandlung |
|---|---|
| Quelle `unavailable` (expire_after 300 s) | `daten_frisch` = off -> kein Anker/Latch; kein `float(0)` |
| Zähler-Reset/Rollover | Utility Meter fängt es; negatives Quell-Inkrement nicht als Energie |
| Unplausibler positiver Zählersprung | Zyklus `invalid` (Grund protokolliert) |
| Zähler-Versatz 60 s | Latch nur bei kohärenten, frischen Zählern |
| Prellen an 3,20 V | Hysterese ±5 mV + `for: 3 min` |
| Fehl-Latch nach Neustart bei niedriger Spannung | `ueberschwelle_gesehen`-Guard |
| Hochlast-Dip | Lastband-Gate 0,5-1,5 kW über das Fenster |
| SoC-Drift/Flattern | Ladeabschluss-Anker statt SoC≥99 %, Re-Arm-Sperre |
| `for:` verworfen bei Neustart | Zyklus `invalid` |
| Defektes Vergleichsmodul | Peer-**Median** statt `min` |

## 11. Deployment

- Alle Entitäten in `packages/byd_bmu.yaml` ergänzen (erweitert PR #39-Feature).
- Helfer: entweder als YAML im Package oder via UI/`ha_config_set_helper` live anlegen - Weg im Implementierungsplan festlegen (Konsistenz Repo <-> live beachten, bekannte Live-Divergenz).
- Live in-place deployen (Package-Edit + Reload), analog bisheriger BYD-Arbeit; danach Live-Regressionscheck.
- MQTT-Rohsensoren bleiben in `mqtt/mqtt.yaml` (Kollisions-Falle top-level `mqtt: !include` beachten).
- Privacy-Scan vor Push (öffentliches Repo).

## 12. Test und Verifikation

Vor Implementierung:

- **Leistungs-Vorzeichen** an `sensor.byd_leistung`-Historie bestätigen (Laden vs. Entladen).
- Prüfen, wie oft Modul-2-min real unter 3,20 V geht (Trigger-Frequenz); Referenz ggf. anpassen.
- Utility-Meter-Reset-Verhalten der Loggerzähler klären (`periodically_resetting`).

Nach Implementierung:

- Anker künstlich testbar? (nicht produktiv erzwingen; an nächster realer Vollladung verifizieren.)
- Latch an einem realen tiefen Entladezyklus beobachten; Attribute auf Plausibilität prüfen.
- Neustart-Verhalten: Zustände (`input_*`, Utility Meter, trigger-Sensor) überleben Reload; Zyklus nahe Knie wird `invalid`.
- Livetest-Protokoll unter `docs/` ablegen (Repo-Konvention).

## 13. Offene Punkte

- Exakte Voll-Anker-Zellspannung (3,55 vs. 3,60 V) - an Logs kalibrieren.
- Lastband final (0,5-1,5 vs. 0,5-2,0 kW) - nach Häufigkeit realer Latches.
- Alarm-Schwelle bewusst offen bis 4-8 gültige Zyklen vorliegen (eigener Folge-PR).
