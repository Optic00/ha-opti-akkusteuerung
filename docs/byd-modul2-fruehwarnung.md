# BYD Modul-2-Frühwarnung: Degradations-Monitoring für das schwächste Modul

Dieses optionale Package ([`packages/byd_modul2_fruehwarnung.yaml`](../packages/byd_modul2_fruehwarnung.yaml)) beobachtet das schwächste Modul eines BYD-HVS-Turms auf schleichende Degradation.
Es baut auf dem BMU-Monitoring ([`packages/byd_bmu.yaml`](../packages/byd_bmu.yaml), Doku: [byd-bmu-monitoring.md](byd-bmu-monitoring.md)) auf und ist rein beobachtend, ohne jede Steuerwirkung.
Nach der Erstinstallation einmalig `input_number.byd_knie_referenzspannung` auf 3,20 V stellen; die Helfer tragen bewusst kein `initial:`, weil HA das bei jedem Neustart anwenden und Referenz wie Zyklus-Status überschreiben würde.

## Ausgangslage

In jedem Serien-Turm stellt genau eine Zelle das Minimum, das ist noch kein Befund.
Interessant sind Größe und Trend der Abweichung: eine stabil leicht schwächere Zelle ist normale Fertigungsstreuung, eine über Monate wachsende Abweichung ist ein alterndes Modul und ein Fall für die Garantie.
In diesem Setup ist Modul 2 (Zelle 23) das schwächste Glied, deshalb sind die Templates und Trigger auf Modul 2 verdrahtet.
Für andere Türme die Modul-Nummer in den Templates und Automationen anpassen.

## Metrik A: relative Zell-Absackung (laufend)

`sensor.byd_modul_2_absackung` misst in mV, wie weit die schwächste Zelle von Modul 2 unter dem Peer-Median der vier Nachbarmodule liegt.
Der Median (Mittel der beiden mittleren von vier Peer-Werten) ist robust gegen einen einzelnen abweichenden Nachbarn.
Der Wert ist zustandsabhängig und muss im gleichen Betriebspunkt verglichen werden: im LFP-Plateau in Ruhe liegt er nahe 0 mV, unter Entladelast (Innenwiderstands-Anteil) und am unteren Kennlinien-Knie (Kapazitäts-Anteil) steigt er.
Attribute liefern Kontext für jede Messung: SoC, Leistung, absolute Modul-2-Spannung, schwächste Zell-ID und welches Modul aktuell das Minimum stellt.

## Metrik B: Nettoenergie bis zum Knie (pro Vollzyklus)

`sensor.byd_modul_2_netto_bis_knie` beantwortet die eigentliche Kapazitätsfrage in kWh statt in mV:
wie viel Nettoenergie (entladen minus geladen) lässt sich nach einer Vollladung entnehmen, bis die schwächste Zelle ihr unteres Knie erreicht?
Sinkt dieser Wert über Monate, verliert das Modul messbar Kapazität.

Der Messzyklus läuft als Zustandsmaschine (`input_select.byd_knie_zyklus_status`):

1. **idle → armed (Voll-Anker):** Nach Ladeschluss (Zellmax war >= 3,55 V für 2 min, dann 5 min keine nennenswerte Ladung, SoC > 94 %, Daten frisch) werden die Utility-Meter (`byd_geladen_seit_voll`/`byd_entladen_seit_voll`) genullt, die Knie-Referenzspannung eingefroren (`byd_knie_ref_frozen`, Default 3,20 V) und der Zyklus scharf geschaltet.
   Eine Re-Arm-Sperre (mindestens 0,5 kWh netto entnommen seit dem letzten Anker) verhindert Mehrfach-Resets im Ladeschluss-Flattern.
2. **armed → latched (Knie-Latch):** Wenn Modul-2-min die eingefrorene Referenz erstmals 3 min lang unterschreitet, im standardisierten Entladeband (500 bis 1500 W über die Haltezeit), bei frischen Daten und nachdem die Spannung seit dem Anker klar über der Schwelle war (Überschwelle-Guard, ref + 30 mV).
   Der Latch-Sensor snapshottet in diesem Moment Nettoenergie und Begleitwerte (Absackung, SoC, Temperatur, Zell-IDs, Cycle-ID) als Attribute.
   Das Entladeband standardisiert den Betriebspunkt, damit die kWh-Werte über Zyklen vergleichbar sind; die 3-min-Haltezeit ersetzt eine explizite mV-Hysterese.
3. **armed → invalid:** Bei Messqualitäts-Problemen wird der Zyklus verworfen statt falsch gemessen: Datenlücke oder HA-Neustart nahe am Knie (Modul-2-min <= ref + 10 mV) oder ein unplausibler Nettoenergie-Sprung (> 3 kWh zwischen zwei Updates, außerhalb der Anker-Sekunden).
   Der Grund landet in `input_text.byd_knie_invalid_grund`.

Das Attribut `sauberer_zyklus` markiert Zyklen ohne nennenswerte Zwischenladung (< 0,5 kWh geladen seit Voll); nur diese sind untereinander streng vergleichbar.

## Interpretation

Ein einzelner Latch-Wert sagt wenig, die Zeitreihe über Wochen und Monate ist die Aussage.
Stabiler Offset bei Metrik A plus stabile kWh bei Metrik B bedeutet Fertigungsstreuung, kein Handlungsbedarf.
Wachsende Absackung im gleichen Betriebspunkt oder sinkende Nettoenergie-bis-Knie über Monate bedeutet beschleunigte Alterung, dann die Messreihe sichern und den Garantiefall mit Daten belegen (BYD Battery-Box Premium: 10 Jahre mit Kapazitätszusage).
Ein Modul-Ausbau (Turm läuft offiziell auch mit 4 Modulen) ist erst sinnvoll, wenn das Modul effektiv mehr als seine eigene Kapazität kostet oder aktiv stört, und damit klar hinter Beobachten und Garantiefall die dritte Option.

## Design-Historie

Das ursprüngliche, Codex-adversarial gehärtete Design samt Umsetzungsplan liegt unter [docs/superpowers/specs/2026-07-15-byd-modul2-fruehwarnung-design.md](superpowers/specs/2026-07-15-byd-modul2-fruehwarnung-design.md) und [docs/superpowers/plans/2026-07-15-byd-modul2-fruehwarnung.md](superpowers/plans/2026-07-15-byd-modul2-fruehwarnung.md).
Abweichung zum dortigen Stand: das Package liegt separat (nicht in `byd_bmu.yaml` gebündelt, Live-Parität) und die Helfer tragen kein `initial:` mehr (Neustart-Reset-Bug, Codex-Review 16.7.).

## Status und offene Punkte

- Live seit 15.07.2026, die Absackungs-Metrik liefert seitdem Daten.
- Der komplette Latch-Pfad (Voll-Anker bis Knie-Latch) ist noch nicht end-to-end durchlaufen; er braucht eine Vollladung mit anschließender Entladung bis unter die Referenzspannung im Entladeband.
- Erst nach den ersten sauberen Zyklen lohnt eine Bewertung der Referenzspannung (3,20 V) und des Entladebands.
