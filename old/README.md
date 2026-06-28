# Legacy-Flachdateien

Diese Dateien sind die alten „Flachdateien" aus dem Repo-Root — überholt durch die neue
`packages/`-Struktur plus `automations/opti_strategie.yaml`.

Sie dienen ausschließlich als **Referenz** und werden nicht mehr aktiv gepflegt.

## Empfohlener Weg

Siehe [Schnell-Nachbau über Packages](../README.md#schnell-nachbau-über-packages-empfohlen)
im Haupt-README.

## Dateien in diesem Ordner

| Datei | Beschreibung |
|---|---|
| `configuration.yaml` | Modbus-Konfiguration zum WR (ersetzt durch `packages/sma_modbus.yaml`) |
| `opti-automatik.yaml` | Prognosebasierte Opti-Automatik (ersetzt durch `automations/opti_strategie.yaml`) |
| `templates.yaml` | Template-Sensoren (ersetzt durch `packages/sma_templates.yaml`) |
| `statistik.yaml` | Gleitende Mittelwert-Sensoren (ersetzt durch `packages/sma_statistik.yaml`) |
| `sma-se-akku-steuerung.yaml` | Manuelle Steuerautomatik (Hardware-Adapter jetzt im Repo `ha-modbus-akku-adapter`) |

## old_legacy/

Der Unterordner `../old_legacy/` enthält noch ältere, ungenutzte Stände (SMA Grid Guard Code
Ära) — nur für Archivzwecke, nutzt niemand mehr aktiv.
