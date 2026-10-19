# Replay-Harness für die Opti-Akku-Strategie

Spielt aufgezeichnete Stundendaten einer Anlage offline durch die echte
`StrategyEngine` (oder eine Vergleichs-Policy) und misst, was ein einfaches
Batteriemodell daraus gemacht hätte: Einspeisung, Netzbezug, Kosten, Zyklen,
Moduswechsel und die Entscheidungsgründe hinter vermeidbarer Einspeisung.
Der Harness ist ein Analysewerkzeug; er ändert weder Engine noch Strategie.

## Datenfluss

```
HA-Statistik-JSON (stündlich, je Monat)
  │  python -m tools.replay import --raw DIR --mapping mapping.json --out series.csv
  ▼
series.csv (ts_local, pv/load/export/import/charge/discharge kWh, soc, price_ct, forecasts)
  │  python -m tools.replay compare --series series.csv --plant plant.json --out-dir out/ \
  │        --policies actual self_consumption strategy@main strategy@HEAD
  ▼
out/records_<policy>.csv   (jeder 15-min-Schritt mit Modus, decision_id, Flüssen, SoC)
out/metrics_<policy>.json  (Kennzahlen je Monat, Übergang Sep/Okt+Mär/Apr, Winter Nov–Feb)
out/report.md              (Vergleichstabellen und Muster vermeidbarer Einspeisung)
```

Policies:

- `actual`: die gemessenen Flüsse unverändert (Referenz, keine Simulation).
- `self_consumption`: immer `Akku Dynamisch` (Überschuss laden, Defizit entladen).
- `strategy[@ref|@pfad]`: echte Engine mit `strategy.json` aus dem Arbeitsbaum,
  einem Git-Ref (`git show <ref>:custom_components/opti_akku/resources/strategy.json`)
  oder einer Datei. Mehrere Refs lassen sich in einem `compare` nebeneinander stellen.

`mapping.json` ordnet Größen den Statistik-Entitäten zu (`change` für Zähler,
`mean`/`min`/`max` für SoC und Preis; `pv_kwh` darf mehrere Zähler summieren,
`price_eur_kwh` kennt `fallback_ct`). `plant.json` liefert Kapazität, minsoc/maxsoc,
Einspeisevergütung, Spreads, Lade-/Entladeleistung und die Schalter
`pv_surplus_charging` / `grid_precharge`; ein optionales `settings`-Objekt
überschreibt beliebige Helfer der Engine (z. B. `input_number.opti_peak_verbrauch_kw`).

## Modus-Semantik im Batteriemodell (wie `sma.py`)

| Modus | Überschuss | Defizit |
|---|---|---|
| Akku Dynamisch | laden bis `opti_charge_power_w` | entladen bis max. Entladeleistung |
| Akku nur Laden | laden | Netzbezug |
| Akku nur Entladen | Einspeisung | entladen |
| Akku Pause | Einspeisung | Netzbezug |
| Akku Netzladen | laden mit `opti_charge_power_w` aus PV+Netz, Rest einspeisen | laden aus dem Netz |

Verluste je Richtung 0,95 (`--eta`), SoC-Grenzen minsoc/maxsoc als harte Grenzen.
Jeder Schritt erfüllt `pv + import + discharge = load + export + charge`.

## Annahmen (bewusst einfach, im Bericht sichtbar)

- **Stundenauflösung**: Jede Stunde wird gleichmäßig auf vier Viertelstunden verteilt.
  Reale Spitzen innerhalb der Stunde werden geglättet; die Simulation unterschätzt
  deshalb Einspeisung und Netzbezug gegenüber `actual`. Policies untereinander
  vergleichen, nicht Simulation gegen Messung.
- **Hausverbrauch** aus der Bilanz `load = pv + import − export − charge + discharge`;
  der Import-Summary meldet die Abweichung zum Hauszähler.
- **Forecast-Rekonstruktion**: Die Solcast-Tagesprognosen liegen nur als
  stündliche `change`-Werte vor (state_class `total`). Der Pegel ist
  `Anker + kumulierte Änderung`; der Anker ist der Median der Abweichung gegen den
  tatsächlichen Tagesertrag (heute) bzw. die Kopplung an die Heute-Prognose des
  Folgetags nach dem Mitternachtswechsel (morgen). Anker und Medianfehler stehen
  im Import-Summary; Werte werden auf ≥ 0 geklemmt und als `forecast_reconstructed`
  markiert. Der Wert am Stundenende gilt für die ganze Stunde (≤ 1 h Vorgriff auf
  Prognose-Aktualisierungen).
- **Restprognose** = Tagesprognose × Anteil des noch ausstehenden tatsächlichen
  PV-Ertrags des Tages (bei 0 kWh Tagesertrag: Tagesprognose bis 16 Uhr, danach 0).
  `estimate10` wird nicht geliefert; die Engine fällt auf den Median zurück.
- **Preise**: Stundenmittel; heute vollständig bekannt, morgen ab 13 Uhr. Tage mit
  fehlenden Stunden bekommen keine Preisreihe (Engine läuft ohne Preisniveau).
  Lücken ≤ 3 h werden linear interpoliert und als `interpolated` markiert, längere
  bleiben `missing`; Kosten zählen nur Schritte mit Preis.
- **Live-Messwerte** für die Engine (PV, Hausverbrauch, Netz, Akku) sind die
  Leistungen des vorherigen Simulationsschritts; der SoC ist der simulierte.
  Batterietemperatur fest 25 °C, keine EV-, Zellspreizungs- oder Lastprofil-Eingänge.
- **Sonnenstand** über `astral` (Standard 51° N, 10° O, `--latitude/--longitude`),
  ohne astral eine grobe Monatstabelle; der Bericht nennt die Quelle.
- **Engine-Zustand** (Latches, Hysteresen, Statistik) bleibt über alle Schritte in
  einer Instanz erhalten. Debounce-Zeiten von 30/60 s wirken im 15-min-Raster als
  ein Schritt Verzögerung. Balancing-Watchdog bleibt per Default aus.
- **Vermeidbare Einspeisung**: Export bei SoC < maxsoc − 1 % zu Schrittbeginn,
  begrenzt auf die im Schritt ungenutzte maximale Ladeleistung der Anlage. Für
  `actual` steht das Stundenmaximum des SoC für den Schritt-SoC.

## Grenzen

- Kein Modell für Wechselrichter-Abregelung, SMA-Settling nach Moduswechsel,
  Lade-/Entladekurven am Rand oder Temperaturderating.
- Keine Netzlade-Preisunterscheidung über Netzentgelte hinaus (Preisreihe = Arbeitspreis).
- Der `oracle`-Modus (perfekte Voraussicht) ist nicht enthalten.

## Datenschutz

Messdaten, Mapping mit Entitäts-IDs und Anlagenparameter bleiben lokal
(z. B. `~/Dev/sandbox/opti-replay-data/`) und gehören nicht ins Repository.
Tests arbeiten ausschließlich mit synthetischen Daten.
