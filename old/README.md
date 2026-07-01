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

## Konzepte (Legacy-Namen, old/templates.yaml)

Diese Erklärungen gehören zu den Sensoren in `templates.yaml` (Legacy). Die aktuelle,
kanonische Entsprechung findet sich in
[docs/canonical-layer.md](../docs/canonical-layer.md) (Alt↔Neu-Mapping-Tabelle).

### Welcher Sensor ist `sensor.betriebsstatus_sma_stp_se_10_0`?

Das ist der Betriebsstatus-Sensor aus der Modbus-Konfiguration. In der mitgelieferten `configuration.yaml` heißt er `sensor.sma_stp_se_33003_betriebsstatus` (Adresse 33003). Ältere Versionen dieses Repos nutzten noch den anderen Namen – bitte in der Automation entsprechend anpassen.

---

### Woher kommt `sensor.akkusteuerung_dynamische_ladestaerke`?

Dieser Template-Sensor ist in `templates.yaml` definiert und berechnet die optimale Ladestärke anhand von **Akku-SoC** und **Temperatur** – abgestimmt auf BYD LiFePO4-Chemie:

| SoC | C-Rate | Begründung |
|---|---|---|
| < 30 % | 0.5C | Schnell laden bei kritisch niedrigem SoC |
| 30–60 % | 0.3C | Optimale Langlebigkeit |
| 60–85 % | 0.2C | Ausgewogen |
| 85–MaxSoC | 0.1C | Schonend bei hohem SoC |
| > MaxSoC | 0.05C | Minimal |
| > 45 °C oder < 0 °C | reduziert/0 | Temperaturschutz |

---

### Was ist `sensor.akku_target_soc_intelligent`?

Berechnet anhand der **verbleibenden Solcast-Prognose** und dem geschätzten **Hausverbrauch bis Sonnenuntergang**, wie weit der Akku *jetzt* geladen werden sollte. Je weniger PV-Produktion noch zu erwarten ist, desto höher der Ziel-SoC:

| Verh. Restproduktion / Akkukapazität | Ziel-SoC |
|---|---|
| > 3× | 50 % |
| 2–3× | 60 % |
| 1.5–2× | 70 % |
| 1–1.5× | 80 % |
| 0.5–1× | 90 % |
| < 0.5× | MaxSoC |

---

### Was ist der Unterschied zwischen Ladestärke, min/max Ladestärke?

| Helfer | Wann aktiv | Beschreibung |
|---|---|---|
| `akkusteuerung_ladestaerke_soll` | Modus "Schnell Laden" | Feste Ziel-Ladestärke für den manuellen Modus |
| _0.2C Laden_ (kein Helfer) | Modus "0.2C Laden" | Ladeleistung wird vom Hardware-Adapter automatisch aus der Batteriekapazität berechnet (0,2 × Kapazität) |
| `akkusteuerung_min_ladestaerke` | Immer (Dynamisch-Betrieb) | Untere Grenze, die der WR nie unterschreiten soll |
| `akkusteuerung_max_ladestaerke` | Immer (Dynamisch-Betrieb) | Obere Grenze – wird durch dynamische Ladestärke weiter begrenzt |
| `sensor.akkusteuerung_dynamische_ladestaerke` | Immer (Dynamisch-Betrieb) | Automatisch berechneter Sollwert (SoC + Temperatur) |

Empfehlung: Min auf `0`, Max auf z.B. `5000`, dann übernimmt die dynamische Berechnung die Feinsteuerung.
