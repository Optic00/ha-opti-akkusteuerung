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
| `byd_monitoring.yaml` | **optional** - BYD-Zell-Monitoring + Akku-Alarme (Spreizung, Temperaturen, Balancing, native Fehlerbits, Daten-Watchdog); braucht die HACS-Integration [`byd_battery_box`](https://github.com/TimWeyand/byd_battery_box) und eine Route/SNAT-Regel zur Box → **[docs/byd-monitoring-nativ.md](byd-monitoring-nativ.md)** |
| `byd_modul2_fruehwarnung.yaml` | **optional** - BYD Modul-2-Frühwarnung (Degradations-Trend des schwächsten Moduls, rein beobachtend, kein Alarm); setzt `byd_monitoring.yaml` voraus → **[docs/byd-modul2-fruehwarnung.md](byd-modul2-fruehwarnung.md)** |
| `opti_ev_sperre.yaml` | **optional** - EV-Schnelllade-Entladesperre (evcc im Modus now/minpv); braucht HACS `evcc_intg` + Ladepunkt-Block im Mapping → [docs/strategie-logik.md](strategie-logik.md) (Option 13) |

**4. Home Assistant neu starten** → Helfer, Templates, Statistik & Modbus sind da.

**5. Sensoren prüfen (Verify-Gate):** Bevor du irgendetwas scharf schaltest, in den Entwicklertools sicherstellen, dass die kanonischen Sensoren plausible Werte liefern und **nicht** `unavailable`/`unknown` sind - v. a. `sensor.opti_target_soc`, `sensor.opti_charge_power_w`, `sensor.opti_price_level`. Stimmt hier etwas nicht, zuerst das Mapping (`opti_mapping.yaml`) korrigieren, nicht weitergehen.

> **Abgrenzung zum Betrieb:** Nach der Inbetriebnahme ist `sensor.opti_price_level` = `unavailable` kein Fehler, sondern gewollt - fällt die Preisquelle aus, meldet der Sensor das ehrlich (fail-closed), statt ein Preisniveau zu erfinden.
> Die preisabhängigen Zweige verstummen dann; die preisunabhängigen (MinSOC-Schutz, Ladedeckel, Überschuss, Ziel-SoC …) laufen unverändert weiter, und nur im Default bleibt ein passiver Modus stehen.
> Ein *dauerhaft* nicht verfügbares Preisniveau hat dagegen eine Ursache, die zu klären ist: fehlendes oder falsches Preis-Mapping, ein Ausfall beim Preis-Anbieter oder ein Netzwerkproblem.
> Wer bewusst **ohne** Preisquelle fährt (siehe [Betrieb ohne Strompreis-Sensor](canonical-layer.md#was-automatisch-wegfällt)), für den ist der Zustand dauerhaft normal.

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
  `opti_balancing_counter_increment` zählt `counter.tage_seit_akku100` um 23:59 nur an
  Tagen ohne bestätigten Abschluss hoch. `opti_balancing_counter_reset` bestätigt
  minutenweise 30 Minuten über dem Done-SoC, setzt den Tageszähler zurück und stempelt
  den Abschluss. Persistente Helfer machen den Ablauf restartfest und verhindern mehrere
  Abschlüsse am selben Tag. Ersetzt eine ggf. bereits live vorhandene, gleichnamige
  Increment-Automation.

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
| `input_number.opti_balancing_spreizungs_schwelle` | 0 mV (= bedarfsgesteuertes Balancing aus) | 35 mV |
| `input_number.opti_balancing_bedarf_cooldown_tage` | 0 Tage | 5 Tage |
| `input_number.akkusteuerung_wr_70proz_ueberschuss_grenze` | 0 W (= Override aus) | deine Einspeise-Abregelgrenze |
| `input_number.akkusteuerung_wr_ac_ueberschuss_grenze` | 0 W (= Override aus) | AC-Nennleistung deines WR |
| `input_number.opti_forecast_optimismus` | 0 % (= konservativ, kann so bleiben) | 40 % |
| `input_number.ladepreis` | -1 EUR/kWh | nichts tun, wird automatisch gefüllt |
| `input_number.mindestpreisdifferenz_lade_entladepreis` | 0 EUR/kWh | 0.08 EUR/kWh |
| `input_boolean.opti_balancing_netzladen` | aus (= Balancing rein per PV) | nach Wunsch an |

Die `opti_balancing_*`-Helfer steuern den **Balancing-/Deep-Charge-Watchdog**
(`sensor.opti_balancing_watchdog`): `intervall_tage` = Tage ohne Voll-/Done-Ladung bis
der Watchdog fällig wird (0 = aus), `karenz_tage` = zusätzliche Wartezeit vor dem
bezahlten Netz-Fallback, `max_ct` = absoluter Brutto-Preisdeckel fürs bezahlte
Balancing-Netzladen (0 = fail-safe aus). `input_number.opti_balancing_done_soc` hat als
einziger ein `initial:` (98.5 %) und muss nicht von Hand gesetzt werden - er definiert die
„Akku ~voll"-Schwelle für Counter-Reset und tägliches Increment. Dieser Wert wird bewusst
bei jedem HA-Start erneut auf 98.5 % gesetzt; eine UI-Änderung dieses einen Helfers ist
daher nicht restart-dauerhaft. Der Schalter
`input_boolean.opti_balancing_netzladen` (**Default aus**) entscheidet, ob der Watchdog fürs
BMS-Balancing auch aus dem **Netz** laden darf; ohne ihn balancet er rein per PV. Er ist
bewusst von `opti_prognose_netzladen` entkoppelt, damit Balancing-Netzladen unabhängig vom
allgemeinen Prognose-Netzladen freigegeben werden kann.

Die beiden **Überschuss-Grenzen** gehören zum Einspeise-Override: liegt die jeweilige
Messgröße über der Grenze, lädt die Strategie auch über den Ziel-SoC hinaus, statt den
Überschuss abregeln zu lassen.
Die beiden Signale messen dabei Unterschiedliches.
`wr_70proz_ueberschuss_grenze` vergleicht die **Netzeinspeisung ohne Akkueingriff**
(Export plus Batterieleistung) und gehört auf die Leistung, ab der deine Einspeisung
begrenzt wird - bei einer 70%-Regel also 0,7 × kWp in Watt.
`wr_ac_ueberschuss_grenze` vergleicht die **PV-Leistung ohne Akkueingriff** und gehört auf
die AC-Nennleistung deines Wechselrichters.
Beide gelten bei **0 als nicht konfiguriert und schalten den Override ab** - sonst würde
eine Grenze von 0 W jeden Export als Überschuss werten.
Wirksam wird der Override ohnehin erst, wenn `input_boolean.opti_pv_ueberschuss_ladung`
eingeschaltet ist (Schritt 9).

`input_number.ladepreis` musst du **nicht** selbst setzen.
Die Strategie schreibt dort den aktuellen Strompreis hinein, wenn sie den manuellen
Netzlade-Booster (`input_boolean.hausakku_aus_netz_laden`) bei einem SoC über 99 % selbst
abschaltet - negative Börsenpreise eingeschlossen, deshalb reicht der Helfer ins Negative.
Zusammen mit `mindestpreisdifferenz_lade_entladepreis` ergibt er
`sensor.opti_mindestentladepreis_ct_kwh`, der bislang nur informativ ist: er zeigt an und
triggert die Automation, sperrt aber kein Entladen.

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

## Aktualisierung bestehender Installationen (September 2026)

Strategie und `opti_derived.yaml` gemeinsam aktualisieren: der Ladedeckel verwendet
jetzt `binary_sensor.opti_ladedeckel_aktiv` aus den abgeleiteten Sensoren. Keine
zusätzlichen UI-Helfer und keine Änderung des Adapter-Vertrags sind erforderlich.
Die direkte MaxSOC-Grenze wirkt auch ohne den neuen Merker; dessen Halteband ist
aber erst mit der neuen Sensor-Datei verfügbar. Das private Mapping bleibt erhalten.

Vor dem Einspielen die betroffenen Live-Dateien sichern. Bestehende Package-Wrapper
(`automation:`), `initial_state`-Anpassungen und benutzerspezifische Include-Pfade
beibehalten. Optionales BYD-Monitoring benötigt zusätzlich das aktualisierte
`byd_modul2_fruehwarnung.yaml`; bei Nutzung der alten Laufzeitanzeige auch
`sma_templates.yaml` mit den eigenen Quell-Entitäten aktualisieren.

Nach HA-Konfigurationsprüfung und Freigabe der Betriebsunterbrechung neu starten,
oder Templates vor den Automationen neu laden. Reloads verwerfen laufende
Automationsausführungen; das deshalb in einem geeigneten Betriebsfenster tun.
Prüfen: neuer Ladedeckel-Sensor vorhanden, Vorschau plausibel, Adapter-Keepalive
aktuell, keine neuen Template- oder Statistik-Warnungen. Der Merker wird bei
Neustart/Quelländerung und spätestens am nächsten Minutentick ausgewertet.
Bei Erstinstallation unterhalb MaxSOC beginnt er ohne belegten Eintritt.
Für Rollback die gesicherten Dateien gemeinsam wiederherstellen und erneut laden.

## Legacy-Setup (Referenz)

Der frühere manuelle Weg mit Flachdateien (Modbus-Config, Sensoren, Helfer-Tabelle, Dashboard-Karte) ist umgezogen nach [`old/README.md`](../old/README.md).
Empfohlen bleibt die Package-Struktur oben.

> 💡 Nutzt du noch die alten Sensor-Namen (`akkusteuerung_dynamische_ladestaerke`,
> `akku_target_soc_intelligent`)? Erklärung und Alt↔Neu-Mapping:
> **[old/README.md#konzepte-legacy-namen](../old/README.md#konzepte-legacy-namen-oldtemplatesyaml)**
