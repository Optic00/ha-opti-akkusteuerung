# Forecast-Verlauf wiedergeben

`tools/replay_forecast_history.py` vergleicht die tatsächlichen Jinja-Templates
zweier Versionen von `packages/opti_derived.yaml` mit aufgezeichneten
Eingangswerten. Das Werkzeug liest ausschließlich lokale Dateien. Es benötigt
die Entwicklungsabhängigkeiten aus `requirements-dev.txt`.

Echte Exporte und Ergebnisse enthalten private Verbrauchs- und Anlagendaten.
Speichere sie außerhalb des Repositories und veröffentliche sie nicht in PRs
oder Issues. Verwende für öffentliche Fehlerbeispiele synthetische Werte.

## Eingabe

Die JSON-Datei enthält eine Liste von Zeitfenstern. Jedes Element besteht aus
`label` und `data`. `data` ist das Ergebnis von `ha_get_history` mit
`minimal_response=false` und `significant_changes_only=false`. Es braucht:

- `success: true`, `query_params.minimal_response: false` und `period` mit
  `start` und `end` als ISO-Zeitstempel einschließlich Zeitzone;
- `entities` mit je `entity_id`, `has_more: false` und `states`;
- pro Zustand `state`, `last_updated` beziehungsweise `last_changed` sowie
  die zu diesem Zeitpunkt aufgezeichneten `attributes`.

Exportiere für jedes Fenster die folgenden Entitäten, einschließlich ihres
Anfangszustands:

```text
sun.sun
sensor.opti_forecast_today_kwh
sensor.opti_forecast_tomorrow_kwh
sensor.opti_forecast_remaining_today_kwh
sensor.opti_forecast_effective_remaining_kwh
sensor.opti_forecast_score_tomorrow
sensor.opti_forecast_score
sensor.opti_house_consumption_60min_w
sensor.opti_house_consumption_w
sensor.opti_soc
sensor.opti_battery_capacity_kwh
input_number.opti_forecast_optimismus
```

Wenn Recorder die Attribute `next_rising` und `next_setting` von `sun.sun`
nicht aufzeichnet, exportiere zusätzlich `sensor.sun_next_rising` und
`sensor.sun_next_setting`. Das Werkzeug verwendet diese historischen Werte
als Ersatz. Sie können auf ganze Sekunden gerundet sein. Aktuelle Sonnenzeiten
sind kein Ersatz für fehlende historische Werte.

## Aufruf und Aussagekraft

```bash
python tools/replay_forecast_history.py /tmp/forecast-history.json \
  --before /tmp/opti-derived-before.yaml \
  --after packages/opti_derived.yaml \
  --output /tmp/forecast-comparison.json
```

Pro aufgezeichnetem Aktualisierungszeitpunkt und voller Minute berechnet das
Werkzeug beide Versionen. Die Ausgabe enthält den aufgezeichneten Score,
beide berechneten Scores und, sofern in der Version vorhanden, den
Sonnentag-Score und die Horizont-Hysterese. Zeitstempel werden auf der
UTC-Zeitachse sortiert, damit die doppelte Stunde bei der Herbstumstellung
erhalten bleibt.

Abgeschnittene Exporte und Exporte ohne vollständige Attribute werden
abgewiesen. `missing_entities` und `initially_missing_entities` markieren
fehlende Zustände der angefragten Entitäten. Prüfe zusätzlich, ob alle oben
genannten Entitäten überhaupt angefragt wurden. Für die Horizont-Hysterese
wird kein früher gespeicherter Zustand angenommen; bei Score 2 kann ihr
Anfangszustand deshalb von der Anlage abweichen.

Die Wiedergabe berechnet die abhängigen Templates nacheinander bis zum
ausgewerteten Zustand. Sie bildet weder HA-Ereigniswarteschlangen noch
Hardwarebefehle, Akkuströme oder Energieeinsparungen nach. Der effektive
Restforecast und der Morgen-Score kommen aus der Aufzeichnung und werden
nicht neu berechnet. Eine verspätete Datumsumschaltung der Prognosequelle
bleibt eine Grenze der Sensoren: Kurz nach Mitternacht kann ihr Ganztagswert
noch zum Vortag gehören.
