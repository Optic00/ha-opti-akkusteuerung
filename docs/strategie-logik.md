# Strategie-Logik: Akku Lade- und Entladesteuerung Opti 2.0

## Überblick

Die Automation `Akku Lade- Entladesteuerung Opti 2.0` trifft **ausschließlich Modus-Entscheidungen**.
Sie schreibt einen Wert in `input_select.akkusteuerung_modus` — und nur das.
Welche Hardware-Befehle dieser Modus am Wechselrichter/Speicher auslöst, steuert ein separater
Hardware-Adapter (ein eigener Automatisierungszweig). Diese Trennung macht die Strategie-Logik
unabhängig vom konkreten Speicherfabrikat.

Die Automation läuft im Modus `single`: nur eine Instanz gleichzeitig, keine Parallelausführung.
Trigger sind Preisniveau-Änderungen, BYD/SOC-Änderungen, PV-Prognose-Bewertungsänderungen
und weitere Zustandswechsel relevanter Helfer.

Zentrales Entitäts-Modell (Platzhalternamen, an das eigene System anpassen):

- `sensor.sn_SERIENNUMMER_battery_soc_total` — aktueller Speicher-SoC
- `sensor.pv_forecast_bewertung_heute` / `_morgen` — Prognose-Bewertung (Kritisch/Mangelhaft/Normal/…)
- `sensor.strompreis_niveau` — aktuelles Preislevel (VERY_CHEAP/CHEAP/NORMAL/EXPENSIVE/VERY_EXPENSIVE), anbieter-agnostisch (optional: Tibber o. ä. als Datenquelle dahinter)
- `input_number.minsoc` / `input_number.maxsoc` — konfigurierbare Grenzen
- `input_select.akkusteuerung_modus` — Ziel-Ausgang dieser Automation

---

## Sicherheit zuerst: MinSOC-Schutz

Die **erste** `choose`-Option im Block „Zwischen Speicherszenarien wählen" ist die Entladesperre:

```
Bedingung: sensor.sn_SERIENNUMMER_battery_soc_total below input_number.minsoc
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
| Laden wenn heute+morgen schlecht, PV-Rest, Winter, < 80 % | < 80 %  | bis EXPENSIVE     | heute UND morgen schlecht, PV-Rest < 15 kWh, kein Sommermodus |
| Laden wenn heute schlecht, PV-Rest, SOC < 15 %       | < 15 %      | bis EXPENSIVE     | heute schlecht, PV-Rest < 20 kWh |
| Laden wenn heute schlecht, PV-Rest, SOC < 45 %, günstig | < 45 %   | nur CHEAP/VERY_CHEAP | heute schlecht, PV-Rest < 20 kWh |

Nachgelagert gibt es außerdem den eigenständigen Aktionsblock:
**„Akku Nur Laden wenn Prognose schlecht und aufsparen bei CHEAP & VERY_CHEAP & NORMAL"**
— dieser greift auch dann, wenn die `choose`-Blöcke oben keinen `stop` ausgelöst haben.

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

## Block-für-Block-Übersicht

Die Automation besteht aus mehreren Aktionsblöcken, die **sequenziell** ausgeführt werden.
Der wichtigste ist „Zwischen Speicherszenarien wählen" mit 12 Optionen und einem Default-Pfad.

---

### Aktionsblock 1 — Counter-Reset bei 100 % SoC

**Was passiert:** Sobald der Trigger „Akku ist voll" (SoC > 99 %) feuert, wird der Zähler
`counter.tage_seit_akku100` auf null zurückgesetzt. Dieser Zähler läuft täglich hoch und
zeigt, wie lange der Akku nicht mehr vollgeladen war — nützlich für Wartungsplanung und
spätere Automatisierungen, die einen Voll-Zyklus erzwingen können.

**Modus-Auswirkung:** Keine direkte Modus-Änderung — nur Counter-Reset.

---

### Aktionsblock 2 — „Zwischen Speicherszenarien wählen" (12 Optionen + Default)

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

#### Option 2 — Laden wenn heute + morgen schlecht, SoC < 20 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 20 %, heute Kritisch/Mangelhaft, morgen Kritisch/Mangelhaft |
| Preis | bis EXPENSIVE (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum (Nuance):** Bei sehr leerem Akku (< 20 %) und schlechter Prognose für beide Tage
wird Netzladen auch bei EXPENSIVE-Preisen erlaubt. Die Ratio: Ein fast leerer Akku bei zwei
aufeinanderfolgenden Schlechtwettertagen ist eine Notfallsituation — da ist auch teurer Strom
noch besser als kein Puffer. Diese Ausnahmeregelung gilt nur für diese kritische SOC-Schwelle.

---

#### Option 3 — Laden wenn heute + morgen schlecht, SoC < 75 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 75 %, heute Kritisch/Mangelhaft, morgen Kritisch/Mangelhaft |
| Preis | bis NORMAL (VERY_CHEAP / CHEAP / NORMAL — kein EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum (Nuance):** Bei moderatem SoC (20–75 %) und zwei schlechten Prognosetagen wird
bis NORMAL-Preis nachgeladen. Der Grenze zu EXPENSIVE bleibt verschlossen, weil der Akku
noch nicht kritisch leer ist — es lohnt sich, auf günstigere Stunden zu warten.
Dieser Block bildet zusammen mit Option 2 eine bewusste Abstufung: je leerer der Akku,
desto teureren Strom darf die Automatik akzeptieren.

---

#### Option 4 — Laden wenn heute + morgen schlecht, PV-Rest < 15 kWh, Wintermodus, SoC < 80 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 80 %, PV-Restproduktion heute < 15 kWh, beide Tage Kritisch/Mangelhaft, `binary_sensor.DEIN_SOMMERMODUS_GATE` = off |
| Preis | bis EXPENSIVE (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Dieser Block ist der „Wintermodus-Booster". Er greift wenn die PV fast
nichts mehr liefert (< 15 kWh verbleibend), beide Tage schlecht sind, und der Sommermodus
deaktiviert ist — dann wird aggressiver bis 80 % geladen, auch bei EXPENSIVE-Preisen.

**Wichtiger Hinweis für Nachbauer:** Das Gate `binary_sensor.DEIN_SOMMERMODUS_GATE` muss
als tatsächliche Entität in deinem HA existieren (z. B. ein `input_boolean`). Fehlt diese
Entität, wertet HA die Bedingung als `false` — der Block **greift dann nie**, auch nicht
im Winter. Entweder die Bedingung durch eine eigene Entität ersetzen oder den Gate-Check
aus der YAML entfernen, wenn kein Sommermodus-Schalter vorhanden ist.

---

#### Option 5 — Laden wenn heute schlecht, wenig PV-Rest, SoC < 15 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 15 %, PV-Rest heute < 20 kWh, heute Kritisch/Mangelhaft |
| Preis | bis EXPENSIVE (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Ähnlich wie Option 2, aber unabhängig von der Morgen-Prognose. Wenn der Akku
fast leer ist (< 15 %), heute nichts mehr kommt (< 20 kWh Rest) und die heutige Bewertung
schlecht ist, wird notgeladen — ohne Rücksicht auf morgen. Kurzfrist-Schutz.

---

#### Option 6 — Laden wenn heute schlecht, wenig PV-Rest, SoC < 45 %, Strom sehr günstig (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 45 %, PV-Rest heute < 20 kWh, heute Kritisch/Mangelhaft |
| Preis | nur VERY_CHEAP / CHEAP |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Opportunistisches Laden bei sehr günstigen Preisen. Hier wird nicht aus
Notwendigkeit nachgeladen, sondern weil Strom gerade besonders billig ist. Dafür ist
die Preisbedingung strenger (kein NORMAL, kein EXPENSIVE) — nur wirklich günstige
Stunden rechtfertigen das Netzladen bei diesem SoC-Niveau.

---

#### Option 7 — Drohende Abregelung in Akku umleiten (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | Abregelungsleistung (`sensor.akku_abregelungsleistung`) > 100 W, SoC < 100 %, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |

**Warum:** Wenn die PV-Anlage kurz vor der 70%-Einspeisekappung steht und Leistung
verloren gehen würde, wird der Akku auf Dynamisch gesetzt — er nimmt dann den Überschuss
auf, statt ihn am Dach-Limit zu verlieren. „Dynamisch" bedeutet hier: der Hardware-Adapter
kann je nach Situation laden oder entladen, optimiert auf den aktuellen Bedarf.

---

#### Option 8 — Bei 70%-Überschuss laden (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `sensor.ueberschuss_pv_watt` über der konfigurierten 70%-Grenze, SoC < 100 %, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |

**Warum:** Sobald die PV mehr produziert als ins Netz eingespeist werden darf (70%-Kappung),
soll dieser Überschuss in den Akku. „Dynamisch" gibt dem Adapter die Freiheit, genau die
richtige Ladeleistung zu wählen.

---

#### Option 9 — Bei AC-Überschuss laden (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | WR-AC-Ausgangsleistung über der konfigurierten WR-Nennleistungsgrenze, SoC < 100 %, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |

**Warum:** Ähnlich wie Option 8, aber die Messgröße ist die WR-Ausgangsleistung statt
der 70%-Schwelle. Nutzt verfügbare PV-Energie aktiv, bevor sie verschwendet oder gekappt wird.

---

#### Option 10 — Bei vollem Akku auf Dynamisch schalten

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

#### Option 11 — Dynamisch laden wenn SoC zwischen MinSOC und Ziel-SoC (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC > MinSOC UND SoC < `sensor.akku_target_soc_intelligent`, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |

**Warum:** Tagsüber soll der Akku progressiv auf den intelligenten Ziel-SoC geladen werden.
`sensor.akku_target_soc_intelligent` berechnet diesen Zielwert aus der Solcast-Restprognose
und dem geschätzten Hausverbrauch bis Sonnenuntergang. Je weniger PV noch erwartet wird,
desto höher der Ziel-SoC. Neue Bausteine darin: P10-Sicherheitsnetz (10. Perzentil der
Prognose als konservativer Referenzwert) und ein P-Regler für sanfte Übergänge zwischen
den Stufen. An `akku_target_soc_intelligent` hängen Decision-Trace-Attribute, die den
aktuellen Berechnungspfad für Debugging nachvollziehbar machen.

---

#### Option 12 — Nur Entladen wenn SoC über Ziel-SoC

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC > `sensor.akku_target_soc_intelligent` |
| Preis | irrelevant |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Entladen** |

**Warum:** Ist der Akku bereits über dem intelligenten Ziel-SoC, hat er genug Reserve
für die Nacht. Weiteres Laden aus dem Netz wäre Verschwendung — stattdessen wird der
Überschuss verbraucht bzw. eingespeist. (Hinweis: Eine zusätzliche Bedingung
„morgen Hervorragend" ist in der YAML vorhanden, aber derzeit deaktiviert.)

---

#### Default — Akku Dynamisch

Trifft keine der 12 Optionen zu (z. B. nachts, kein Überschuss, kein Ladegrund),
wird der Modus auf **Akku Dynamisch** gesetzt. Der Adapter entscheidet dann
situationsabhängig, ob leicht geladen oder entladen wird — ein sicherer Mittelweg.

---

### Aktionsblock 3 — Netzladen-Booster deaktivieren bei vollem Akku

**Was passiert:** Wenn der Trigger „Akku ist voll" (SoC > 99 %) feuert und der
manuelle Netzlade-Booster (`input_boolean.hausakku_aus_netz_laden`) aktiv ist,
wird dieser automatisch deaktiviert. Außerdem wird der Ladepreis-Helfer
(`input_number.ladepreis`) auf den aktuellen Strompreis gesetzt und der Modus
auf „Akku nur Laden" geschaltet.

**Warum:** Ein aktivierter Netzladen-Booster bei vollem Akku wäre sinnlos und
würde weiter Strom ziehen. Dieser Block putzt das automatisch auf.

---

### Aktionsblock 4 — „Akku Nur Laden wenn Prognose schlecht und aufsparen" (nachgelagert)

Dieser Block läuft **nach** dem großen `choose`-Block und greift auch dann,
wenn oben kein `stop` ausgelöst wurde. Er hat drei interne Unterbedingungen (OR):

1. **Wintermodus-Abend:** SoC < 80 %, PV-Rest < 15 kWh, morgen schlecht, kein Sommermodus
2. **Nachmittags-Puffer (13:00–23:59):** SoC < 30 %, PV-Rest < 20 kWh, morgen schlecht
3. **Nachts/Früh (00:00–12:59):** SoC < 30 %, heute schlecht

Dazu muss das Preisniveau CHEAP / VERY_CHEAP / NORMAL sein.

**Warum:** Dieser Block ist eine zweite Sicherheitsebene für prognosebasiertes Laden.
Er stellt sicher, dass der Akku auch dann nachgeladen wird, wenn der Haupt-`choose`-Block
keinen `stop` gesetzt hat (z. B. weil kein passender Trigger dabei war). Tageszeit-Logik
(13:00-Schwelle) berücksichtigt, dass morgen-Prognosen am Nachmittag relevanter werden
als am frühen Morgen.

---

### Aktionsblock 5 — Legacy Tibber-Preis-Ladesteuerung (deaktiviert)

Dieser Block ist in der YAML vorhanden, aber mit `enabled: false` vollständig deaktiviert.
Er enthält drei Optionen:

- **Netz-Laden wenn Preisspanne lohnt:** Zieht Tibber-spezifische Sensoren heran
  (`sensor.tibber_preisspanne_heute`, `sensor.tibber_aktueller_preis_ist_tageshochstpreis`),
  die in der neuen anbieter-agnostischen Strategie nicht mehr verwendet werden.
- **Akku Automatisch wenn PV gut und Akku hoch (ohne Netzladen)**
- **Akku Automatisch wenn morgen oder heute ausreichend PV**

**Warum deaktiviert:** Im Winter ist diese Logik kaum wirtschaftlich (Kommentar in der YAML).
Sie ist zur Referenz erhalten, wird aber nicht mehr gepflegt. Die neue Preisniveauquelle
`sensor.strompreis_niveau` (anbieter-agnostisches Perzentil-Enum) macht Tibber-spezifische
Sensoren in der Strategie obsolet.

---

## Welcher Modus wann — Kurzübersicht

| Modus | Typische Situation |
|---|---|
| **Akku nur Laden** | SoC unter MinSOC (Notfall); schlechte Prognose + günstiger Strom; Wintermodus aktiv; Akku fast leer bei Schlechtwetter |
| **Akku Dynamisch** | PV-Überschuss tagsüber; Akku zwischen MinSOC und Ziel-SoC; voller Akku; kein klarer Lade-/Entladegrund (Default) |
| **Akku nur Entladen** | SoC über intelligentem Ziel-SoC (Akku hat genug Reserve für die Nacht) |
| **Akku Automatisch** | Nur im deaktivierten Legacy-Tibber-Block — in der aktiven Strategie derzeit nicht vergeben |

**Modus-Contract (Single-Writer-Regel):**
Die Strategie-Automation schreibt ausschließlich in `input_select.akkusteuerung_modus`.
Was dieser Modus am Wechselrichter/Speicher auslöst, entscheidet allein der Hardware-Adapter
(Blueprint im Repo `ha-modbus-akku-adapter`). Nur eine Automation darf gleichzeitig
via Modbus schreiben — keine zweite Steuer-Automation parallel aktiv lassen.

---

## Neue Bausteine (seit Task 5–6)

| Baustein | Beschreibung |
|---|---|
| **P10-Sicherheitsnetz** | `sensor.pv_forecast_bewertung_heute` / `_morgen` verwenden das 10. Perzentil der Solcast-Prognose als konservativen Referenzwert — schützt vor Überoptimismus bei unsicheren Prognosen |
| **Decision-Trace-Attribute** | `akku_target_soc_intelligent` hängt Debugging-Attribute an (`branch`, `ratio`, `net_available_kwh`, `remaining_hours`), lesbar über HA-Entwicklerwerkzeuge |
| **Sollkurve + P-Regler** | `sensor.akkusteuerung_dynamische_ladestaerke_p` ist ein eigenständiger Sensor (P-Regler), der die Basis-Ladestärke proportional zur Abweichung Ist-SOC vs. Sollkurve moduliert — sanfter Übergang statt abrupter Sprünge |
| **Abregelungs-Umleitung** | Trigger + Option 7 erkennen drohende 70%-Kappung und leiten Überschuss aktiv in den Akku (statt Verlust am Dach-Limit) |
| **`sensor.strompreis_niveau`** | Anbieter-agnostisches Preisniveau-Enum (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE / VERY_EXPENSIVE) auf Basis eines gleitenden Perzentils — ersetzt Tibber-spezifische Direktabfragen in der Strategie |
