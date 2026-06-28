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

Zentrales Entitäts-Modell (Canonical-`opti_*`-Layer):

- `sensor.opti_soc` — aktueller Speicher-SoC (aus `opti_mapping.yaml` abgeleitet)
- `sensor.opti_forecast_score` / `sensor.opti_forecast_score_tomorrow` — PV-Fit heute/morgen (0–10)
- `sensor.opti_price_level` — aktuelles Preisniveau (VERY_CHEAP/CHEAP/NORMAL/EXPENSIVE/VERY_EXPENSIVE), anbieter-agnostisch
- `sensor.opti_target_soc` — intelligenter Ziel-SoC (aus Restprognose + Hausverbrauch)
- `input_number.minsoc` / `input_number.maxsoc` — konfigurierbare SoC-Grenzen
- `input_boolean.opti_prognose_netzladen` — Gate für prognosebasiertes Netzladen
- `input_boolean.opti_pv_ueberschuss_ladung` — Gate für PV-Überschuss-Laden
- `binary_sensor.opti_winter_charging_allowed` — Winterladefreigabe (Standard: `true`)
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
| Laden wenn heute+morgen schlecht, PV-Rest, Winter, < 80 % | < 80 %  | bis EXPENSIVE     | heute UND morgen schlecht, PV-Rest < 15 kWh, `opti_winter_charging_allowed` = on |
| Laden wenn heute schlecht, PV-Rest, SOC < 15 %       | < 15 %      | bis EXPENSIVE     | heute schlecht, PV-Rest < 20 kWh |
| Laden wenn heute schlecht, PV-Rest, SOC < 45 %, günstig | < 45 %   | nur CHEAP/VERY_CHEAP | heute schlecht, PV-Rest < 20 kWh |

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
Der wichtigste ist „Zwischen Speicherszenarien wählen" mit 11 Optionen und einem Default-Pfad.

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
| Bedingung | SoC < 20 %, `opti_forecast_score` ≤ 2, `opti_forecast_score_tomorrow` ≤ 2 |
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
| Bedingung | SoC < 75 %, `opti_forecast_score` ≤ 2, `opti_forecast_score_tomorrow` ≤ 2 |
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
| Bedingung | SoC < 80 %, PV-Restproduktion heute < 15 kWh, `opti_forecast_score` ≤ 2 + `opti_forecast_score_tomorrow` ≤ 2, `binary_sensor.opti_winter_charging_allowed` = on |
| Preis | bis EXPENSIVE (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE) |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Dieser Block ist der „Wintermodus-Booster". Er greift wenn die PV fast
nichts mehr liefert (< 15 kWh verbleibend), beide Tage schlecht sind und die Winterladefreigabe
aktiv ist — dann wird aggressiver bis 80 % geladen, auch bei EXPENSIVE-Preisen.

**Hinweis:** `binary_sensor.opti_winter_charging_allowed` ist in `opti_derived.yaml`
als fail-open Gate definiert (Standard: immer `true`). Es kann mit einem eigenen
Sommermodus-Schalter überschrieben werden, wenn ein saisonales Gate gewünscht ist.

---

#### Option 5 — Laden wenn heute schlecht, wenig PV-Rest, SoC < 15 % (Tag und Nacht)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC < 15 %, PV-Rest heute < 20 kWh, `opti_forecast_score` ≤ 2 |
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
| Bedingung | SoC < 45 %, PV-Rest heute < 20 kWh, `opti_forecast_score` ≤ 2 |
| Preis | nur VERY_CHEAP / CHEAP |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Laden** |

**Warum:** Opportunistisches Laden bei sehr günstigen Preisen. Hier wird nicht aus
Notwendigkeit nachgeladen, sondern weil Strom gerade besonders billig ist. Dafür ist
die Preisbedingung strenger (kein NORMAL, kein EXPENSIVE) — nur wirklich günstige
Stunden rechtfertigen das Netzladen bei diesem SoC-Niveau.

---

#### Option 7 — Bei 70%-Überschuss laden (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `sensor.opti_grid_export_w` über der konfigurierten 70%-Grenze, SoC < 100 %, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |
| Gate | `input_boolean.opti_pv_ueberschuss_ladung` muss `on` sein |

**Warum:** Sobald die PV mehr ins Netz einspeist als die konfigurierte 70%-Grenze erlaubt,
soll dieser Überschuss in den Akku. „Dynamisch" gibt dem Adapter die Freiheit, genau die
richtige Ladeleistung zu wählen.

---

#### Option 8 — Bei AC-Überschuss laden (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | `sensor.opti_pv_power_w` über der konfigurierten WR-Nennleistungsgrenze, SoC < 100 %, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |
| Gate | `input_boolean.opti_pv_ueberschuss_ladung` muss `on` sein |

**Warum:** Ähnlich wie Option 7, aber die Messgröße ist die WR-AC-Ausgangsleistung statt
der Netzeinspeisung. Nutzt verfügbare PV-Energie aktiv, bevor sie verschwendet wird.

---

#### Option 9 — Bei vollem Akku auf Dynamisch schalten

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

#### Option 10 — Dynamisch laden wenn SoC zwischen MinSOC und Ziel-SoC (nur tagsüber)

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC > MinSOC UND SoC < `sensor.opti_target_soc`, tagsüber |
| Preis | irrelevant |
| Tageszeit | nach Sonnenaufgang bis Sonnenuntergang |
| Gesetzter Modus | **Akku Dynamisch** |

**Warum:** Tagsüber soll der Akku progressiv auf den intelligenten Ziel-SoC geladen werden.
`sensor.opti_target_soc` berechnet diesen Zielwert aus der Solcast-Restprognose
und dem geschätzten Hausverbrauch bis Sonnenuntergang. Je weniger PV noch erwartet wird,
desto höher der Ziel-SoC. Bausteine: P10-Sicherheitsnetz (10. Perzentil der Prognose als
konservativer Referenzwert) und Decision-Trace-Attribute für Debugging.

---

#### Option 11 — Nur Entladen wenn SoC über Ziel-SoC

| Eigenschaft | Wert |
|---|---|
| Bedingung | SoC > `sensor.opti_target_soc` |
| Preis | irrelevant |
| Tageszeit | jederzeit |
| Gesetzter Modus | **Akku nur Entladen** |

**Warum:** Ist der Akku bereits über dem intelligenten Ziel-SoC, hat er genug Reserve
für die Nacht. Weiteres Laden aus dem Netz wäre Verschwendung — stattdessen wird der
Überschuss verbraucht bzw. eingespeist.

---

#### Default — Akku Dynamisch

Trifft keine der 11 Optionen zu (z. B. nachts ohne Ladegrund, Preis zu hoch),
wird der Modus auf **Akku Dynamisch** gesetzt. Der Adapter entscheidet dann
situationsabhängig, ob leicht geladen oder entladen wird — ein sicherer Mittelweg.

Der Default-Pfad greift nur wenn `sensor.opti_soc` und `sensor.opti_battery_capacity_kwh`
verfügbar sind — Fail-safe bei unavailable Quellen.

---

### Aktionsblock 3 — Cleanup: Netzladen-Booster deaktivieren bei vollem Akku

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
| **Akku nur Laden** | SoC unter MinSOC (Notfall); schlechte Prognose + günstiger Strom; Wintermodus aktiv; Akku fast leer bei Schlechtwetter |
| **Akku Dynamisch** | PV-Überschuss tagsüber; Akku zwischen MinSOC und Ziel-SoC; voller Akku; kein klarer Lade-/Entladegrund (Default) |
| **Akku nur Entladen** | SoC über intelligentem Ziel-SoC (`sensor.opti_target_soc`) — Akku hat genug Reserve für die Nacht |

**Modus-Contract (Single-Writer-Regel):**
Die Strategie-Automation schreibt primär `input_select.akkusteuerung_modus`. Im
Cleanup-Block (Aktionsblock 3) werden zusätzlich `input_boolean.hausakku_aus_netz_laden`
und `input_number.ladepreis` gesetzt. Was der Modus am Wechselrichter/Speicher auslöst,
entscheidet allein der Hardware-Adapter (Blueprint im Repo `ha-modbus-akku-adapter`).
Nur eine Automation darf gleichzeitig via Modbus schreiben — keine zweite
Steuer-Automation parallel aktiv lassen.

---

## Bausteine des Canonical-Layers

| Baustein | Beschreibung |
|---|---|
| **P10-Sicherheitsnetz** | `sensor.opti_forecast_score` / `_tomorrow` verwenden das 10. Perzentil der Solcast-Prognose (`estimate10`) als konservativen Referenzwert — schützt vor Überoptimismus bei unsicheren Prognosen |
| **Decision-Trace-Attribute** | `sensor.opti_target_soc` hängt Debugging-Attribute an (`branch`, `ratio`, `net_available_kwh`, `remaining_hours`), lesbar über HA-Entwicklerwerkzeuge |
| **Forecast-Score-Bänder** | `sensor.opti_charge_power_w` variiert die C-Rate in drei Bändern (score ≤ 1: aggressiv; 2–4: moderat; ≥ 5: schonend) statt starrer Prognose-Labels |
| **`sensor.opti_price_level`** | Anbieter-agnostisches Preisniveau-Enum (VERY_CHEAP / CHEAP / NORMAL / EXPENSIVE / VERY_EXPENSIVE) auf Basis eines gleitenden Perzentils über `today`/`tomorrow`-Preislisten |
| **`binary_sensor.opti_winter_charging_allowed`** | Fail-open Gate für Winterladeblöcke (Standard: `true`); kann mit eigenem Sommermodus-Sensor überschrieben werden |
