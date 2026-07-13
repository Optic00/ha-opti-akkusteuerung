# Strategie-Logik: Akku Opti Strategie

## Überblick

Die Automation `Akku Opti Strategie` trifft **ausschließlich Modus-Entscheidungen**.
Sie schreibt einen Wert in `input_select.akkusteuerung_modus` — und nur das.
Welche Hardware-Befehle dieser Modus am Wechselrichter/Speicher auslöst, steuert ein separater
Hardware-Adapter (ein eigener Automatisierungszweig). Diese Trennung macht die Strategie-Logik
unabhängig vom konkreten Speicherfabrikat.

Die Automation läuft im Modus `single`: nur eine Instanz gleichzeitig, keine Parallelausführung.
Trigger sind Preisniveau-Änderungen, BYD/SOC-Änderungen, PV-Prognose-Bewertungsänderungen
und weitere Zustandswechsel relevanter Helfer.

Zentrales Entitäts-Modell (Canonical-`opti_*`-Layer):

- `sensor.opti_soc` — aktueller Speicher-SoC (aus `opti_mapping.yaml` abgeleitet)
- `sensor.opti_forecast_score` / `sensor.opti_forecast_score_tomorrow` — PV-Fit heute/morgen (0–10)
- `sensor.opti_price_level` — aktuelles Preisniveau (VERY_CHEAP/CHEAP/NORMAL/EXPENSIVE/VERY_EXPENSIVE), anbieter-agnostisch
- `sensor.opti_target_soc` — intelligenter Ziel-SoC (aus Restprognose + Hausverbrauch)
- `input_number.minsoc` / `input_number.maxsoc` — konfigurierbare SoC-Grenzen
- `input_boolean.opti_prognose_netzladen` — Gate für prognosebasiertes Netzladen
- `input_boolean.opti_pv_ueberschuss_ladung` — Gate für PV-Überschuss-Laden
- `binary_sensor.opti_winter_charging_allowed` — Winterladefreigabe (Standard: `true`)
- `sensor.opti_peak_reserve_soc` - berechneter Reserve-SoC für kommende Preisspitzen (36h-Horizont)
- `binary_sensor.opti_peak_reserve_aktiv` - Gate: Peaks im Wiederauflade-Horizont vorhanden
- `input_number.opti_peak_verbrauch_kw` / `opti_einspeiseverguetung_ct` / `opti_netzlade_spread_ct` / `opti_peak_min_aufschlag_ct` / `opti_halte_spread_ct` - Konfiguration der Peak-Allokation
- `input_select.akkusteuerung_modus` — Ziel-Ausgang dieser Automation

---

## Sicherheit zuerst: MinSOC-Schutz

Die **erste** `choose`-Option im Block „Zwischen Speicherszenarien wählen" ist die Entladesperre:

```
Bedingung: sensor.opti_soc below input_number.minsoc
Aktion:    input_select.akkusteuerung_modus → "Akku nur Laden"
           stop: "MinSOC Schutz aktiv – Entladen gesperrt"
```

Diese Option wird **vor allen anderen Szenarien** geprüft. Fällt der SoC unter den konfigurierten
MinSOC-Wert, wechselt der Modus sofort auf „Akku nur Laden" und die weitere Abarbeitung der
`choose`-Liste wird mit `stop` beendet. Kein anderer Block kann diesen Schutz überstimmen.

---

## Winter-/Prognose-Ladelogik — Intent

### Was die Blöcke tun

Im Block „Zwischen Speicherszenarien wählen" folgen nach dem MinSOC-Schutz mehrere
**SOC-gestufte Ladeblöcke**, die bei schlechter PV-Prognose und günstigen Strompreisen
den Modus auf „Akku nur Laden" setzen:

| Block-Alias (gekürzt)                                | SOC-Schwelle | Preislevel-Limit  | Prognose-Bedingung          |
|------------------------------------------------------|-------------|-------------------|-----------------------------|
| Laden wenn morgen+heute schlecht, SOC < 20 %         | < 20 %      | bis EXPENSIVE     | heute UND morgen schlecht   |
| Laden wenn morgen+heute schlecht, SOC < 75 %         | < 75 %      | bis NORMAL        | heute UND morgen schlecht   |
| Laden wenn heute+morgen schlecht, Winter, < 80 % | < 80 %  | bis EXPENSIVE     | heute UND morgen schlecht, `opti_winter_charging_allowed` = on |
| Laden wenn heute schlecht, SOC < 15 %       | < 15 %      | bis EXPENSIVE     | heute schlecht |
| Laden wenn heute schlecht, SOC < 45 %, günstig | < 45 %   | nur CHEAP/VERY_CHEAP | heute schlecht |

### Warum diese Struktur funktioniert

Diese Blöcke wurden empirisch entwickelt und sind **Bens bewährte Umsetzung** von zwei
Prinzipien, die sich in der Praxis ergänzen:

1. **SoC-Band ~40–60 %**: Die gestuften SOC-Schwellen (15/20/45/75/80 %) verhindern sowohl
   sehr tiefe Entladung als auch unnötig volles Laden in schlechten Prognose-Situationen.
   In der Summe pendelt der Speicher — abhängig von Prognose und Preis — meist im Bereich
   40–60 % SoC.

2. **Peak-Shaving**: Die Preislevel-Bedingungen (CHEAP/NORMAL/EXPENSIVE) sorgen dafür, dass
   der Akku gezielt zu günstigen Zeiten geladen wird, um teure Spitzenlastbezüge aus dem Netz
   zu reduzieren.

### Bewährt — und wo Verbesserungspotenzial liegt

Diese Logik funktioniert zuverlässig im Winter/Schlechtwetter-Betrieb. Sie ist jedoch
**kein explizites Bandsystem**: Es gibt keine direkte Regel „lade bis 60 %, entlade bis 40 %".
Das Band entsteht als emergentes Verhalten der gestuften Schwellen zusammen mit dem
Prognose-Filter.

**Offener Verbesserungskandidat:** Eine explizite Bandregelung (z. B. Ziel-SoC-Bereich als
konfigurierbare Helfer) könnte die Logik transparenter und leichter anpassbar machen.
Das ist bewusst als zukünftiger Task (nicht Teil dieser Portierung) zurückgestellt.

---

## Der intelligente Ziel-SoC — Herzstück der Akkuschonung

Das **Kernfeature** dieser Steuerung steckt nicht in der Strategie-Automation, sondern im
abgeleiteten Sensor `sensor.opti_target_soc` (definiert in `packages/opti_derived.yaml`).
Er beantwortet die Frage: *„Wie voll soll der Akku jetzt geladen werden?"* — **prognosebasiert**.

### Warum überhaupt ein dynamischer Ziel-SoC?

Ein Akku, der morgens schon auf 100 % steht, hat zwei Nachteile:

1. **Zellalterung:** Lithium-Zellen altern kalendarisch schneller, je höher der SoC. Dauerhaft
   bei 100 % zu stehen kostet Lebensdauer.
2. **Verschenkte PV:** Ein voller Akku kann den PV-Überschuss des Tages nicht mehr aufnehmen —
   der Strom wird (oft schlecht vergütet) eingespeist statt selbst genutzt.

**Ziel:** Morgens nur so weit laden, dass die **erwartete Rest-PV des Tages** den Akku bis zum
Abend von selbst voll macht. Gute Prognose → niedriger Ziel-SoC (Platz für PV lassen).
Schlechte Prognose → hoher Ziel-SoC (mehr laden, notfalls aus dem Netz über die Ladeblöcke oben).

### Wie der Zielwert berechnet wird

Pro Aktualisierung rechnet der Sensor:

```
remaining_hours = Stunden bis Sonnenuntergang        (begrenzt auf 0.5…12 h; Fallback 6 h)
net_available   = max(0, restproduktion_kWh − hausverbrauch_kW × remaining_hours)
ratio           = net_available / akku_kapazität_kWh
```

- `restproduktion_kWh` = `sensor.opti_forecast_effective_remaining_kwh`, der zentrale
  Blend aus Median und P10 der Rest-Tag-Prognose. Default ist `min(median, p10)` (der
  konservativere der beiden gewinnt); über den Regler `input_number.opti_forecast_optimismus`
  (0-100 %) lässt sich Richtung Median optimieren. Fehlt `estimate10` oder ist er `<= 0`,
  bleibt es beim Median (siehe [canonical-layer.md](canonical-layer.md#solcast-anbindung)).
- `hausverbrauch_kW` = `sensor.opti_house_consumption_w` / 1000.
- `ratio` ist also der erwartete **PV-Überschuss bis Sonnenuntergang, ausgedrückt in
  „Akku-Kapazitäten"** — wie viele Akkufüllungen an Überschuss noch kommen.

Daraus eine **Stufenkennlinie** `ratio → Ziel-SoC`:

| `ratio` (Überschuss / Kapazität) | Ziel-SoC | Bedeutung |
|---|---|---|
| < 0.375        | **maxsoc** | kaum Überschuss erwartet → voll laden |
| 0.375 – 0.875  | **90 %** | |
| 0.875 – 1.375  | **80 %** | |
| 1.375 – 1.875  | **70 %** | |
| 1.875 – 2.875  | **60 %** | |
| ≥ 2.875        | **50 %** | viel Überschuss → niedrig halten, PV füllt auf |

Abschließend auf `[minsoc, maxsoc]` begrenzt. Sonderfall: ist der manuelle Netzlade-Booster
(`input_boolean.hausakku_aus_netz_laden`) aktiv, gilt direkt `maxsoc`.

### Hysterese statt Flattern (Schmitt-Trigger)

`ratio` driftet kontinuierlich (Sonnenuntergang rückt näher, Leistungssensoren zappeln). Sitzt
der Wert auf einer Stufengrenze, würde der Ziel-SoC zwischen zwei Nachbarstufen oszillieren
(z. B. 80 ↔ 90). Weil der reale SoC dann *zwischen* beiden Zielwerten liegt, kippte früher der
**Modus** dutzendfach pro Tag zwischen „Dynamisch" und „nur Entladen".

**Lösung:** ein echter **Schmitt-Trigger**. Die aktuelle Stufe wird im Attribut `level` des
Sensors gehalten (Selbstbezug über `this.attributes`) und erst gewechselt, wenn `ratio` die
nächste Grenze um eine Marge **m = 0.10** über- bzw. unterschreitet:

- **Hochschalten** (höhere `ratio` → niedrigerer Ziel-SoC) erst ab `Grenze + 0.10`
- **Runterschalten** erst unter `Grenze − 0.10`

Das ergibt ein Totband um jede Grenze und beseitigt das Flattern an der Wurzel. Die
Grenzwerte `[0.375, 0.875, …]` sind bewusst die **ursprünglichen effektiven Schwellen** — es
ändert sich nur das Halteverhalten, **nicht** die Lade-Kennlinie.

> **Hinweis:** Eine reine Rundung von `ratio` (z. B. auf 0.25-Schritte) ist **keine** Hysterese
> — sie verschiebt nur den Kipppunkt, schafft aber kein Halteband. Genau das war der ursprüngliche
> Bug. Hysterese braucht zwingend ein Gedächtnis des Vorzustands (hier das `level`-Attribut).

**Debug-Trace:** Das Attribut `branch` zeigt die Live-Entscheidung, z. B.
`ratio=0.47 plain=1 → level 1 → 90%` — mit Zusatz `(gehalten)`, wenn die Hysterese gerade eine
Stufe gegen die Roh-`ratio` festhält.

### Wie die Strategie den Ziel-SoC nutzt (mit Band H)

Die Modus-Automation vergleicht den realen SoC mit `sensor.opti_target_soc` — mit einem
zusätzlichen **Band H = 3 %** als zweiter Schutzschicht gegen Pendeln direkt an der Ziel-Kante:

- **SoC < Ziel − 3** → *Akku Dynamisch* (lädt Richtung Ziel, Option 16)
- **SoC > Ziel + 3** → *Akku nur Entladen* (genug Reserve, Option 17)
- **innerhalb ±3 % um das Ziel** → neutrale Zone → Default *Akku Dynamisch*

### Nachbauen über die zwei Repos

Dieses Feature lebt in **`ha-opti-akkusteuerung`** (Strategie + abgeleitete Sensoren); die
eigentliche Hardware-Ansteuerung in **`ha-modbus-akku-adapter`**. Zum Nachbauen genügen:

1. Wechselrichter über den [Canonical-Layer](canonical-layer.md) auf `sensor.opti_*` abbilden.
2. Eine **Solcast-Restprognose** (`opti_forecast_remaining_today_kwh`) und einen
   **Hausverbrauchs-Sensor** (`opti_house_consumption_w`) bereitstellen.
3. Helfer `input_number.minsoc` / `input_number.maxsoc` setzen (maxsoc = oberer Deckel; z. B.
   95 % schont die Zellen, 100 % gibt volle Kapazität).
4. Den Modus-Ausgang `input_select.akkusteuerung_modus` vom Adapter im anderen Repo umsetzen lassen.

---

## Entlade-Peak-Allokation: Reserve für die teuersten Stunden

### Das Problem

Ein Winterakku, der nach dem intelligenten Ziel-SoC geladen wird, reicht oft nicht bis zum nächsten Morgen.
Bisher entlädt die Strategie undifferenziert: sobald der Modus „Akku Dynamisch" oder „Akku nur Entladen" steht, fließt die gespeicherte Energie in der Reihenfolge ab, in der der Hausverbrauch sie abruft - unabhängig davon, ob gerade eine günstige oder eine sehr teure Stunde läuft.
Die Entlade-Peak-Allokation löst das, indem sie einen Teil des SoC gezielt für die **kommenden teuersten Stunden** reserviert, statt ihn an eine x-beliebige NORMAL-Stunde davor zu verlieren.

### Wie die Reserve berechnet wird

Der trigger-basierte Block „Entlade-Peak-Allokation" in `packages/opti_derived.yaml` rechnet alle 15 Minuten (und bei relevanten State-Changes) eine gemeinsame `peak`-Variable, aus der zwei Entitäten abgeleitet werden: `sensor.opti_peak_reserve_soc` und `binary_sensor.opti_peak_reserve_aktiv`.

**Wiederauflade-Horizont.**
Die Reserve muss nur bis zum nächsten Zeitpunkt reichen, an dem der Akku voraussichtlich wieder auflädt.
Das Fenster beginnt an der nächsten vollen Stunde und endet:

- **leer**, wenn es gerade Tag ist (Sonne über dem Horizont) und der heutige Forecast-Score gut ist (> 2) - dann füllt die PV den Akku ohnehin gleich wieder auf, eine Reserve ist überflüssig.
- sonst am **nächsten Sonnenaufgang + 3 h**, wenn der Score des Tages, an dem dieser Sonnenaufgang liegt, gut ist (> 2) - heute oder morgen, je nachdem, ob der nächste Sonnenaufgang schon heute war oder erst morgen kommt.
- sonst **maximal 36 h** ab jetzt (kein guter Score in Sicht, oder Score fehlt).

Wichtig: Ist der nächste Sonnenaufgang erst **morgen** (Score von morgen entscheidet), zählt die **heutige** Abendspitze trotzdem mit - sie liegt ja vor diesem Wiederaufladepunkt.
Ein sonniger Tag von morgen schließt die heutige Abendspitze also nicht aus dem Horizont aus, er verkürzt ihn nur ab dem morgigen Sonnenaufgang + 3 h.

**Klassifikation.**
Jede Stunde im Horizont wird mit demselben **Midrank-Perzentil** wie `sensor.opti_price_level` eingestuft (gleiche Grenzen: ≥ 80 % → VERY_EXPENSIVE, ≥ 60 % → EXPENSIVE) - ein konsistentes Preisniveau-Konzept für die ganze Strategie.

**Ökonomische Peak-Filterung (Tuning-Hebel 1).**
Zusätzlich zur Perzentil-Einstufung zählt eine Stunde nur dann als Peak, wenn ihr Preis das Horizont-Tief (`fenster_min_ct`, günstigster Preis im Horizont) um mindestens `input_number.opti_peak_min_aufschlag_ct` übersteigt (UND-Bedingung, Grenzfall `>=` zählt).
Das filtert die Perzentil-Zwangs-Peaks flacher Tage heraus: bei 30.0 vs 30.5 ct gibt es nichts zu reservieren, auch wenn die 30.5-ct-Stunden formal im obersten Quintil liegen.
`min_preis_vor_peak_ct` bezieht sich weiterhin auf die erste **gezählte** Peak-Stunde.
Empfohlener Startwert aus dem Winter-Backtest-Sweep (Nov 25 - Feb 26, siehe unten): **5 ct**.

**Formel.**
Für jede VERY_EXPENSIVE- bzw. VERY_EXPENSIVE+EXPENSIVE-Stunde im Horizont:

```
reserve_kwh = anzahl_stunden * opti_peak_verbrauch_kw / eta      (eta = 0.9 Entladewirkungsgrad)
reserve_soc = min(minsoc + reserve_kwh / kapazität_kwh * 100, maxsoc)
```

`reserve_ve_soc` deckt nur die VERY_EXPENSIVE-Stunden, `reserve_gesamt_soc` (der State von `sensor.opti_peak_reserve_soc`) zusätzlich die EXPENSIVE-Stunden.
Beide werden bei `maxsoc` gekappt - ein kleiner Akku kann nie mehr reservieren, als er fasst.

### Die Leiter: L1-L4

Ist `binary_sensor.opti_peak_reserve_aktiv` an (Reserve-Sensor gültig **und** mindestens eine Peak-Stunde im Horizont), steuert eine vierstufige „Leiter" die Entlade-Freigabe.
Sie steht in der Optionsliste **vor** den normalen Ziel-SoC-Optionen, sonst würde „nur Entladen über Ziel-SoC" die Reserve bei NORMAL-Preis vorzeitig verheizen.

| Stufe | Preisniveau | Zusatzbedingung | Modus |
|---|---|---|---|
| **L1** | VERY_EXPENSIVE | - | Akku nur Entladen |
| **L2** | EXPENSIVE | SoC > `reserve_ve_soc` + Band | Akku nur Entladen |
| **L3** | EXPENSIVE | SoC ≤ `reserve_ve_soc` + Band **und** `peak_preis_ve_avg_ct` - aktueller Preis ≥ `opti_halte_spread_ct` | Akku nur Laden (halten) |
| **L4** | NORMAL oder billiger | SoC ≤ `reserve_gesamt_soc` + Band | Akku nur Laden (halten) |

Ist keine Stufe einschlägig (z. B. NORMAL-Preis mit ausreichend SoC über der Reserve), fällt die Prüfung durch zu den normalen Ziel-SoC-Optionen.

**L3-Halte-Spread (Tuning-Hebel 2).**
L3 hält bei EXPENSIVE nur noch, wenn der Preisdurchschnitt der kommenden gezählten VERY_EXPENSIVE-Stunden (neues Attribut `peak_preis_ve_avg_ct` am Reserve-Sensor, `none` ohne VE-Stunden) mindestens `input_number.opti_halte_spread_ct` über dem aktuellen Preis liegt.
Halten bei dünnem VE/EXP-Spread (3-6 ct) kostet mehr Netzbezug, als das spätere VE-Entladen zurückbringt.
Greift die Bedingung nicht, fällt EXPENSIVE ohne Halten zur restlichen Kette durch (L2 hat oberhalb der VE-Reserve bereits entladen, unterhalb geht es zu den Ziel-SoC-Optionen bzw. dem Default).
Empfohlener Startwert aus dem Winter-Backtest-Sweep: **5 ct**.

**Tuning-Runde Winter-Backtest (2026-07-02).**
Beide Hebel wurden per gestuftem Parameter-Sweep gegen einen 120-Tage-Winter-Backtest (Nov 2025 - Feb 2026, echte Preise/Last/PV) kalibriert; `opti_netzlade_spread_ct` = 10 wurde dabei bestätigt (15 drückt die VE-Abdeckung unter die Vorgabe).
Gewinner-Kombination: `opti_peak_min_aufschlag_ct` = 5, `opti_halte_spread_ct` = 5, `opti_netzlade_spread_ct` = 10.
Die ersten beiden Helfer haben Template-Fallbacks (10 ct bzw. 3 ct) - diese gelten aber nur, wenn der jeweilige Helfer gar nicht existiert. Ein bereits angelegter Helfer gewinnt immer gegen den Fallback, auch mit dem Erststart-Wert 0 (siehe Erststart-Werte-Tabelle im README). Nach dem ersten Anlegen der Helfer also aktiv auf die empfohlenen Startwerte setzen, sonst steuert die Strategie mit 0 statt mit 5.
Ergebnis: VE-Stunden-Abdeckung aus dem Akku steigt von 26.2 % (alt) auf 32.9 %, die Mehrkosten der Peak-Allokation sinken von 17.07 EUR auf 3.61 EUR pro Winter (inkl. Rest-SoC-Korrektur), die PV-Verdrängung von 131 kWh auf 53 kWh.
Das ursprüngliche Ziel "billiger als alt bei mehr VE-Abdeckung" wurde damit **nicht** erreicht - die Peak-Allokation kostet im Backtest-Winter noch ~3.6 EUR Aufpreis für +6.7 Prozentpunkte VE-Abdeckung.

**Asymmetrisches Freigabeband.**
Damit der Modus nicht direkt an der Reserve-Kante flattert, ist das Band abhängig vom *aktuellen* Modus - der Modus dient als Gedächtnis des Vorzustands:

- **+5 %**, wenn der aktuelle Modus „Akku nur Laden" ist (aus dem Halten heraus erst bei deutlichem Überschuss wieder freigeben)
- **+3 %**, wenn der aktuelle Modus etwas anderes ist (beim Entladen genügt ein kleineres Band, um nicht sofort wieder zu halten)

### Negativpreis-Laderegel

Steht **vor** den alten SOC-gestuften Ladeblöcken (Option 2 in der Übersicht unten) und lädt gezielt bis `maxsoc`, wenn Netzstrom günstiger ist als die eigene Einspeisung:

| Eigenschaft | Wert |
|---|---|
| Bedingung | Preis < `input_number.opti_einspeiseverguetung_ct` (Default 0 ct, Regel greift dann nur bei negativen Preisen), `opti_forecast_score` < 3, SoC < maxsoc |
| Ladefenster | keine günstigere Stunde vor der nächsten Preisspitze in Sicht (siehe unten) |
| Gate | `input_boolean.opti_prognose_netzladen` = on |
| Ziel-SoC | maxsoc |
| Gesetzter Modus | Akku Netzladen (braucht `ha-modbus-akku-adapter` >= v1.5.0) |

### Peak-Vorladeregel

Direkt danach (Option 3): lädt gezielt bis zur Gesamt-Reserve nach, wenn sich das wirtschaftlich lohnt, auch ohne negative Preise.

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_peak_reserve_aktiv` = on, SoC < `reserve_gesamt_soc`, (`peak_preis_avg_ct` minus aktueller Preis) ≥ `input_number.opti_netzlade_spread_ct` (Default 10 ct) |
| Ladefenster | wie Negativpreis-Regel |
| Gate | `input_boolean.opti_prognose_netzladen` = on |
| Ziel-SoC | `reserve_gesamt_soc` (**nicht** maxsoc) |
| Gesetzter Modus | Akku Netzladen (braucht `ha-modbus-akku-adapter` >= v1.5.0) |
| Selbst-Stop | Bedingung entfällt automatisch, sobald SoC die Reserve erreicht - kein extra Abschalt-Trigger nötig |

Der Spread-Schwellwert (Default 10 ct) ist bewusst so hoch gesetzt, dass er die Round-Trip-Verluste des Akkus (Lade- + Entladewirkungsgrad) deckt.
Vorladen soll sich nach Verlusten noch lohnen, nicht nur brutto.

### Ladefenster-Wahl

Beide Laderegeln oben laden nicht einfach sofort los, sobald ihre Preisbedingung erfüllt ist.
Sie prüfen zusätzlich `min_preis_vor_peak_ct` (Attribut von `sensor.opti_peak_reserve_soc`): den günstigsten Preis, der **vor** der nächsten Preisspitze noch kommt.
Ist der aktuelle Preis mehr als **0.5 ct** teurer als dieses Minimum, wird gewartet, weil eine noch günstigere Stunde absehbar ist.

Das ist **selbstkorrigierend**: `min_preis_vor_peak_ct` wird bei jeder Neuberechnung aus den aktuellen Preislisten ermittelt.
Trifft die erwartete günstigere Stunde nicht ein (z. B. weil sich der Markt seit der letzten Preisaktualisierung geändert hat), wird der neue, jetzt aktuelle Minimalpreis zum Maßstab.
Die Regel wartet also nicht ewig auf einen Wert, der nicht mehr existiert.

### Fail-safes und bekannte Grenzen

- **< 4 Preise gesamt** (`today` + `tomorrow`): `sensor.opti_peak_reserve_soc` wird `unavailable`, `binary_sensor.opti_peak_reserve_aktiv` fällt auf `off` - die komplette Leiter (L1-L4) ist inaktiv.
  Das ist **bewusst anders** als der NORMAL-Fallback von `sensor.opti_price_level` bei derselben Datenlage: eine Reserve von 0 % sähe wie „keine Peaks in Sicht" aus und würde die Leiter fälschlich freigeben, statt sie einfach abzuschalten.
- **Raster-Erkennung:** Die Preislisten liefern keine Zeitstempel.
  Die Slot-Länge (`slot_h`) wird pro Liste (`today`/`tomorrow` getrennt) aus der Listenlänge abgeleitet: 24 geteilt durch die Anzahl der Einträge.
  Unterstützt werden Stundenraster (Listenlänge 20-27, inklusive Zeitumstellungstage) und Viertelstundenraster (Listenlänge 80-108, inklusive Zeitumstellungstage) - seit der Tibber-Umstellung auf 15-Minuten-Day-Ahead-Preise (Juli 2026) liefert `sensor.opti_price_series` 96 Werte pro Tag statt 24.
  Jede andere Listenlänge macht die komplette Preisbasis `gueltig=false`.
  An Tagen mit Zeitumstellung ist `slot_h` (z. B. 24/92 oder 24/100 bei Viertelstunden) leicht ungenau - akzeptiertes, bekanntes Verhalten.
  Das Fenster beginnt weiterhin an der aktuellen vollen Stunde (nicht am aktuellen Slot): bei Viertelstundenraster zählen dadurch bis zu drei bereits vergangene Slots der laufenden Stunde konservativ mit.

### Wechselwirkung mit den alten Ladeblöcken

Die bestehenden SOC-gestuften Ladeblöcke (Optionen 8-12 in der Übersicht unten, siehe [Winter-/Prognose-Ladelogik](#winter-prognose-ladelogik--intent)) stehen weiterhin **hinter** der Negativpreis-/Vorladeregel und **hinter** den Entlade-Stufen der Leiter (L1/L2), aber **vor** deren Halte-Stufen (L3/L4) - Peak-Entladen schlägt Winterladen, günstiges Laden schlägt Halten (Ben-Entscheidung 2026-07-02). Sie kennen die Ladefenster-Wahl **nicht** - sie laden sofort, sobald ihre eigenen SoC-/Preisbedingungen erfüllt sind.
Das ist bewusst so belassen: Fällt die Peak-Allokation aus (z. B. wegen zu weniger Preise) oder ist ihre Reserve zu knapp bemessen, sorgen die alten Blöcke weiterhin als **Sicherheitsnetz** dafür, dass bei schlechter Prognose überhaupt geladen wird, unabhängig davon, ob gerade das optimale Fenster ist.

---

## Balancing-/Deep-Charge-Watchdog

Ein Lithium-BMS (hier BYD HVS 12.8 kWh) muss den Akku regelmäßig einmal ~voll laden, um die
Zellen zu balancen und die SoC-Kalibrierung frisch zu halten. Im PV-Alltag - und besonders im
Sommer-Schonband mit `maxsoc` < 100 - erreicht die dynamische Ziel-SoC-Treppe die 100 % aber
nie. Der Watchdog erzwingt diesen Voll-Zyklus gezielt, sobald der Akku zu lange nicht mehr
oben war.

**Zustand rein abgeleitet, kein Latch:** `sensor.opti_balancing_watchdog` berechnet seinen
Zustand jederzeit neu aus dem Zähler `counter.tage_seit_akku100`, dem SoC und den Helfern -
damit ist er **restart-durabel** (kein gelatchtes Flag, das ein HA-Neustart verlieren könnte).

**Fälligkeit:** Der Watchdog wird fällig, wenn
`input_number.opti_balancing_intervall_tage` > 0 **und** SoC < 100 % **und**
`counter.tage_seit_akku100` ≥ Intervall. Das Ladeziel ist bewusst **100 %** (nicht nur
`maxsoc`), damit die Zelle in die CV-Phase kommt. `intervall = 0` schaltet den Watchdog
komplett aus.

**Zähler-Pflege (zwei Automationen in `automations/opti_balancing_counter.yaml`):**
`opti_balancing_counter_increment` zählt `counter.tage_seit_akku100` täglich um 23:59 um 1
hoch, solange der Akku an dem Tag den Done-SoC nicht erreicht hat.
`opti_balancing_counter_reset` setzt den Zähler auf 0, sobald der Akku **30 min stabil** über
`input_number.opti_balancing_done_soc` (Default 98.5 %) steht. Der Reset ist bewusst ein
numeric_state-**Trigger** mit `for: 30 min` (nicht eine numeric_state-Condition mit `for:` -
letzteres wertet HA nicht belastbar aus). Beide nutzen dieselbe Done-Schwelle - eine einzige
Voll-/Done-Definition. Damit hält der Watchdog (Ladeziel 100 %/CV-Phase), bis der Reset-Trigger
nach stabilem Stand über Done-SoC feuert und `counter = 0` setzt → Watchdog `aus`.

**Staffelung (Kosten aufsteigend)** - der Zustand entscheidet über den Strategie-Zweig:

| Zustand | Bedingung (fällig vorausgesetzt) | Strategie-Modus |
|---|---|---|
| `pv` | tagsüber (`sun.sun` above_horizon) | Akku nur Laden |
| `netz` | nachts, **`opti_balancing_netzladen` = on**, aktueller Preis < Einspeisevergütung (gratis/negativ) | Akku Netzladen |
| `netz` | nachts, **`opti_balancing_netzladen` = on**, nach Karenz (`counter` ≥ Intervall + `opti_balancing_karenz_tage`), Preisniveau VERY_CHEAP/CHEAP **und** aktueller Preis ≤ `opti_balancing_max_ct` (> 0) | Akku Netzladen |
| `aus` | sonst (auf besseres Fenster warten) | — (Kaskade läuft weiter) |

**Netzlade-Schalter:** Beide `netz`-Zweige hängen am **eigenen** Schalter
`input_boolean.opti_balancing_netzladen` (**Default aus**) - ist er aus, bleibt der Watchdog
nachts `aus` und balancet rein per PV. Bewusst **entkoppelt** von `opti_prognose_netzladen`:
so lässt sich Balancing-Netzladen erlauben, ohne das allgemeine Prognose-Netzladen zu öffnen
(die **harte Netzlade-Garantie** bleibt darüber erhalten, siehe
[canonical-layer.md](canonical-layer.md#harte-garantie-gegen-jedes-netzladen)). Der
`pv`-Zweig ist **ungegatet** - PV-Vollladung zieht keinen Netzstrom.

Der PV-Zweig ist im MVP bewusst simpel (ganztags PV-bevorzugt, kein PV > Haus-Gate). Der
bezahlte Netz-Fallback ist doppelt abgesichert: erst nach der Karenz, nur bei günstigem
Preisniveau **und** unter einem absoluten Brutto-Deckel. `opti_balancing_max_ct = 0`
(Erststart-Wert) lässt den bezahlten Fallback komplett aus - fail-safe kein bezahltes
Netzladen, bis der Deckel bewusst gesetzt wird (empfohlen 25 ct).

**Position in der Kaskade:** Die beiden Watchdog-Zweige stehen **nach** der Peak-Entlade-Leiter
L1/L2 (ein aktiver Preisspitzen-Peak schlägt das Balancing) und **vor** den Prognose-Ladeblöcken
(Optionen 6/7 in der Übersicht unten). Fehlt `opti_soc`, ist der Watchdog `aus` (fail-safe).

---

## Ladedeckel: `maxsoc` als harte Obergrenze

`input_number.maxsoc` ist ein **harter Ladedeckel**, nicht nur ein Planungswert.
Oberhalb der Obergrenze wählt die Strategie den Modus **Akku nur Entladen** und der Adapter fährt die Ladeleistung auf 0.
Der einzige reguläre Weg über `maxsoc` hinaus ist der Balancing-Watchdog (gezielt bis ~100 % fürs BMS-Balancing).

**Warum der Zweig nötig ist:** Ohne ihn setzt die neutrale Zone um den Ziel-SoC (der Ziel-SoC-Entladezweig greift erst bei `soc > Ziel + 3`) bzw. der Default den Modus auf **Akku Dynamisch**.
In „Dynamisch" gibt die Strategie die Kontrolle an die WR-Eigenverbrauchslogik ab, und der Wechselrichter trickelt freien PV-Überschuss über `maxsoc` hinaus (live beobachtet 2026-07-13: SoC 95 → 99 % bei `maxsoc` 95, obwohl Export-Headroom vorhanden war).
Der Deckel schließt diese Lücke, indem er oberhalb `maxsoc` aktiv **Akku nur Entladen** erzwingt.

**Bedingungen:** `binary_sensor.opti_peak_reserve_aktiv` = `off` **und** `soc >= maxsoc` (mit Hysterese).
Das Peak-Reserve-Gate gibt der Peak-Reserve-Haltelogik (L3/L4) bei anstehender Preisspitze bewusst Vorrang - dann darf die Reserve bis knapp über `maxsoc` gehalten werden, statt sie vor dem Peak zu verlieren.

**Anti-Flatter-Hysterese:** Rein bei `soc >= maxsoc`, drin bleibt der Deckel bis `soc < maxsoc − 3` (asymmetrisch, an den Modus-String gebunden - dieselbe Philosophie wie der Ziel-SoC-Entladezweig).
So chattert der Modus nicht an der Kante.

**Position in der Kaskade:** **nach** dem Balancing-Watchdog (der darf `maxsoc` bewusst überschreiten) und **vor** den Prognose-Ladeblöcken, Überschuss-Zweigen und dem Default - damit greift der Deckel, bevor irgendein Ladepfad über `maxsoc` hinaus aktiv werden kann.

---

## Block-für-Block-Übersicht

Die Automation besteht aus mehreren Aktionsblöcken, die **sequenziell** ausgeführt werden.
Der wichtigste ist „Zwischen Speicherszenarien wählen" mit 20 Optionen und einem Default-Pfad.

> **Hinweis — Counter-Pflege ausgelagert:** Der Zähler `counter.tage_seit_akku100` (Increment
> täglich, Reset bei 30 min stabil über Done-SoC) liegt **nicht** in dieser Automation, sondern
> als zwei eigene, trigger-basierte Automationen in `automations/opti_balancing_counter.yaml` -
> ein zuverlässiger 30-min-Halt braucht einen numeric_state-**Trigger** mit `for:`, nicht eine
> gleichnamige Condition. Details siehe [Balancing-/Deep-Charge-Watchdog](#balancing-deep-charge-watchdog).

---

### Aktionsblock 1 — „Zwischen Speicherszenarien wählen" (20 Optionen + Default)

Dies ist das Herzstück. Die Optionen werden **der Reihe nach** geprüft;
die erste, deren Bedingungen alle erfüllt sind, wird ausgeführt und die weitere Prüfung
(via `stop`) beendet. Kein nachfolgender Block kann eine bereits getroffene Entscheidung
überschreiben.

#### Option 1 — MinSOC-Schutz: Entladen sperren (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < `input_number.minsoc` |
| Preis | irrelevant |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Sicherheitsnetz. Fällt der Speicher unter den konfigurierten Mindest-SoC,
wird das Entladen sofort gestoppt — unabhängig von Preis, Prognose oder Tageszeit.
Diese Option steht bewusst ganz oben und kann von keinem anderen Block überstimmt werden.

---

#### Option 2 - Negativpreis-Laden (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | Preis < `input_number.opti_einspeiseverguetung_ct`, `opti_forecast_score` < 3, SoC < maxsoc |
| Preis | unter Einspeisevergütung (Default 0 ct, faktisch nur negative Preise) |
| Tageszeit | jederzeit |
| Zusatz | Ladefenster-Wahl (keine günstigere Stunde vor der nächsten Spitze in Sicht) |
| Gesetzter Modus | **Akku Netzladen** (bis maxsoc; braucht `ha-modbus-akku-adapter` >= v1.5.0) |

**Warum:** Ist der Netzstrom günstiger als die eigene Einspeisevergütung, lohnt sich Laden aus
dem Netz fast immer, unabhängig vom sonstigen SoC-Niveau.
Details zur Reserve-Logik, der Ladefenster-Wahl und den Fail-safes stehen im Abschnitt
**[Entlade-Peak-Allokation](#entlade-peak-allokation-reserve-für-die-teuersten-stunden)**.

**Warum ein eigener Modus:** „Akku nur Laden" ist im Live-Setup eine reine Entladesperre
(`min_ladestaerke = 0`, kein Netzbezug) — damit wäre diese Regel wirkungslos. „Akku Netzladen"
setzt am Wechselrichter `BatChaMinW = opti_charge_power_w` (dynamisch, SMA-Register 2289) und
erzwingt so tatsächliches Laden aus dem Netz.

---

#### Option 3 - Peak-Vorladen (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_peak_reserve_aktiv` = on, SoC < `reserve_gesamt_soc`, Spread zur kommenden Spitze ≥ `opti_netzlade_spread_ct` |
| Preis | irrelevant (Spread-Vergleich statt Preisniveau) |
| Tageszeit | jederzeit |
| Zusatz | Ladefenster-Wahl wie Option 2; stoppt selbsttätig bei Erreichen der Reserve |
| Gesetzter Modus | **Akku Netzladen** (bis `reserve_gesamt_soc`, nicht maxsoc; braucht `ha-modbus-akku-adapter` >= v1.5.0) |

**Warum:** Ist eine kommende Preisspitze absehbar deutlich teurer als der aktuelle Preis,
lohnt sich gezieltes Vorladen bis zur Reserve, auch ohne dass der Preis gerade negativ ist.
Details siehe **[Entlade-Peak-Allokation](#entlade-peak-allokation-reserve-für-die-teuersten-stunden)**.

---

#### Option 4 - Peak-Leiter L1: Entladen bei VERY_EXPENSIVE (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_peak_reserve_aktiv` = on |
| Preis | VERY_EXPENSIVE |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Entladen** |

**Warum:** Die teuerste Preisklasse darf immer aus dem Akku bedient werden, dafür ist die
Reserve ja da. Details zur Leiter (L1-L4) und zum Freigabeband stehen im Abschnitt
**[Entlade-Peak-Allokation](#entlade-peak-allokation-reserve-für-die-teuersten-stunden)**.

**Warum vor den alten Ladeblöcken (Ben-Entscheidung 2026-07-02):** L1/L2 stehen jetzt direkt
nach der Peak-Vorladeregel und damit vor den alten SOC-gestuften Ladeblöcken (Option 8-12):
Peak-Entladen schlägt Winterladen. Die Halte-Stufen L3/L4 (Option 16/17) bleiben hinter den
Ladeblöcken stehen — günstiges Laden schlägt Halten. Dazwischen (Option 6/7) sitzt der
Balancing-Watchdog: er lädt vor den Prognoseblöcken, aber hinter einem aktiven Peak.

---

#### Option 5 - Peak-Leiter L2: Entladen bei EXPENSIVE über VE-Reserve (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_peak_reserve_aktiv` = on, SoC > `reserve_ve_soc` + Freigabeband |
| Preis | EXPENSIVE |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Entladen** |

**Warum:** Bei EXPENSIVE darf entladen werden, solange noch genug SoC über der
VERY_EXPENSIVE-Reserve liegt (Freigabeband +5 %/+3 %, siehe Hauptabschnitt).

---

#### Option 6 - Balancing-Watchdog: PV-Vollladung (Tag)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `sensor.opti_balancing_watchdog` = `pv` (fällig **und** tagsüber) |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Ist der Watchdog fällig (Akku zu lange nicht ~voll), wird tagsüber PV-bevorzugt bis
100 % geladen, damit das BMS balancen kann. Details und Staffelung siehe
**[Balancing-/Deep-Charge-Watchdog](#balancing-deep-charge-watchdog)**.

---

#### Option 7 - Balancing-Watchdog: Netz-Vollladung (Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `sensor.opti_balancing_watchdog` = `netz` (fällig, nachts, `opti_balancing_netzladen` = on; gratis/negativer Preis, oder nach Karenz günstig **und** unter dem Preisdeckel) |
| Preis | gratis/negativ (< Einspeisevergütung) oder VERY_CHEAP/CHEAP ≤ `opti_balancing_max_ct` |
| Tageszeit | nachts (der PV-Zweig hat tagsüber Vorrang) |
| Schalter | `input_boolean.opti_balancing_netzladen` muss `on` sein (im Sensor gegated, Default aus) |
| Gesetzter Modus | **Akku Netzladen** (braucht `ha-modbus-akku-adapter` >= v1.5.0) |

**Warum:** Reicht die PV nicht (nachts), lädt der Watchdog aus dem Netz - sofort bei gratis/
negativem Strom, sonst erst nach einer Karenz und nur günstig unter einem absoluten Deckel.
Beide `netz`-Fälle hängen am eigenen Wartungs-Schalter `opti_balancing_netzladen` (Default
aus, entkoppelt von `opti_prognose_netzladen`); der `pv`-Zweig bleibt ungegatet.
`opti_balancing_max_ct = 0` (Erststart) lässt den bezahlten Fallback aus. Details siehe
**[Balancing-/Deep-Charge-Watchdog](#balancing-deep-charge-watchdog)**.

---

#### Option 8 — Laden wenn heute + morgen schlecht, SoC < 20 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 20 %, `opti_forecast_score` ≤ 2, `opti_forecast_score_tomorrow` ≤ 2 |
| Preis | bis EXPENSIVE (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum (Nuance):** Bei sehr leerem Akku (< 20 %) und schlechter Prognose für beide Tage
wird Netzladen auch bei EXPENSIVE-Preisen erlaubt. Die Ratio: Ein fast leerer Akku bei zwei
aufeinanderfolgenden Schlechtwettertagen ist eine Notfallsituation — da ist auch teurer Strom
noch besser als kein Puffer. Diese Ausnahmeregelung gilt nur für diese kritische SOC-Schwelle.

---

#### Option 9 — Laden wenn heute + morgen schlecht, SoC < 75 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 75 %, `opti_forecast_score` ≤ 2, `opti_forecast_score_tomorrow` ≤ 2 |
| Preis | bis NORMAL (VERY_CHEAP / CHEAP / NORMAL — kein EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum (Nuance):** Bei moderatem SoC (20–75 %) und zwei schlechten Prognosetagen wird
bis NORMAL-Preis nachgeladen. Der Grenze zu EXPENSIVE bleibt verschlossen, weil der Akku
noch nicht kritisch leer ist — es lohnt sich, auf günstigere Stunden zu warten.
Dieser Block bildet zusammen mit Option 8 eine bewusste Abstufung: je leerer der Akku,
desto teureren Strom darf die Automatik akzeptieren.

---

#### Option 10 — Laden wenn heute + morgen schlecht, Wintermodus, SoC < 80 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 80 %, `opti_forecast_score` ≤ 2 + `opti_forecast_score_tomorrow` ≤ 2, `binary_sensor.opti_winter_charging_allowed` = on |
| Preis | bis EXPENSIVE (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Dieser Block ist der „Wintermodus-Booster". Er greift wenn beide Tage schlecht sind
und die Winterladefreigabe aktiv ist — dann wird aggressiver bis 80 % geladen, auch bei EXPENSIVE-Preisen.

**Hinweis:** `binary_sensor.opti_winter_charging_allowed` ist in `opti_derived.yaml`
als fail-open Gate definiert (Standard: immer `true`). Es kann mit einem eigenen
Sommermodus-Schalter überschrieben werden, wenn ein saisonales Gate gewünscht ist.

---

#### Option 11 — Laden wenn heute schlecht, SoC < 15 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 15 %, `opti_forecast_score` ≤ 2 |
| Preis | bis EXPENSIVE (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Ähnlich wie Option 8, aber unabhängig von der Morgen-Prognose. Wenn der Akku
fast leer ist (< 15 %) und die heutige Bewertung schlecht ist, wird notgeladen — ohne
Rücksicht auf morgen. Kurzfrist-Schutz.

---

#### Option 12 — Laden wenn heute schlecht, SoC < 45 %, Strom sehr günstig (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 45 %, `opti_forecast_score` ≤ 2 |
| Preis | nur VERY_CHEAP / CHEAP |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Opportunistisches Laden bei sehr günstigen Preisen. Hier wird nicht aus
Notwendigkeit nachgeladen, sondern weil Strom gerade besonders billig ist. Dafür ist
die Preisbedingung strenger (kein NORMAL, kein EXPENSIVE) — nur wirklich günstige
Stunden rechtfertigen das Netzladen bei diesem SoC-Niveau.

---

#### Option 13 — Bei 70%-Überschuss laden (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_ueberschuss_70_aktiv` = on (Export **plus Batterieleistung** über der 70%-Grenze, 30 s entprellt, 1 kW Hysterese), SoC < 100 %, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |
| Gate | `input_boolean.opti_pv_ueberschuss_ladung` muss `on` sein |

**Warum:** Sobald die PV mehr ins Netz einspeist als die konfigurierte 70%-Grenze erlaubt,
soll dieser Überschuss in den Akku. „Dynamisch" gibt dem Adapter die Freiheit, genau die
richtige Ladeleistung zu wählen.
Das Signal rechnet die Batterieleistung mit ein (= Export ohne Akku-Eingriff): der rohe
Export ist über den Akku rückgekoppelt (Laden drückt ihn unter die Grenze und würde die
eigene Freigabe sofort wieder beenden - live beobachtetes Minutentakt-Flattern).
Entprellung 30 s beidseitig plus Hysterese-Band, wie in der bewährten Opti-2.0-Automatik.

---

#### Option 14 — Bei AC-Überschuss laden (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_ueberschuss_ac_aktiv` = on (WR-AC-Leistung **plus Batterieleistung** über der WR-Nennleistungsgrenze, 30 s entprellt, 300 W Hysterese), SoC < 100 %, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |
| Gate | `input_boolean.opti_pv_ueberschuss_ladung` muss `on` sein |

**Warum:** Ähnlich wie Option 11, aber die Messgröße ist die WR-AC-Ausgangsleistung statt
der Netzeinspeisung. Nutzt verfügbare PV-Energie aktiv, bevor sie verschwendet wird.

---

#### Option 15 — Bei vollem Akku auf Dynamisch schalten

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC > 99 % |
| Preis | irrelevant |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku Dynamisch** |

**Warum:** Ein voller Akku soll nicht weiter geladen werden, aber auch nicht zwingend
aktiv entladen. „Dynamisch" erlaubt dem Adapter zu entscheiden: bei PV-Überschuss
einspeisen, bei Verbrauch den Akku nutzen — je nach aktuellem Systemzustand.

---

#### Option 16 - Peak-Leiter L3: Halten bei EXPENSIVE unter VE-Reserve (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_peak_reserve_aktiv` = on, SoC ≤ `reserve_ve_soc` + Freigabeband, `peak_preis_ve_avg_ct` - aktueller Preis ≥ `opti_halte_spread_ct` |
| Preis | EXPENSIVE |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** (Entladesperre, hält die VE-Reserve) |

**Warum:** Reicht der SoC nicht mehr deutlich über der VERY_EXPENSIVE-Reserve, wird das
Entladen gesperrt, damit die kommende VERY_EXPENSIVE-Stunde nicht leerläuft.
Der Halte-Spread verhindert Halten ohne echten VE-Vorteil: liegen die kommenden VE-Stunden
preislich kaum über der aktuellen EXPENSIVE-Stunde, kostet die Entladesperre nur Netzbezug
(siehe [L3-Halte-Spread](#die-leiter-l1-l4)).

---

#### Option 17 - Peak-Leiter L4: Halten bei NORMAL oder billiger unter Gesamt-Reserve (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `binary_sensor.opti_peak_reserve_aktiv` = on, SoC ≤ `reserve_gesamt_soc` + Freigabeband |
| Preis | VERY_CHEAP / CHEAP / NORMAL |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** (Entladesperre, hält die Gesamt-Reserve) |

**Warum:** Auch bei günstigem Preis wird nicht in die für Peaks reservierte Energie
entladen, solange der SoC die Gesamt-Reserve noch nicht deutlich übersteigt.
Trifft keine der vier Leiter-Stufen zu, fällt die Prüfung durch zu den normalen
Ziel-SoC-Optionen (Option 18/19).

---

#### Option 18 — Dynamisch laden wenn SoC zwischen MinSOC und Ziel-SoC (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC > MinSOC UND SoC < `sensor.opti_target_soc` **− 3 %**, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |

**Warum:** Tagsüber soll der Akku progressiv auf den intelligenten Ziel-SoC geladen werden.
Wie `sensor.opti_target_soc` diesen Zielwert herleitet (Restprognose, `ratio`-Stufen, Hysterese),
ist im Abschnitt **[Der intelligente Ziel-SoC](#der-intelligente-ziel-soc--herzstück-der-akkuschonung)**
erklärt. Das **−3 %-Band** (H) verhindert Modus-Pendeln direkt an der Ziel-Kante; innerhalb
±3 % um das Ziel greift der Default (Dynamisch).

---

#### Option 19 — Nur Entladen wenn SoC über Ziel-SoC

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC > `sensor.opti_target_soc` **+ 3 %** |
| Preis | irrelevant |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Entladen** |

**Warum:** Ist der Akku bereits über dem intelligenten Ziel-SoC, hat er genug Reserve
für die Nacht. Weiteres Laden aus dem Netz wäre Verschwendung — stattdessen wird der
Überschuss verbraucht bzw. eingespeist. Das **+3 %-Band** (H) sorgt zusammen mit der
Ziel-SoC-Hysterese dafür, dass der Modus an der Grenze nicht flattert.

---

#### Default — Akku Dynamisch

Trifft keine der 20 Optionen zu (z. B. nachts ohne Ladegrund, Preis zu hoch),
wird der Modus auf **Akku Dynamisch** gesetzt. Der Adapter entscheidet dann
situationsabhängig, ob leicht geladen oder entladen wird — ein sicherer Mittelweg.

Der Default-Pfad greift nur wenn `sensor.opti_soc` und `sensor.opti_battery_capacity_kwh`
verfügbar sind — Fail-safe bei unavailable Quellen.

---

### Aktionsblock 2 — Cleanup: Netzladen-Booster deaktivieren bei vollem Akku

**Was passiert:** Wenn SoC > 99 % und der manuelle Netzlade-Booster
(`input_boolean.hausakku_aus_netz_laden`) aktiv ist, wird dieser automatisch deaktiviert.
Außerdem wird der Ladepreis-Helfer (`input_number.ladepreis`) auf den aktuellen Strompreis
gesetzt (Einheit: EUR, `ct/kWh ÷ 100`) und der Modus auf „Akku nur Laden" geschaltet.

**Warum:** Ein aktivierter Netzladen-Booster bei vollem Akku wäre sinnlos. Dieser Block
putzt den Zustand automatisch auf — er läuft immer (kein Toggle-Gate).

---

## Welcher Modus wann — Kurzübersicht

| Modus | Typische Situation |
|---|---|
| **Akku nur Laden** | SoC unter MinSOC (Notfall); schlechte Prognose + günstiger Strom; Wintermodus aktiv; Akku fast leer bei Schlechtwetter; Peak-Leiter L3/L4 (halten); Balancing-Watchdog PV-Vollladung (tagsüber) |
| **Akku Netzladen** | Negativpreis-Laderegel, Peak-Vorladeregel oder Balancing-Watchdog (nachts) aktiv — erzwungenes dynamisches Netzladen (BatChaMinW = `opti_charge_power_w`); braucht `ha-modbus-akku-adapter` >= v1.5.0 |
| **Akku Dynamisch** | PV-Überschuss tagsüber; Akku zwischen MinSOC und Ziel-SoC; voller Akku; kein klarer Lade-/Entladegrund (Default) |
| **Akku nur Entladen** | SoC über intelligentem Ziel-SoC (`sensor.opti_target_soc`); **Ladedeckel `maxsoc` erreicht** (harte Obergrenze, kein Balancing/Peak fällig); Peak-Leiter L1/L2 (entladen) |

**Modus-Contract (Single-Writer-Regel):**
Die Strategie-Automation schreibt primär `input_select.akkusteuerung_modus`. Im
Cleanup-Block (Aktionsblock 2) werden zusätzlich `input_boolean.hausakku_aus_netz_laden`
und `input_number.ladepreis` gesetzt. Was der Modus am Wechselrichter/Speicher auslöst,
entscheidet allein der Hardware-Adapter (Blueprint im Repo `ha-modbus-akku-adapter`).
Nur eine Automation darf gleichzeitig via Modbus schreiben — keine zweite
Steuer-Automation parallel aktiv lassen.

---

## Bausteine des Canonical-Layers

| Baustein | Beschreibung |
|---|---|
| **P10-Sicherheitsnetz** | `sensor.opti_forecast_score`, `_tomorrow` und `sensor.opti_target_soc` verwenden das 10. Perzentil der Solcast-Prognose (`estimate10`) als konservativen Referenzwert — schützt vor Überoptimismus bei unsicheren Prognosen. Der Grad ist per `input_number.opti_forecast_optimismus` einstellbar: 0 = `min(median, P10)` wie bisher, 100 = reiner Median. Der Regler wirkt konsistent auf **beide** Scores: heute (`opti_forecast_score` + `opti_target_soc`, über den zentralen Sensor `opti_forecast_effective_remaining_kwh`) **und** morgen (`opti_forecast_score_tomorrow` nutzt denselben Blend). Letzteres ist wichtig, weil `score_tomorrow` abends den Peak-Reserve-Horizont steuert - sonst hielte die Reserve nachts bei pessimistischem Solcast-P10 unnötig Ladung. |
| **Score-Abendfallback** | `sensor.opti_forecast_score` zählt nach dem heutigen Sonnenuntergang den Morgen-Score (`sensor.opti_forecast_score_tomorrow`), falls verfügbar, statt jeden Abend auf 0 zu fallen.<br>Ohne diesen Fallback degradieren die "heute schlecht"-Blöcke der Strategie abends zu reinen Preis-Bedingungen, egal wie sonnig der nächste Tag wird.<br>Fehlt der Morgen-Score, bleibt die alte Formel als Fallback (best-effort, keine Availability-Kopplung).<br>Der Verbrauchsanteil in `opti_forecast_score`, `_tomorrow` und `opti_target_soc` nutzt außerdem den geglätteten 60-min-Mittelwert (`sensor.opti_house_consumption_60min_w`) statt des Momentanwerts, damit kurze Lastspitzen (z. B. ein Wasserkocher) den Score nicht minütlich auf 0 kippen und Modus-Flips auslösen. |
| **Ziel-SoC-Hysterese** | `sensor.opti_target_soc` hält die aktuelle `ratio`-Stufe im Attribut `level` (Schmitt-Trigger, Marge 0.10) → kein Flattern der Zielstufe. Siehe [Der intelligente Ziel-SoC](#der-intelligente-ziel-soc--herzstück-der-akkuschonung) |
| **Decision-Trace-Attribute** | `sensor.opti_target_soc` hängt Debugging-Attribute an (`branch`, `level`, `ratio`, `net_available_kwh`, `remaining_hours`), lesbar über HA-Entwicklerwerkzeuge |
| **Forecast-Score-Bänder** | `sensor.opti_charge_power_w` variiert die C-Rate in drei Bändern (score ≤ 1: aggressiv; 2–4: moderat; ≥ 5: schonend) statt starrer Prognose-Labels |
| **`sensor.opti_price_level`** | Anbieter-agnostisches Preisniveau-Enum (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE / VERY_EXPENSIVE) auf Basis eines gleitenden Perzentils über `today`/`tomorrow`-Preislisten |
| **Midrank-Perzentil** | `sensor.opti_price_level` zählt Preis-Gleichstände seit dem Fix nur noch halb (statt sie wie ein `select('le')` komplett auf die teure Seite zu zählen) - flache Preistage landen dadurch bei NORMAL statt fälschlich bei VERY_EXPENSIVE. Dieselbe Klassifikation nutzt auch `sensor.opti_peak_reserve_soc` |
| **`sensor.opti_peak_reserve_soc`** | Reserve-SoC für kommende Preisspitzen im Wiederauflade-Horizont (36h); steuert die Peak-Leiter L1-L4. Siehe [Entlade-Peak-Allokation](#entlade-peak-allokation-reserve-für-die-teuersten-stunden) |
| **`binary_sensor.opti_winter_charging_allowed`** | Fail-open Gate für Winterladeblöcke (Standard: `true`); kann mit eigenem Sommermodus-Sensor überschrieben werden |
| **`sensor.opti_balancing_watchdog`** | Balancing-/Deep-Charge-Watchdog (`aus`/`pv`/`netz`): erzwingt einen Voll-Zyklus, wenn der Akku zu lange nicht ~voll war (BMS-Balancing). Rein abgeleitet aus `counter.tage_seit_akku100`, SoC und den `opti_balancing_*`-Helfern → restart-durabel. Siehe [Balancing-/Deep-Charge-Watchdog](#balancing-deep-charge-watchdog) |
