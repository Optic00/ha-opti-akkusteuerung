# Design: kanonischen Netzbezugsensor wiederherstellen

Datum: 2026-07-21

## Ausgangslage

Das Dashboard verwendet `sensor.opti_grid_import_w`. Der Sensor ist in der
öffentlichen Mapping-Vorlage beschrieben, fehlt aber im privaten
`packages/opti_mapping.yaml` der Live-Installation und ist deshalb
`unavailable`. Die eigentliche Akkusteuerung verwendet diesen Sensor nicht;
der private SMA-Quellsensor für `metering_power_absorbed` liefert plausible
Werte.

## Ziel

Der kanonische Sensor soll wieder verfügbar sein, den positiven Netzbezug in
Watt anzeigen und rein beobachtend bleiben. Die Akku- und Wechselrichterlogik
wird nicht verändert. Insbesondere wird kein künstlicher Einspeise-Puffer
eingeführt.

## Umsetzung

1. Im privaten Mapping wird ein Template-Sensor mit der bestehenden Unique-ID
   `opti_mapping_grid_import_w` ergänzt.
2. Als Quelle dient der private SMA-Sensor für `metering_power_absorbed` (im
   öffentlichen Beispiel: `sensor.DEIN_GRID_IMPORT`).
3. Die Availability folgt `has_value()` der Quelle; der Zustand wird auf
   mindestens 0 W begrenzt.
4. Ein Regressionstest prüft Existenz, Einheit, Device-/State-Class,
   Availability und die Nichtnegativitäts-/Durchreichungssemantik des privaten
   Mappings.
5. Dieselbe Mapping-Ergänzung wird in Home Assistant eingespielt. Zuerst wird
   die Template-Konfiguration neu geladen; nur falls HA den neuen YAML-Sensor
   dabei nicht registriert, wird Home Assistant kontrolliert neu gestartet.

## Verifikation

- Alle Repository-Tests bestehen.
- `sensor.opti_grid_import_w` ist live verfügbar.
- Bei positivem Netzbezug stimmt sein Wert mit dem SMA-Quellsensor überein.
- Bei 0 W Quelle zeigt er 0 W und niemals einen negativen Wert.
- Bestehende Steuerungsentitäten, Automationen und Modbus-Schreibwerte bleiben
  unverändert.

## Nicht im Umfang

- Kein Umbau der Akkustrategie.
- Kein permanenter Export-Sollwert zur Vermeidung kleinster Bezugsspitzen.
- Keine Änderung des Dashboard-Entity-IDs.
