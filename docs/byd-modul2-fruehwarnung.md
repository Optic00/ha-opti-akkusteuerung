# BYD Modul-2-Frühwarnung: Degradations-Monitoring für das schwächste Modul

Dieses optionale Package ([`packages/byd_modul2_fruehwarnung.yaml`](../packages/byd_modul2_fruehwarnung.yaml)) beobachtet das schwächste Modul eines BYD-HVS-Turms auf schleichende Degradation.
Es baut auf dem nativen BYD-Monitoring ([`packages/byd_monitoring.yaml`](../packages/byd_monitoring.yaml), Doku: [byd-monitoring-nativ.md](byd-monitoring-nativ.md)) auf und ist rein beobachtend, ohne jede Steuerwirkung und **ohne Alarm/Push**.
Datenquelle sind die nativen Modbus-Zelldaten der Integration `byd_battery_box` (Attribut `cell_voltages`, native Energie-Lebenszähler), nicht mehr die abgelösten MQTT-Sensoren.

Nach der Erstinstallation einmalig `input_number.byd_knie_referenzspannung` auf 3,20 V stellen; die Helfer tragen bewusst kein `initial:`, weil HA das bei jedem Neustart anwenden und Referenz wie Zyklus-Status überschreiben würde.

> **Zäsur 17.7.2026 gegenüber der alten (MQTT-)Frühwarnung:**
> Beide Metriken wechseln Quelle **und** Definition, die Zeitreihen sind nicht durchgängig zu lesen.
> Metrik A misst jetzt gegen den Median **aller 160 Zellen** statt gegen den Peer-Median der vier Nachbarmodule (der war zur schwächsten Zelle der Nachbarn hin systematisch nach unten verzerrt).
> Metrik B wird über einen Bestätigungszähler mit Mess-Anker statt über eine `for:`-Haltezeit erkannt.
> Der alte Latch-Pfad war ohnehin **nie** end-to-end durchlaufen (kein einziger echter Messwert), die Absackungs-Reihe erst zwei Tage alt - deshalb bekommen alle Entities neue `unique_id`s (Suffix `_nativ`) und werden über einen Orphan-Purge frisch registriert, statt eine fachlich gebrochene Reihe scheinbar fortzuführen.

## Ausgangslage

In jedem Serien-Turm stellt genau eine Zelle das Minimum, das ist noch kein Befund.
Interessant sind Größe und Trend der Abweichung: eine stabil leicht schwächere Zelle ist normale Fertigungsstreuung, eine über Monate wachsende Abweichung ist ein alterndes Modul und ein Fall für die Garantie.
In diesem Setup ist Modul 2 (bislang Zelle 23) das schwächste Glied, deshalb sind die Templates und Trigger auf Modul 2 verdrahtet.
Für andere Türme die Modul-Nummer in den Templates und Automationen anpassen.

## Metrik A: relative Zell-Absackung (in standardisierten Betriebspunkten)

`sensor.byd_modul_2_zell_absackung` misst in mV, wie weit die schwächste Zelle von Modul 2 unter dem **Median aller 160 Zellen** liegt.
Der Median über den ganzen Turm ist der robuste zentrale Referenzpunkt (anders als der alte Peer-Median der Nachbar-**Minima**, der die schwächste Zelle gegen die jeweils schwächsten Zellen der Nachbarn verglich).

Der Wert ist zustandsabhängig und wird deshalb **nicht mehr laufend**, sondern nur in einem standardisierten Betriebspunkt gesampelt - die Interpretations-Vorgabe „nur im gleichen Betriebspunkt vergleichen" erzwingt jetzt der Sensor selbst:
Der Sensor ist trigger-basiert (Trigger direkt auf den Attribut-Träger `sensor.bms_1_cells_average_voltage`, damit das gelesene Attribut aus dem auslösenden Zyklus stammt) und übernimmt ein Sample nur, wenn die Zelldaten frisch sind, das Entladeband seit ≥ 3 min an ist und der BMU-SoC zwischen 25 und 85 % liegt.

Attribute liefern Kontext für jede Messung: `zelle` (Zell-Nummer 1-32 des Minimums **innerhalb** von Modul 2 - beantwortet dauerhaft „ist es noch Zelle 23?"), `schwaechstes_modul` (Modul 1-5 der global schwächsten Zelle - macht eine Drift zu einem anderen Modul sichtbar), `median_mv`, `soc`, `leistung_w`.

> **Restrisiko (dokumentiert, bewusst ohne Glättung):** SoC und Leistung für Gate und Attribute stammen vom letzten BMU-Poll (bis ~60 s vor dem Zell-Read).
> Ein Lastwechsel genau in diesem Fenster kann ein einzelnes Sample falsch etikettieren; über die Kontext-Attribute ist es filterbar.
> Es gibt bewusst keine Median-Glättung auf der Trend-Serie - Ausreißer sollen sichtbar bleiben.

## Metrik B: Nettoenergie bis zum Knie (pro Vollzyklus)

`sensor.byd_modul_2_nettoenergie_bis_knie` beantwortet die eigentliche Kapazitätsfrage in kWh statt in mV:
wie viel Nettoenergie (entladen minus geladen) lässt sich nach einer Vollladung entnehmen, bis die schwächste Zelle ihr unteres Knie erreicht?
Sinkt dieser Wert über Monate, verliert das Modul messbar Kapazität.

Der Messzyklus läuft als Zustandsmaschine (`input_select.byd_knie_zyklus_status`):

1. **idle → armed (Voll-Anker):** Nach Ladeschluss (Zellmax war ≥ 3,55 V für 2 min, dann 5 min keine nennenswerte Ladung, SoC > 94 %, beide Frische-Binaries an, Zähler-Basiswerte gültig) werden die utility_meter (`byd_geladen_seit_voll`/`byd_entladen_seit_voll`, Quellen `bms_1_charge/discharge_total_energy`) genullt, die Knie-Referenzspannung eingefroren (`byd_knie_ref_frozen`, Default 3,20 V), eine **neue Cycle-ID** vergeben (sie setzt den Bestätigungszähler zurück) und der Zyklus scharf geschaltet.
   Eine Re-Arm-Sperre (mindestens 0,5 kWh netto entnommen seit dem letzten Anker) verhindert Mehrfach-Resets im Ladeschluss-Flattern.
2. **armed → latched (Knie-Latch):** Wenn der Bestätigungszähler `sensor.byd_knie_bestaetigungen` **≥ 2 konsekutive qualifizierende BMS-Zyklen** erreicht (Zellmin von Modul 2 unter der eingefrorenen Referenz, im Entladeband, bei frischen Daten), und die Spannung seit dem Anker klar über der Schwelle war (Überschwelle-Guard, ref + 30 mV).
   Der Latch-Sensor snapshottet in diesem Moment die **Mess-Anker-Werte** (Nettoenergie und Absackung am ersten Unterschreitungs-Sample, siehe unten) plus Begleitwerte (SoC, Zellmin, schwächste Zelle, Cycle-ID) als Attribute.
3. **armed → invalid:** Bei Messqualitäts-Problemen wird der Zyklus verworfen statt falsch gemessen: Datenlücke (eines der beiden Frische-Binaries ≥ 2 min aus) oder HA-Neustart, jeweils **nahe am Knie** (Zellmin ≤ ref + 10 mV, fail-safe: unbekannt zählt als nah → verwerfen), oder ein unplausibler Nettoenergie-Sprung (> 3 kWh zwischen zwei Updates, außerhalb der Anker-Sekunden).
   Beim HA-Neustart wartet der Pfad **bis zu 15 min auf frische Zelldaten**, bevor der Nähe-Check greift - direkt nach dem Boot ist Zellmin noch `unavailable`, und `float(0)` würde sonst **jeden** Routine-Restart im armed-Zustand als knie-nah invalidieren und gesunde Plateau-Zyklen verwerfen; erst nach dem Timeout (Integration wirklich tot) greift `float(0)` korrekt als „verwerfen".
   Mit `invalid` werden `armed` und der Überschwelle-Guard zurückgesetzt (kein Zustandswiderspruch), der Grund landet in `input_text.byd_knie_invalid_grund`.

Das Attribut `sauberer_zyklus` markiert Zyklen ohne nennenswerte Zwischenladung (< 0,5 kWh geladen seit Voll); nur diese sind untereinander streng vergleichbar.

### Statistik-Klassen der Diagnosekurven

`byd_nettoenergie_seit_voll` und `byd_modul_2_nettoenergie_bis_knie` bleiben
kWh-Diagnosekurven mit `state_class: measurement`, aber ohne `device_class: energy`.
Die bisherige Kombination wurde von HA als ungültig gemeldet (#64).
Ausgewertet werden die absoluten Zykluswerte und deren Min/Max/Mittelwerte,
keine aufsummierten Energieumsätze. Insbesondere ein kleinerer Knie-Wert muss
als kleinerer Messwert sichtbar bleiben. `total` oder `total_increasing` würden
hier eine andere Statistik erzeugen. Die eigentlichen Energiezähler bleiben die
beiden `utility_meter` mit ihren jeweiligen Quellzählern.

Entity-IDs, Mess-Anker und Kurven bleiben erhalten. Nach einem Update die
Statistik-Reparaturen in HA prüfen; vorhandene Recorder-Daten werden durch diese
Dateiänderung weder gelöscht noch rückwirkend umgerechnet.
Die [HA-Dokumentation zu Sensor-Statistiken](https://developers.home-assistant.io/docs/core/entity/sensor/#long-term-statistics)
beschreibt die Unterscheidung zwischen Messwerten und Zählern.

### Bestätigungszähler statt `for:` - Zähler-Mechanik und Mess-Anker

Die Knie-Erkennung läuft bewusst **nicht** über eine `for:`-Haltezeit.
Ein `for:` von z. B. ≥ 21 min würde (a) Latch-Ausbeute an Haushaltslast-Flattern verlieren, (b) sich per Stagnation auch auf eingefrorenen Werten erfüllen und (c) einen mit der Wartezeit wachsenden kWh-Offset in die Messung tragen.

Stattdessen zählt `sensor.byd_knie_bestaetigungen` konsekutive qualifizierende BMS-Detail-Zyklen.
Er ist trigger-basiert (auf den Attribut-Träger **und** auf die Cycle-ID als Reset-Signal) und **zustands- statt flankensicher** - alle Entscheidungen fallen im Template gegen die eigenen Vorwerte (`this.attributes`):

- **Reset über die Cycle-ID:** ist die gespeicherte Cycle-ID ≠ der aktuellen, wird der Zähler **bedingungslos auf 0** gesetzt - das reine Reset-Event ist kein qualifizierendes Sample. Erst der nächste `cells_average_voltage`-Trigger im laufenden Zyklus kann Stand 1 erzeugen (sonst könnte das erste echte Folge-Sample bereits Stand 2 und damit einen verfrühten Latch auslösen). Das fängt auch ein Re-Arm `armed → armed` ab, weil der Voll-Anker **immer** eine neue Cycle-ID vergibt.
- **Dedupe (300 s):** liegt der Abstand zum letzten gezählten Sample unter 300 s, ist es ein Doppel-Event desselben BMS-Samples und wird nicht gezählt.
- **Lücken-Guard / Streak-Start (1500 s):** liegt der Abstand über 1500 s (auch: kein laufender Streak, `letzter_zyklus` = 0 nach Reset), ist es das erste Sample eines Streaks und der Zähler startet bei 1.
- Ein nicht-qualifizierendes Sample nullt den Zähler.

**Mess-Anker:** Beim frischen Übergang auf Zählerstand 1 snapshottet der Zähler den aktuellen `sensor.byd_nettoenergie_seit_voll` als `netto_bei_erstem_sample` sowie - **aus demselben `cell_voltages`-Attribut, das die Qualifikation gerechnet hat** - `absackung_bei_erstem_sample`, `zelle_bei_erstem_sample` und `schwaechstes_modul_bei_erstem_sample`.
Der Latch übernimmt **diese** geankerten Werte, nicht die zum Latch-Zeitpunkt und nicht den (gegateten, ggf. veralteten) Momentanwert des Absackungs-Sensors - so passen Absackungswert und Zell-Nummer garantiert zusammen.
Gemessen wird am ersten Unterschreitungs-Sample (Lag zum echten Knie ≤ 1 Zyklus, ~0-0,26 kWh bandbegrenzt), die zweite Bestätigung validiert nur noch; der Wartezeit-Offset verschwindet aus der Messung.

Der Latch selbst hängt an einem **state-Trigger** auf den Zähler mit der Bedingung Wert ≥ 2 **und** einem numerischen Mess-Anker (kein `numeric_state`, das nur die steigende Flanke sähe): scheitert der erste Latch-Versuch (Bedingung, ungültiger Anker, Crash, Neustart mit restauriertem Zähler), feuert der nächste Zyklus (2 → 3) erneut.

## Interpretation

Ein einzelner Latch-Wert sagt wenig, die Zeitreihe über Wochen und Monate ist die Aussage.
Stabiler Offset bei Metrik A plus stabile kWh bei Metrik B bedeutet Fertigungsstreuung, kein Handlungsbedarf.
Wachsende Absackung im gleichen Betriebspunkt oder sinkende Nettoenergie-bis-Knie über Monate bedeutet beschleunigte Alterung, dann die Messreihe sichern und den Garantiefall mit Daten belegen (BYD Battery-Box Premium: 10 Jahre mit Kapazitätszusage).
Ein Modul-Ausbau (Turm läuft offiziell auch mit 4 Modulen) ist erst sinnvoll, wenn das Modul effektiv mehr als seine eigene Kapazität kostet oder aktiv stört, und damit klar hinter Beobachten und Garantiefall die dritte Option.

## Warum kein Alarm

Das alte Package hatte keinen `notify`, und das bleibt richtig: Trend-Metriken ohne Baseline können keinen sinnvollen Schwellwert haben (Bens Fehlalarm-Vorgabe).
Erst nach Wochen Baseline lohnt eine Drift-Regel - dann als eigener PR.
Im Package steht dafür nur ein TODO-Vermerk.

## Design-Historie

Das ursprüngliche, Codex-adversarial gehärtete MQTT-Design liegt unter [byd-modul2-fruehwarnung-design.md](byd-modul2-fruehwarnung-design.md); die alte Implementierung unter [`legacy/byd_modul2_fruehwarnung.yaml`](../legacy/byd_modul2_fruehwarnung.yaml).
Der native Neubau (Phase 2) ist in [byd-monitoring-nativ.md](byd-monitoring-nativ.md#phase-2-modul-frühwarnung-neu-bauen-umgesetzt) eingeordnet.
Wesentliche Abweichungen zum MQTT-Stand: Median(160) statt Peer-Median, Bestätigungszähler mit Mess-Anker statt `for:`-Haltezeit, native Modbus-Quellen und die beiden Frische-Binaries aus dem Monitoring-Package.

## Status und offene Punkte

- Package, Tests und Doku sind gebaut; der Live-Deploy (Orphan-Purge, Re-Seed, Dashboard-Karten-Umzug) steht noch aus (Checkliste im Package-Header).
- Der komplette Latch-Pfad (Voll-Anker bis Knie-Latch) ist auch nativ noch nicht end-to-end durchlaufen; er braucht eine Vollladung mit anschließender Entladung bis unter die Referenzspannung im Entladeband. Der erste E2E-Durchlauf verifiziert diesen im Alt-Design nie erreichten Pfad erstmals.
- Der Invalid-Pfad wird aktiv getestet (Integration nahe am Knie einmal reloaden → `datenluecke`-Invalid muss greifen), nicht nur passiv beobachtet.
- Erst nach den ersten sauberen Zyklen lohnt eine Bewertung der Referenzspannung (3,20 V) und des Entladebands.
