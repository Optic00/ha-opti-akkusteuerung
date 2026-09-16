## Steuerung und Schreibschutz

> **Achtung: aktive Akkusteuerung.** Bei freigegebenen Schreibzugriffen beeinflusst
> Opti Akku das Lade- und Entladeverhalten. Falsche Einstellungen oder Fehler
> können zusätzliche Stromkosten, unerwünschten Betrieb oder Schäden verursachen.
> Herstellervorgaben beachten und vor der Freigabe andere schreibende Steuerungen
> deaktivieren. Shadow bleibt strikt lesend. Dieser Hinweis ist keine Zusicherung
> eines vollständigen Haftungsausschlusses.

### Strikt lesender Shadow-Test

Bei einer neuen Einrichtung ist **Shadow-Modus** vorausgewählt. Dieser
Eintrag kann grundsätzlich keine Modbus-Schreibbefehle senden, auch keine
Pause beim Beenden. Die Sperre gilt zusätzlich im Gerätetreiber. Es gibt
keinen Schalter zur Schreibfreigabe; über die Optionen lässt sich die Sperre
nicht aufheben. Für einen späteren schreibenden Betrieb ist ein neuer,
ausdrücklich dafür angelegter Eintrag erforderlich.

Für einen Vergleich neben einer bestehenden Steuerung:

1. Shadow-Modus bei der Einrichtung eingeschaltet lassen. Die bisherige
   Steuerung bleibt aktiv, die Single-Writer-Bestätigung bleibt aus.
2. Quellen und Strategieparameter passend zur bestehenden Steuerung einstellen.
   Optional deren Modus-Entität als Vergleichsquelle auswählen. Die
   **Strategie berechnen** der Shadow-Instanz einschalten, damit sie echte
   Entscheidungen berechnet. Dies erteilt im Shadow-Modus keine Schreibrechte.
3. Auf der Geräteseite **24-Stunden-Test starten** drücken.
4. **Shadow-Aufzeichnung** zeigt Status, Start, feste Deadline, Stichproben,
   Quellenfehler, Abweichungen, Einstellungsänderungen und Beobachtungslücken.

Die Aufzeichnung schreibt höchstens alle 15 Sekunden eine Stichprobe in
`<config>/opti_akku_shadow/<Sitzungs-ID>.jsonl`. Diese privaten Betriebsdaten
gehören nicht ins Repository und liegen nicht unter `www`. Jede neue Sitzung
bekommt eine eigene Datei; erneutes Drücken während einer laufenden Sitzung
verlängert sie nicht. Ein HA-Neustart erhält die ursprüngliche Deadline. Mit **Shadow-Test beenden**
lässt sich die Aufzeichnung vorzeitig beenden; der Eintrag bleibt lesend.

Die Journaldateien werden nicht automatisch gelöscht und können in HA-Backups
enthalten sein. Nach Auswertung nicht mehr benötigte Sitzungen gezielt im
privaten Verzeichnis entfernen; niemals eine laufende Aufzeichnung löschen.

Nach 24 Stunden endet nur die Aufzeichnung. Die Integration bleibt strikt
lesend. `completed` bedeutet Fristende, nicht lückenlose Datenabdeckung.
Abweichende Modi sind zunächst Vergleichsbefunde: Die neue Instanz muss
Mittelwerte und Hysterese erst aufbauen und übernimmt alte Helferzustände
nicht automatisch. Ein Shadow-Test belegt keine physische Wirkung der
Schreibregister und ersetzt keinen späteren begleiteten Schreibtest.

### Schreibender Betrieb

Die Strategieparameter werden über **Konfigurieren** gepflegt. Die zugehörigen
Zahl- und Konfigurationsschalter-Entitäten sind bei neuen Einträgen standardmäßig
deaktiviert und können bei Bedarf aktiviert werden. Die Vorgaben sind konservativ. Automatisches Netzladen und Balancing über das Netz sind bei neuen Einträgen
ausgeschaltet. **Reserveplanung mit Netzladen erlauben** aktiviert auch
preisabhängiges Vorladen und Laden bei negativen Preisen; es ist keine reine
Haltefunktion. Gespeicherte und ausdrücklich importierte Werte bleiben erhalten.

Die eigentliche Steuerung hat drei getrennte Ebenen:

1. Der Schalter **Strategie berechnen** ist die Hauptfreigabe für die Steuerung,
   einschließlich der manuellen Modi. Ist er aus, gilt immer Pause.
2. In den Integrationsoptionen muss bestätigt werden, dass keine zweite
   Steuerung parallel auf den Wechselrichter schreibt.
3. Erst der Schalter **Schreibzugriffe freigeben** erlaubt Modbus-Schreibzugriffe.

Die Bestätigung während der Einrichtung aktiviert keine Schreibzugriffe. Bei
einem neuen Setup ist die Schreibfreigabe aus. Eine gespeicherte Freigabe wird
nur bei aktivierter Strategie, unveränderter Gerätebindung und gültigen gespeicherten
Einstellungen wiederhergestellt, solange die Single-Writer-Bestätigung weiter gesetzt ist.

Bei aktivierter Strategie stehen für SMA über **Betriebsart** `Strategie`
und neun manuelle Modi zur Auswahl. Ohne Strategie ersetzt **Beobachtung** (Select-Rohwert `observation`) den
Strategiemodus; Dynamisch und berechnetes Netzladen entfallen:

- Akku Automatisch
- Akku schnell Laden
- Akku schnell Entladen
- Akku Pause
- Akku nur Laden
- Akku Netzladen
- Akku nur Entladen
- Akku Dynamisch
- Akku 0.2C Laden

Ein erzwungener manueller Modus wird nach einem Neustart nicht fortgesetzt.
Ohne aktivierte Strategie wird auch die Schreibfreigabe nicht wiederhergestellt.
Zuerst wieder einen manuellen Modus wählen, dann die Schreibfreigabe setzen.
Vor jeder Steuerungsbewertung müssen die Profilprüfung und aktuelle Messungen erfolgreich
sein. Ohne lesbare Seriennummer bleibt die Schreibfreigabe gesperrt.
Sicherheitsgrenzen für SoC, Messwerte und Quellen gelten auch im
manuellen Betrieb.

Manuelles Laden ist zusätzlich unter 5 °C und ab 50 °C gesperrt. Das ist eine
konservative Bediengrenze dieser Integration, keine zugesicherte
Herstellergrenze. Die automatische Strategie behält ihre ursprüngliche
Temperaturabsenkung und Ladeabschaltung bei. Bei manueller Automatik oder
Dynamik bleibt an einer SoC- oder Temperaturgrenze die zulässige Gegenrichtung
aktiv. Treffen Entlade- und Ladesperren zusammen, folgt Pause. Eine EV-Sperre
überstimmt weder die Temperaturgrenze noch Max-SoC.

Ab 45 °C begrenzt die berechnete Ladeleistung auch manuelle Schnell- und
0.2C-Vorgaben. Manuelle SoC-Sperren lösen sich erst mit bis zu drei
Prozentpunkten Abstand zur Grenze; bei engem Min-/Max-SoC-Bereich beträgt
der Abstand höchstens die Hälfte dieses Bereichs. Diese Sperrzustände bleiben
beim Neuladen erhalten. Nicht konfigurierte EV-Ladepunkte beteiligen sich
nicht an der Entladesperre; fehlende Daten eines konfigurierten Ladepunkts
halten eine bereits aktive Sperre weiterhin fest.

Der Ladedeckel sperrt ab `maxsoc` weiteres Laden und bleibt mit drei
Prozentpunkten Hysterese aktiv. Ohne weitere Sperre bleibt Entladen erlaubt.
Trifft der Ladedeckel auf einen Reserve-Haltefall oder die aktive
EV-Schnelllade-Sperre, setzt die Strategie stattdessen Pause. So entlädt sie den
Akku nicht selbst unter den Ladedeckel, um ihn anschließend wieder bis zur
Reserve zu laden. Die geplante Entladung während eines teuren Peak-Fensters
bleibt davon unberührt.
Ein fälliger Balancing-Zyklus hat weiterhin Vorrang und darf den Akku gezielt
bis 100 Prozent laden.

Der Diagnosesensor **Befehlsnachweis** trennt drei Aussagen, die nicht
gleichgesetzt werden dürfen:

- **Adapterausführung abgeschlossen** bedeutet bei SMA nur, dass die
  Modbus-Schreibsequenz ohne gemeldeten Fehler beendet wurde. Die verwendeten
  BMS- und Sollwertregister werden nicht zurückgelesen.
- Bei Huawei werden verfügbare Steuerentitäten und der Forced-Status geprüft.
  Diese Rücklesung bleibt partiell, weil die TOU-Tabelle nicht unabhängig
  zurückgelesen werden kann.
- **Physische Wirkung** bleibt unbestätigt. Eine gemessene Akkuleistung oder
  das Ausbleiben einer Sperrverletzung beweist keine exakte Sollwertübernahme.

Der ältere Sensor **Befehlsbestätigung** bleibt aus Kompatibilitätsgründen
erhalten. Sein Zustand **Kein offener Befehl** sagt nur, dass aktuell keine
Transaktion aussteht.

### SMA-Befehle prüfen

Die drei Nachweisstufen werden getrennt bewertet:

| Stufe | Belastbare Aussage | Derzeitiger SMA-Stand |
|---|---|---|
| Transport | Die vollständige Modbus-Schreibfolge endete ohne gemeldeten Fehler. | Als **Adapterausführung abgeschlossen** sichtbar. |
| Sollwertübernahme | Der Wechselrichter meldet den tatsächlich übernommenen Modus und Sollwert aus einer unabhängigen Quelle. | Nicht unterstützt. Die verwendete Registerfamilie wird nicht zurückgelesen. |
| Physische Wirkung | Eine frische Gerätemessung zeigt die erwartete Lade- oder Entladerichtung beziehungsweise den Stillstand. | Nur als Beobachtung prüfbar; kein Nachweis des exakten Sollwerts. |

Ein begleiteter Hardwaretest soll deshalb Modell, Firmware, Ausgangs-SoC,
Modus, angeforderten Wert, Befehlsnachweis und die Akkuleistung aus einem
späteren erfolgreichen Geräteabruf gemeinsam festhalten. Der im
Befehlsnachweis enthaltene Leistungswert wurde vor dem Schreiben gelesen und
eignet sich dafür nicht. Zuerst **Akku Pause**, danach **Akku schnell Laden**
und **Akku schnell Entladen** mit kleinen Sollwerten in sicherem Abstand zu
Min- und Max-SoC prüfen. Bei **Akku Pause** soll die Akkuleistung nahe 0 W
liegen; Laden und Entladen sollen jeweils die passende Richtung zeigen. Die
Schalter **Strategie berechnen** und **Schreibzugriffe freigeben** sowie die
Single-Writer-Bestätigung in den Optionen müssen aktiv sein. Für einen
unbegrenzten Schnellladetest muss die Batterietemperatur mindestens 5 und
weniger als 45 °C betragen. Andere schreibende Steuerungen müssen aus sein und
ein lokaler Rückweg muss bereitstehen. Eine passende Leistungsrichtung belegt
nur die Wirkung dieses Versuchs;
BMS-Begrenzungen, Rampen, PV-Leistung und Hauslast können die Höhe verändern.

Die automatisierten Ausfalltests decken veraltete Daten vor und während einer
Schreibfolge, Abbruch und Zeitüberschreitung, fehlgeschlagene Bereinigung,
mehrfache Abbruchsignale, konkurrierende Befehle, Neuladen der Integration und
die Sperre nach einer Wiederverbindung bis zur erneuten Gerätebereitschaft ab.
Sie belegen das Verhalten des Adapters, nicht das Verhalten jeder
Wechselrichter-Firmware. Ein realer Kommunikationsabbruch während eines
Schreibvorgangs und die spätere Rückkehr zur Eigenregelung bleiben deshalb
getrennte Hardware-Abnahmen.

Beim Ausschalten versucht die Integration, den Wechselrichter in Pause zu
setzen. Das ist ein Best-Effort-Vorgang. Hardware-, Netzwerk- oder
Protokollfehler können den Stopp verhindern. Auch eine bestätigte Pause ist
keine nachgewiesene dauerhafte Sperre. Ob und wann der Wechselrichter nach einem
Timeout oder HA-Ausfall seine Eigenregelung übernimmt, wurde nicht unabhängig
nachgewiesen. Der Diagnosesensor **Rückgabe an Gerätesteuerung** zeigt diese
fehlende Fähigkeit ausdrücklich als **Nicht unterstützt** an.

## Diagnose herunterladen

Unter **Einstellungen -> Geräte & Dienste -> Opti Akku -> Drei-Punkte-Menü ->
Diagnose herunterladen** stellt Home Assistant eine kompakte Support-Datei
bereit. Sie enthält Betriebsart, Verbindungs- und Funktionsstatus sowie
zusammengefasste Fehlercodes. Hostnamen, IP-Adressen, Seriennummern, Entity-IDs,
Fahrzeugdaten, Messwerte und Historien werden nicht exportiert. Prüfe die Datei
trotzdem vor einer Veröffentlichung und teile sie bevorzugt über die
Issue-Vorlage statt als vollständige Home-Assistant-Konfiguration.
