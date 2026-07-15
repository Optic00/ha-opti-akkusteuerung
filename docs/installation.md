# Installation & Konfiguration (ausführlich)

Der kompakte Happy Path steht in der **[README](../README.md#schnell-start)**.
Diese Seite ist die vollständige Referenz: Komponenten und Reihenfolge, beide Einspiel-Varianten, alle Erststart-Werte und die Watchdog-Konfiguration.
Kein Detail geht verloren - es ist nur aus der README hierher verschoben, damit der Schnellpfad wirklich schnell bleibt.

---

## Komponenten und Reihenfolge

Das Setup besteht aus zwei Repos: der **Strategie** (dieses Repo, hardware-agnostisch) und dem **Hardware-Adapter** ([`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter), spricht Modbus mit dem WR). Was woher kommt:

| Kommt aus | Was | GUI oder YAML |
|---|---|---|
| Adapter-Repo | Modbus-Hub zum WR | YAML (`configuration.yaml`/Package) |
| Adapter-Repo (**oder** Opti-Repo, siehe unten) | Modus-Dropdown + 6 Leistungs-Helfer | GUI oder YAML (Package) |
| Adapter-Repo (**oder** Opti-Repo, siehe unten) | 2 Write-on-Change-Helfer (`input_text`/`input_datetime`) | GUI oder YAML (Package) |
| Adapter-Repo | Blueprint (übersetzt Modus → Modbus) | Blueprint-Import |
| Opti-Repo | `opti_mapping.yaml` (Hardware → kanonische Sensoren) | YAML, von dir ausgefüllt |
| Opti-Repo | `opti_derived.yaml` (Score, Ziel-SoC, Preisniveau) | YAML (Package) |
| Opti-Repo | Strategie-Automation (setzt den Modus) | YAML (editierbar, kein Blueprint) |

**Verbindliche Reihenfolge, wenn du beide Repos zusammen nutzt:**

1. Modbus-Verbindung anlegen (Adapter-Repo, Schritt 1). Helfer NICHT hier anlegen, wenn du Schritt 2 nutzt - siehe Hinweis unten.
2. Opti-Packages aktivieren + `opti_mapping.yaml` ausfüllen (Opti-Repo)
3. Home Assistant neu starten - `sensor.opti_*` prüfen
4. Adapter-Blueprint importieren, Inputs prüfen: `dynamic_charge_strength_sensor` auf `sensor.opti_charge_power_w` setzen, dazu `battery_capacity_sensor`, `inverter_status_sensor` und `inverter_ok_states` auf deine echten Entitäten bzw. Status-Codes (nicht ungeprüft die Blueprint-Vorschlagswerte übernehmen, falls sie abweichen)
5. Strategie-Automation (`automations/opti_strategie.yaml`) aktivieren

> ⚠️ **Helfer nur aus einer Quelle:** Bei kombinierter Nutzung liefert
> `ha-opti-akkusteuerung/packages/sma_helpers.yaml` bereits alle Helfer (Modus-Dropdown,
> 6 Leistungs-Helfer, 2 Write-on-Change-Helfer). Die Adapter-GUI-Anleitung bzw. das
> Adapter-Package dann NICHT zusätzlich verwenden - zwei Packages mit denselben
> Entity-IDs führen zu einem Duplicate-Key-Fehler im HA-Log. Nutzt du den Adapter
> **ohne** das Opti-Repo (eigene Strategie), gilt die Adapter-Anleitung normal.

> ⚠️ **Modbus-Hub nur aus einer Quelle:** Dieselbe Falle gilt für den Modbus-Hub selbst.
> Wer Schritt 1 bereits im Adapter-Repo erledigt hat (eigener `modbus:`-Block in der
> `configuration.yaml`), lässt `packages/sma_modbus.yaml` aus diesem Repo weg - zwei
> `modbus:`-Blöcke mit demselben Hub-Namen führen zum selben Duplicate-Key-Fehler im
> HA-Log.

### Versions-Kompatibilität

| Strategie-Feature | benötigter Adapter-Stand |
|---|---|
| Peak-Allokation / Modus „Akku Netzladen" | [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) >= v1.5.0 |
| Alle übrigen Modi (Automatisch, Dynamisch, Pause, nur Laden, nur Entladen, schnell Laden, schnell Entladen, 0.2C Laden) | [`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) >= v1.2.0 (Write-on-Change-Helfer) |

```
Strategie  →  input_select.akkusteuerung_modus  →  [ ADAPTER-BLUEPRINT ]  →  Modbus-Register  →  WR
(setzt Modus)        (+ input_number.* in W)              übersetzt
```

---

## Schritt für Schritt

**1. Packages aktivieren** (einmalig) in deiner `configuration.yaml`:
```yaml
homeassistant:
  packages: !include_dir_named packages/
```

**2. Hardware-Mapping anlegen:** `opti_mapping.example.yaml` nach `packages/opti_mapping.yaml`
kopieren und alle `DEIN_*`-Platzhalter durch echte Entitäts-IDs ersetzen.
→ Ausführliche Anleitung: **[docs/canonical-layer.md](canonical-layer.md)**

**3. Package-Dateien** aus dem Ordner [`packages/`](../packages/) in dein HA-`packages/`-Verzeichnis kopieren:

| Datei | Inhalt |
|---|---|
| `opti_derived.yaml` | Abgeleitete Entscheidungs-Sensoren (Score, Ziel-SoC, Preisniveau, …) |
| `sma_modbus.yaml` | ⚠️ **nur falls nicht bereits über Adapter-Repo Schritt 1 angelegt** - Modbus-TCP-Verbindung zum WR (nur **IP** anpassen) |
| `sma_helpers.yaml` | alle `input_select`/`input_number`/`input_boolean`/`counter` (Modus, Sollwerte, SoC-Grenzen …) |
| `sma_templates.yaml` | **optional** - nur für Sollkurve-/Abregelungs-Anzeige (Legacy), enthält Platzhalter-Entity-IDs: ersetzen oder ganz weglassen |
| `sma_statistik.yaml` | gleitende Mittelwerte (Verbrauch, Batterielast) |
| `opti_ki_analyse.yaml` | **optional** - täglicher KI-Tagesreport per `ai_task.generate_data`, rein lesend; Details siehe [docs/canonical-layer.md](canonical-layer.md#ki-analyse-schicht-optional-phase-1) |
| `byd_bmu.yaml` | **optional** - BYD-Zell-Monitoring (Spreizung, Temperaturen, Balancing) via bydlogc→MQTT; braucht das BYD-Logger-Tool in Docker/VM, einen MQTT-Broker und ggf. eine Route/SNAT-Regel zur Box → **[docs/byd-bmu-monitoring.md](byd-bmu-monitoring.md)** |
| `opti_ev_sperre.yaml` | **optional** - EV-Schnelllade-Entladesperre (evcc im Modus now/minpv); braucht HACS `evcc_intg` + Ladepunkt-Block im Mapping → [docs/strategie-logik.md](strategie-logik.md) (Option 13) |

**4. Home Assistant neu starten** → Helfer, Templates, Statistik & Modbus sind da.

**5. Sensoren prüfen (Verify-Gate):** Bevor du irgendetwas scharf schaltest, in den Entwicklertools sicherstellen, dass die kanonischen Sensoren plausible Werte liefern und **nicht** `unavailable`/`unknown` sind - v. a. `sensor.opti_target_soc`, `sensor.opti_charge_power_w`, `sensor.opti_price_level`. Stimmt hier etwas nicht, zuerst das Mapping (`opti_mapping.yaml`) korrigieren, nicht weitergehen.

**6. Hardware-Adapter importieren:** Blueprint aus
[`ha-modbus-akku-adapter`](https://github.com/Optic00/ha-modbus-akku-adapter) per Raw-URL
importieren (*Einstellungen → Automatisierungen & Szenen → Blueprints → importieren*) und
beim Anlegen der Automation die Eingaben auf deine Entitäten mappen
(Modbus-Hub, WR-Status-Sensor, Modus-Select `input_select.akkusteuerung_modus`,
dyn. Ladestärke `sensor.opti_charge_power_w`).

**7. Strategie einspielen:** die Opti-Automatik (steuert *welcher Modus wann*) liegt in
`automations/opti_strategie.yaml` - als Top-Level-Liste im Format von `automations.yaml`,
**nicht** als fertiges Package. Sie ist bewusst **editierbar** (kein Blueprint), damit du
sie an deine Anlage/Strategie anpassen kannst. Zwei Wege, sie einzuspielen:

- **(a) An `automations.yaml` anhängen** (einfachster Weg): den kompletten Inhalt von
  `automations/opti_strategie.yaml` ans Ende deiner `automations.yaml` kopieren. HA
  erkennt sie danach als normale Automation, editierbar über die UI.
- **(b) Als eigenes Package speichern:** die Datei nach `packages/opti_strategie.yaml`
  kopieren und mit dem Schlüssel `automation:` wrappen - dann lassen sich zusätzlich
  Optionen wie `initial_state` ergänzen:

  ```yaml
  automation:
    - id: "opti_canonical_strategie"
      alias: "Akku Opti Strategie"
      initial_state: true
      # ... restlicher Inhalt aus automations/opti_strategie.yaml unverändert ...
  ```

  Auf demselben Weg gehört die Datei `automations/opti_balancing_counter.yaml` eingespielt -
  sie enthält **zwei** Automationen, die den Balancing-Watchdog speisen:
  `opti_balancing_counter_increment` (zählt `counter.tage_seit_akku100` täglich um 23:59 hoch)
  und `opti_balancing_counter_reset` (setzt den Zähler auf 0, sobald der Akku 30 min stabil
  über dem Done-SoC steht - numeric_state-Trigger mit `for:`). Ersetzt eine ggf. bereits live
  vorhandene, gleichnamige Increment-Automation.

**8. Erststart-Werte setzen:** `input_number`-Helfer ohne `initial:` starten beim
allerersten Anlegen auf ihrem **Minimum** - bei `maxsoc` und den beiden
Max-Ladestärke-Helfern ist das **0**, was jedes Laden/Entladen blockiert. Nach dem ersten
Anlegen (Schritt 4, HA-Neustart) einmalig über die HA-Oberfläche setzen - danach übersteht
der Wert jeden weiteren Neustart:

| Helfer | Erststart-Wert (Minimum) | Empfohlener Startwert |
|---|---|---|
| `input_number.minsoc` | 0 % | 10 % |
| `input_number.maxsoc` | 0 % | 95 % |
| `input_number.akkusteuerung_max_ladestaerke` | 0 W | 3000 W |
| `input_number.akkusteuerung_max_entladestaerke` | 0 W | 5000 W |
| `input_number.akkusteuerung_ladestaerke_soll` | 100 W | 2000 W |
| `input_number.akkusteuerung_entladestaerke_soll` | 100 W | 2000 W |
| `input_number.akkusteuerung_min_ladestaerke` | 0 W | 0 W |
| `input_number.akkusteuerung_min_entladestaerke` | 0 W | 0 W |
| `input_number.opti_peak_verbrauch_kw` | 0.1 kW | 0.8 kW |
| `input_number.opti_einspeiseverguetung_ct` | 0 ct/kWh | dein eigener EEG-Satz |
| `input_number.opti_netzlade_spread_ct` | 0 ct/kWh | 10 ct/kWh |
| `input_number.opti_peak_min_aufschlag_ct` | 0 ct/kWh | 5 ct/kWh |
| `input_number.opti_halte_spread_ct` | 0 ct/kWh | 5 ct/kWh |
| `input_number.opti_balancing_intervall_tage` | 0 Tage (= Watchdog aus) | 14 Tage |
| `input_number.opti_balancing_karenz_tage` | 0 Tage | 3 Tage |
| `input_number.opti_balancing_max_ct` | 0 ct/kWh (= kein bezahltes Netzladen) | 25 ct/kWh |
| `input_boolean.opti_balancing_netzladen` | aus (= Balancing rein per PV) | nach Wunsch an |

Die `opti_balancing_*`-Helfer steuern den **Balancing-/Deep-Charge-Watchdog**
(`sensor.opti_balancing_watchdog`): `intervall_tage` = Tage ohne Voll-/Done-Ladung bis
der Watchdog fällig wird (0 = aus), `karenz_tage` = zusätzliche Wartezeit vor dem
bezahlten Netz-Fallback, `max_ct` = absoluter Brutto-Preisdeckel fürs bezahlte
Balancing-Netzladen (0 = fail-safe aus). `input_number.opti_balancing_done_soc` hat als
einziger ein `initial:` (98.5 %) und muss nicht von Hand gesetzt werden - er definiert die
„Akku ~voll"-Schwelle für Counter-Reset und tägliches Increment. Der Schalter
`input_boolean.opti_balancing_netzladen` (**Default aus**) entscheidet, ob der Watchdog fürs
BMS-Balancing auch aus dem **Netz** laden darf; ohne ihn balancet er rein per PV. Er ist
bewusst von `opti_prognose_netzladen` entkoppelt, damit Balancing-Netzladen unabhängig vom
allgemeinen Prognose-Netzladen freigegeben werden kann.

**9. Einschalten:** die Strategie-Automation bleibt wirkungslos, solange ihr Master-Schalter
aus ist - und frisch angelegte `input_boolean`-Helfer starten **aus** (kein `initial:`, siehe Schritt 8). Über die HA-Oberfläche auf **an** stellen:

| Helfer | Wirkung |
|---|---|
| `input_boolean.akku_opti_automatik` | Master-Schalter - ohne „an" tut die gesamte Strategie-Automation nichts |
| `input_boolean.opti_prognose_netzladen` | Gate für die prognosebasierten „Akku nur Laden"-Blöcke (Reserve halten bei schlechter PV-Prognose) |
| `input_boolean.opti_pv_ueberschuss_ladung` | Gate für die PV-/AC-Überschussblöcke (Akku Dynamisch bei Einspeise-Überschuss) |

**10. Feinjustieren:** SoC-Grenzen, Lade-/Entladegrenzen, Prognose-Schwellen über die
HA-Oberfläche weiter an die eigene Anlage anpassen (alle als Helfer vorhanden).

> ⚠️ **Single-Writer-Regel:** Nur **eine** Automation darf den WR via Modbus schreiben.
> Wenn du den Adapter-Blueprint nutzt, keine zweite Steuer-Automatik gleichzeitig aktiv lassen.

---

## Legacy-Setup (Referenz)

Der frühere manuelle Weg mit Flachdateien (Modbus-Config, Sensoren, Helfer-Tabelle, Dashboard-Karte) ist umgezogen nach [`old/README.md`](../old/README.md).
Empfohlen bleibt die Package-Struktur oben.

> 💡 Nutzt du noch die alten Sensor-Namen (`akkusteuerung_dynamische_ladestaerke`,
> `akku_target_soc_intelligent`)? Erklärung und Alt↔Neu-Mapping:
> **[old/README.md#konzepte-legacy-namen](../old/README.md#konzepte-legacy-namen-oldtemplatesyaml)**
