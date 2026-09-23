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
   Optional deren Modus-Entität als Vergleichsquelle auswählen.
   Den Schalter **Strategie berechnen** der Shadow-Instanz einschalten, damit sie echte
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

### Passiver Vergleich während der aktiven Steuerung

Auch ein Standard-Eintrag kann mit **24-Stunden-Vergleich starten** ein
privates Journal führen. Die laufende Steuerung bleibt unverändert aktiv.
Start und Stopp verändern weder Schreibfreigabe noch Strategie und lösen
keine zusätzlichen Gerätebefehle aus. **Status des 24-Stunden-Vergleichs** zeigt den
Aufzeichnungsstatus; **24-Stunden-Vergleich beenden** stoppt nur das Journal.

Zusätzlich zu Modus und Messwerten enthält jede Stichprobe ausgewählte
Refill-Prognosen, die Profilreife sowie aktive Werte, beobachtende Kandidaten
und ihre Differenzen. Die Kandidaten bleiben rein informativ. `read_only` und `observation_only`
in einer Journalzeile beziehen sich auf die Aufzeichnung; `entry_shadow_mode`
und `write_enabled` zeigen separat, ob der Eintrag steuern darf. Fehlende oder
historisch ersetzte Profilwerte sind kein Beleg für eine belastbare Reserve.

Der Vergleich ist nur aussagekräftig, wenn die Referenz-Entität weiterhin von
einer laufenden Entscheidungslogik gesetzt wird. Ist die alte Automation
abgeschaltet, bleibt der Helfer stehen und jede Abweichung ist bedeutungslos.
Ändert sich die Referenz während der Sitzung nie, obwohl Opti Akku mindestens
dreimal den Modus wechselt, zeigt der Status `reference_static: true`; die
Diagnose übernimmt diesen Hinweis.

Für diese Aufzeichnung gelten dasselbe private Verzeichnis, die feste
24-Stunden-Frist und die Hinweise zu Datenlücken und Datenschutz wie beim
Shadow-Journal. Sie startet nur auf ausdrücklichen Knopfdruck. Ein vollständiger
Lauf belegt noch keine Einsparung oder physische Wirkung der Steuerbefehle.

### Vom Shadow-Test zur aktiven Steuerung

Unter **Integration hinzufügen → Opti Akku** steht bei geladenen
Shadow-Einträgen **Shadow-Einstellungen für aktive Steuerung übernehmen** zur
Auswahl. Auch das Konfigurationsmenü eines Shadow-Eintrags erklärt diesen Weg
unter **Aktive Steuerung einrichten**.

1. Passenden Shadow-Eintrag auswählen und die Übernahme bestätigen. Der
   Assistent legt einen **neuen Standard-Eintrag** an. Der alte Eintrag wird
   weder entsichert noch gelöscht oder automatisch deaktiviert.
2. Die vorbelegten Quellen, Grenzen und Funktionen prüfen. Die Gerätekennung
   wird vor der Übernahme und beim Speichern erneut lesend geprüft. Ein
   vorhandener Standard-Eintrag für dasselbe Gerät verhindert ein Duplikat.
   Bei Huawei müssen zusätzlich die Steuerentitäten zugeordnet und die
   Temperaturquelle geprüft werden. Nach den Benachrichtigungen folgen die
   erweiterten Grenzen und alle bereits konfigurierten Zusatzfunktionen auf
   ihren vorhandenen Einstellungsseiten.
3. Die bisherige schreibende Steuerung deaktivieren und dies im letzten Schritt
   bestätigen. Erst dann lässt sich die Übernahme speichern. Anschließend am
   neuen Eintrag **Strategie berechnen**, **Betriebsart → Strategie** und bewusst
   **Schreibzugriffe freigeben** einschalten. Bei deaktivierter optionaler
   Strategie stattdessen eine verfügbare manuelle Betriebsart wählen.
4. Modus, Entscheidungsgrund, Befehlsnachweis und tatsächliche Akkuleistung beim
   ersten Schreibbetrieb beobachten. Ist kein weiterer Vergleich nötig, den
   alten Shadow-Eintrag deaktivieren, um zusätzliche Abfragen zu vermeiden.

Übernommen werden Verbindung, Quellen, aktuelle Einstellungen und konfigurierte
optionale Funktionen. Schreibfreigabe, vorübergehende Ladegrenzen-Übersteuerung,
manuelle Betriebsart, gelernte Profile und Aufzeichnungen werden **nicht**
kopiert. **Strategie berechnen** ist zunächst aus und kann im letzten Schritt
bewusst aktiviert werden; die Schreibfreigabe bleibt beim neuen Eintrag immer
aus. Neue Profile müssen sich erst aufbauen oder erneut importiert werden;
auch bisherige Strategie- und Balancing-Zustände bleiben beim Shadow-Eintrag.
Ändern sich Verbindung, gespeicherte Konfiguration oder andere feste
Einstellungen während der Einrichtung, muss die Übernahme neu gestartet
werden. Normale Strategieaktualisierungen des Ladepreises und Netzlade-Boosters
blockieren das Speichern nicht: Es gelten die im Assistenten angezeigten und
geprüften Werte. Bei deaktivierter Strategie wird die Tarifseite übersprungen;
der erfasste Netzlade-Booster wird trotzdem übernommen. Vor einer späteren
Strategieaktivierung die Tarifwerte prüfen. Nicht geladene Einträge zuerst
aktivieren und ihre Verbindung prüfen.

In Beta 6 und älter gibt es diese Einstellungsübernahme noch nicht. Dort ist
ein neuer Eintrag mit abgewähltem Shadow-Modus und manueller Eingabe der
Einstellungen erforderlich.

### Schreibender Betrieb

Die Strategieparameter werden über **Konfigurieren** gepflegt. Die zugehörigen
Zahl- und Konfigurationsschalter-Entitäten sind bei neuen Einträgen standardmäßig
deaktiviert und können bei Bedarf aktiviert werden. Die Vorgaben sind konservativ. Automatisches Netzladen und Balancing über das Netz sind bei neuen Einträgen
ausgeschaltet. **Reserveplanung mit Netzladen erlauben** aktiviert auch
preisabhängiges Vorladen und Laden bei negativen Preisen; es ist keine reine
Haltefunktion. Gespeicherte und ausdrücklich importierte Werte bleiben erhalten.

Beide Netzladefälle laden nur im günstigsten Fenster vor der nächsten Spitze:
Einstieg bis 0,5 ct/kWh über dem Horizont-Tief `min_preis_vor_peak_ct`. Läuft
`Akku Netzladen` bereits, hält das Fenster bis 1,5 ct/kWh über dem Tief. Diese
Hysterese verhindert, dass Viertelstundenpreise mit Schwankungen um bis zu
1 ct/kWh den Modus im Viertelstundentakt zwischen Vorladen und Reserve halten
umschalten. Ein teurer Slot beendet das Vorladen unabhängig davon, weil der
Spread zur Spitze dann fehlt.

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
halten eine bereits aktive Sperre weiterhin fest. Optional kann je Ladepunkt
der evcc-Binärsensor `smart_cost_active` ausgewählt werden. Dann sperrt auch
aktives Laden im Modus `pv` die Entladung, solange Smart Cost aktiv ist.
Normales PV-Laden ohne aktives Smart Cost sperrt nicht. Fehlt der konfigurierte
Smart-Cost-Wert während eines erkannten Ladevorgangs im Modus `pv` oder meldet
die Quellintegration ihn als unbekannt oder nicht verfügbar, gilt der Ladepunkt
als unverfügbar. Die Automatik hält dann eine
bereits aktive Sperre; in manueller Betriebsart wird die Entladung
vorsorglich gesperrt.
Die Modi `now` und `minpv` sperren bei aktivem Laden weiterhin unabhängig von
der optionalen Smart-Cost-Quelle.
Modus, Ladestatus und Smart Cost bleiben gültig, solange die Quellintegration
keinen ungültigen Zustand meldet. Deshalb muss die Integration oder MQTT-Bridge
einen Verbindungsausfall an Home Assistant weitergeben: als `unavailable`, über
ein Verfügbarkeitstopic oder mit `expire_after`.

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

**Letzte SMA-Steuerwerte** zeigt die zuletzt durch den Modbus-Transport
quittierten Register und Werte als kompakten String, etwa `40151=803, 40793=0`.
Die Attribute enthalten die Registerfolge, den angeforderten Modus, den
Zeitpunkt der letzten quittierten Schreiboperation und den Ausführungsstatus.
Das ergänzt den bisherigen Sensor **Letzter Schreibzugriff**, der nur den
Zeitpunkt zeigt.

Eine vollständige Folge meldet `completed`. Nach Fehler oder Überholung kann
die Anzeige auch die quittierten Schreiboperationen des anschließenden
Pauseversuchs enthalten (`failed_safe_pause` bzw. `superseded_safe_pause`).
Scheitert dieser Versuch, bleiben nur die tatsächlich quittierten Teile mit
Status `failed` bzw. `superseded` sichtbar. Ein Timeout kann trotzdem am Gerät
angekommen sein; solche unbestätigten Schreiboperationen werden nicht als
quittierte Werte ausgegeben. Ohne neue quittierte Werte bleibt der vorherige
Nachweis mit seinem ursprünglichen Zeitpunkt stehen. Den aktuellen Fehler
zusätzlich unter **Letzter Fehler** und **Befehlsnachweis** prüfen.

Die Anzeige ist **keine unabhängige Registerrücklesung** und kein Nachweis der
physischen Wirkung. Sie beginnt nach Neustart oder Neuladen leer; im Shadow-Modus
bleibt sie leer. Die detaillierten Attribute werden nicht mit jedem Abruf in
Recorder gespeichert. Bei Huawei bleibt der vorhandene Befehlsnachweis
maßgeblich, da dort über HA-Steuerentitäten statt direkt auf SMA-Register
geschrieben wird.

Die drei Nachweisstufen werden getrennt bewertet:

| Stufe | Belastbare Aussage | Derzeitiger SMA-Stand |
|---|---|---|
| Transport | Die vollständige Modbus-Schreibfolge endete ohne gemeldeten Fehler. | Als **Adapterausführung abgeschlossen** sichtbar. |
| Sollwertübernahme | Der Wechselrichter meldet den tatsächlich übernommenen Modus und Sollwert aus einer unabhängigen Quelle. | Nicht unterstützt. Die verwendete Registerfamilie wird nicht zurückgelesen. |
| Physische Wirkung | Eine frische Gerätemessung zeigt die erwartete Lade- oder Entladerichtung beziehungsweise den Stillstand. | Nur als Beobachtung prüfbar; kein Nachweis des exakten Sollwerts. Bei SMA nennt der Befehlsnachweis den Abschlusszeitpunkt des frischen Modbus-Lesevorgangs und den Ausführungszeitpunkt getrennt. Bei Huawei bleibt der Messzeitpunkt leer, solange der Gerätezeitpunkt der zugrunde liegenden HA-Entität nicht sicher weitergegeben wird. |

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

Für jede geprüfte Modell-/Firmwarekombination vor dem Versuch eine passende
Leistungstoleranz, Beobachtungsdauer und lokale Abbruchmöglichkeit festlegen.
Sollwertübernahme nur dann als geprüft festhalten, wenn eine unabhängige
Geräteanzeige oder Schnittstelle den übernommenen Modus und Wert bestätigt.
Fehlt diese Quelle, bleibt dieser Nachweis offen, auch bei passender
Leistungsrichtung. Ein stabiler Modus muss außerdem über mindestens zwei
tatsächlich ausgeführte Befehlserneuerungen beobachtet werden. Der reguläre
Erneuerungsabstand beträgt 120 Sekunden.

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

Die reale Ausfallabnahme umfasst getrennt das Ausschalten der Schreibfreigabe,
einen geordneten HA-Neustart sowie einen beaufsichtigten Kommunikations- oder
HA-Ausfall während aktiver Steuerung. Während HA nicht erreichbar ist, muss
die Gerätewirkung lokal beobachtbar bleiben. Je Versuch festhalten, ob und
wann der Akku pausiert, einen alten Sollwert weiterverfolgt oder in
Eigenregelung wechselt. Nach Wiederverbindung Geräteidentität, aktuelle
Messwerte und Bereitschaft prüfen; die vorherige manuelle Modusauswahl darf
nach einem HA-Neustart nicht wiederhergestellt sein.

Zum Abschluss neue Schreibzugriffe ausschalten, den tatsächlichen
Gerätezustand prüfen und den vorbereiteten Rückweg aus
[Migration und Rückweg](migration.md#rückweg) ausführen. Die bisherige
Steuerung erst wieder aktivieren, wenn kein neuer Writer mehr läuft; ihre
erwartete Wirkung anschließend erneut beobachten. Eine verbleibende
0-W-Begrenzung ist keine bestätigte Rückgabe an die Eigenregelung. Für Huawei
eine ausstehende Pause am bisherigen Gerätepfad abschließen, bevor die alte
Steuerung wieder schreibt. Zusätzlich die fortbestehenden TOU- und
Netzladeeinstellungen aus [Huawei-Steuerung](../HUAWEI_CONTROL.md) prüfen.

## Meldungen bei Quellenfehlern und Neustart

Quellen- und Verbindungsfehler müssen mindestens 60 Sekunden anhalten, bevor
eine Meldung entsteht. Solange derselbe Fehler besteht, erzeugen weitere
Abfragen keine erneute Meldung. Nach 60 Sekunden ohne Fehler wird der Vorfall
beendet. Schreibstillstand, Sperrverletzung und eine ausstehende Pause werden
ohne diese Wartefrist gemeldet; für den ersten Tibber-Abruf gilt die gesonderte
Startfrist.

Ein Neuladen der Integration oder HA-Neustart beginnt eine neue
Vorfallserkennung. Ein weiterhin vorhandener Quellenfehler kann deshalb nach
erneuten 60 Sekunden wieder melden. Häufige Neustarts können diese Meldungen
wiederholen. Der Shadow-Eintrag verschickt keine solchen Störungsmeldungen;
seine Fehler bleiben in den Diagnoseanzeigen sichtbar.

Ist **Strategie berechnen** aktiv und die Alleinsteuerung bestätigt, die
Schreibfreigabe aber 15 Minuten lang aus, meldet Opti Akku, dass der Akku nicht
gesteuert wird. Wer nur beobachten will, schaltet die Strategie in den Optionen
ab. Verwirft ein Neustart eine zuvor aktive Schreibfreigabe, etwa wegen
geänderter Gerätebindung oder ungültiger gespeicherter Einstellungen, steht der
Grund im letzten Fehler, im Log und in der Diagnose unter
`write_restore_blocked`.

Bei unveränderten, aber gültigen Messwerten die
[Altersgrenze der Quelle](configuration.md#unveränderte-externe-messwerte-und-ausfälle)
prüfen. Die Einstellung 0 vertraut ihrer HA-Verfügbarkeit. Tatsächlich
fehlende oder nicht verfügbare Werte bleiben Fehler; ihre Zeitstempel müssen
nicht künstlich erneuert werden.

## Diagnose herunterladen

Unter **Einstellungen -> Geräte & Dienste -> Opti Akku -> Drei-Punkte-Menü ->
Diagnose herunterladen** stellt Home Assistant eine kompakte Support-Datei
bereit. Sie enthält Betriebsart, Verbindungs- und Funktionsstatus sowie
zusammengefasste Fehlercodes. Hostnamen, IP-Adressen, Seriennummern, Entity-IDs,
Fahrzeugdaten, Messwerte und Historien werden nicht exportiert. Prüfe die Datei
trotzdem vor einer Veröffentlichung und teile sie bevorzugt über die
Issue-Vorlage statt als vollständige Home-Assistant-Konfiguration.

### Wie viel Energie zum Nachfüllen fehlt

Die rein beobachtende Bedarfsprognose zeigt zusätzlich zu `refill_covered`:

- `refill_missing_kwh`: fehlende Batterieenergie bis zum Nachfüllziel, mindestens 0.
- `refill_coverage_percent`: Anteil des Nachfüllziels, den der prognostizierte
  Nettoüberschuss nach Ladeverlusten decken kann, höchstens 100 %.

Beispiel: Ein Ziel von 1,180 kWh und erwartete 1,173 kWh bedeuten 0,007 kWh
fehlende Energie und etwa 99,41 % Deckung. Das zeigt die Größenordnung hinter
einem `refill_covered: false`, statt eine neue Toleranzschwelle einzuführen.
Die Werte sind gerundet; solange rechnerisch etwas fehlt, bleibt die angezeigte
Deckung unter 100 %. Ein Abstand unter der Anzeigeauflösung kann als 0,000 kWh
erscheinen. `refill_covered` vergleicht weiterhin die ungerundeten Energien.

Der Bezugszeitraum steht in `refill_horizon_end`; es handelt sich nicht um die
PV-Tagesmenge. `refill_profile_ready` bleibt ein gesonderter Qualitätsnachweis.
Diese Angaben sind keine Ladefreigabe und ändern weder Reserve noch Steuerung.
