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

Die Aufzeichnung schreibt im Hintergrund. Solange ein Schreibvorgang läuft,
werden weitere Stichproben ausgelassen, damit die Steuerung weiter aktualisiert
werden kann. Die Zeitstempel zeigen diese Lücken; fehlende Werte werden nicht
nachgetragen. Start und Stopp warten auf einen noch laufenden Journalzugriff,
bevor die Sitzung gewechselt wird.
Solange ein Start oder Stopp läuft, wird eine weitere Aktion mit einem Hinweis
abgewiesen.
Bleibt die Festplatte dauerhaft hängen, können auch das Entladen und Neuladen
der Integration auf laufende Speicherzugriffe warten. Bei freigegebenen
Schreibzugriffen und erreichbarem, identifiziertem Gerät wird der Pause-Befehl
zuvor versucht. Das Warten bestätigt keine physische Wirkung dieses Befehls.

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

In teuren und sehr teuren Preisphasen entlädt der Akku bei Hausbedarf. Tagsüber
setzt die Strategie dafür „Akku Dynamisch“ statt „Akku nur Entladen“, solange
**PV-Überschussladen erlauben** eingeschaltet ist und der Ladedeckel nicht
erreicht ist. So geht Solarüberschuss während einer Abendspitze in den Akku
statt ins Netz. Nachts bleibt es bei „Akku nur Entladen“.

Die prognose- und SoC-abhängige Ladedrossel (schonendes Laden an guten Tagen)
wird bei PV-Spitzen gelöst: Ist **PV-Überschussladen erlauben** eingeschaltet,
steht die Strategie auf „Akku Dynamisch“ oder „Akku nur Laden“ und gingen mehr
als 200 W Überschuss vor dem Akku ins Netz (Einspeisung − Netzbezug +
Akkuleistung), lädt der Akku unter 85 % SoC mit **Max. Ladeleistung**. Die
Freigabe hält, solange der SoC unter 87 % liegt; verschwindet der Überschuss,
endet sie erst nach 60 Sekunden durchgehend ohne Überschuss, damit kurze
Wolkeneinbrüche keine Schreibsequenzen am Wechselrichter auslösen. Den
Halte-Zustand führt der Binärsensor **Opti PV Spitze** (Attribut
`kein_ueberschuss_seit`); die geplante Ladeleistung zeigt im Attribut
`drossel_frei`, ob die Freigabe gerade wirkt. Temperaturgrenzen, Balancing-Taper
und die manuelle Ladegrenze bleiben davon unberührt und sperren sofort; fehlen
Einspeise-, Bezugs- oder Akkuleistung, Temperatur oder SoC, fällt der
Binärsensor sofort auf `off` und verwirft den Timer. Nach einem Neustart oder
einer Wiederherstellung älter als 90 Sekunden braucht die Freigabe einen neuen
vollen Einstieg. Ist er jünger, gilt ein wiederhergestellter Halte-Zustand
weiter, bis die reguläre Rücknahmebedingung erfüllt ist. Im Modus „Akku Netzladen“ gilt die Freigabe nie,
damit Netzladen nicht mit Maximalleistung aus dem Netz zieht. Schalten
Arbitrage-Halten oder die Auto-Vorbereitung nachträglich auf „Akku nur Laden“
oder „Akku Dynamisch“, hebt die Steuerung die Drossel unter denselben
Bedingungen ebenfalls auf (Hinweis `PV-Spitze` in `charge_power_limits`).

Die eigentliche Steuerung hat drei getrennte Ebenen:

1. Der Schalter **Strategie berechnen** ist die Hauptfreigabe für die Steuerung,
   einschließlich der manuellen Modi. Ist er aus, gilt immer Pause.

2. In den Integrationsoptionen muss bestätigt werden, dass keine zweite
   Steuerung parallel auf den Wechselrichter schreibt.
3. Erst der Schalter **Schreibzugriffe freigeben** erlaubt Modbus-Schreibzugriffe.

Die automatische MinSOC-Regel sperrt Entladung bei oder unter dem eingestellten
Minimum. Nach diesem Eintritt hält sie den Schutz bis drei Prozentpunkte darüber;
bei engem Abstand zum Maximum wird das Band auf den halben Abstand begrenzt.
Bei MinSOC 10 % gibt sie also ab 13 % wieder frei. Der eigene Schutzzustand bleibt
über Neustarts und ungültige SoC-Messungen erhalten. Solche Routinezustände
(Haltezustände, Zähler, Berichte) schreibt Opti Akku höchstens einmal pro
Minute und nur nach einer Änderung; ein geordneter Stopp oder HA-Neustart
speichert sofort, ein harter Absturz verliert höchstens die letzte Minute.
Bewusste Aktionen wie Schreibfreigabe, Einstellungen oder Pause speichern
weiterhin direkt. Änderungen der Min-/Max-Grenzen
setzen den Haltezustand zurück; liegt der SoC über dem neuen Minimum, wird
der Schutz freigegeben. Andere Gründe für „Akku nur Laden“ aktivieren ihn nicht.
Zulässiges Peak-Vorladen und Netzladen unter der Einspeisevergütung haben auch
bei oder unter MinSOC Vorrang, denn „Akku Netzladen“ entlädt nicht. Ein leerer
Akku kann so vor einer Preisspitze günstig nachladen. Meldet der
Wechselrichter dabei `not_ready` (bei SMA-Hybridgeräten nachts mit leerem Akku
üblich), wird dieses Netzladen nicht geschrieben: Die Bereitschaftssperre gilt
unverändert, der Befehl folgt erst nach `ready`. Netz-Balancing ist nur
innerhalb des Haltebandes möglich; bei oder unter MinSOC behält die
Entladesperre dort Vorrang. Die Schutzregeln der manuellen Modi gelten separat weiter.
Der Schutz im Halteband hat wie die harte MinSOC-Regel Vorrang vor der
PV-Vorbereitung fürs Auto. Dadurch kann der Hausakku während einer Autoladung
Solarüberschuss aufnehmen, bis die Freigabegrenze erreicht ist.

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
aktives Laden im Modus `pv` (evcc vor 0.316) oder `smart` (ab 0.316) die
Entladung, solange Smart Cost aktiv ist. Normales PV-Laden ohne aktives Smart
Cost sperrt nicht. Fehlt der konfigurierte Smart-Cost-Wert während eines
erkannten Ladevorgangs in einem dieser Modi oder meldet
die Quellintegration ihn als unbekannt oder nicht verfügbar, gilt der Ladepunkt
als unverfügbar. Die Automatik hält dann eine
bereits aktive Sperre; in manueller Betriebsart wird die Entladung
vorsorglich gesperrt.
Die Modi `now` und das ältere `minpv` sperren bei aktivem Laden weiterhin
unabhängig von der optionalen Smart-Cost-Quelle. Die neue evcc-Option
`alwaysCharge` innerhalb von `smart` wird ohne eigene HA-Quelle nicht erkannt:
Sie sperrt die Hausakku-Entladung nur bei aktivem Smart Cost. Wer unabhängig
davon eine Entladesperre braucht, verwendet `now`. Unbekannte Moduswerte gelten
als unverfügbar und lösen keine neue automatische Sperre aus. In manuellen
Betriebsarten wird die Entladung bei fehlendem Modus vorsorglich gesperrt.
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

Eine Sperrverletzung liegt vor, wenn der Akku in **Pause**, **nur Laden**,
**Netzladen** oder **nur Entladen** länger als 30 Sekunden mit mehr als 200 W in
die gesperrte Richtung arbeitet. Bewertet wird nur ein bestätigt geschriebener
Befehl für genau diesen Modus, frühestens 10 Sekunden nach dem Moduswechsel.
Opti Akku erneuert die Sollwerte danach höchstens dreimal im Abstand von
mindestens 30 Sekunden. Hält die Verletzung an, bleibt die Meldung aktiv, und
es gilt wieder der reguläre Erneuerungsabstand von 120 Sekunden.

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

Bei SMA zeigt der Diagnosesensor **Wechselrichter-Bereitschaft** im Attribut
`inverter_status` den zuletzt gelesenen Betriebszustand aus Register 33003.
Nur die konfigurierten Betriebscodes (Standard 235 und 2119) gelten als
schreibbereit; jeder andere Wert ergibt `not_ready`. Fehlt der Wert oder ist
er ungültig, steht der Sensor auf `offline` und das Attribut ist leer. Nach
einer Störung meldet der erste gültige Betriebscode `recovering`, erst der
zweite `ready`. Das Attribut wird im
Verlauf gespeichert, damit sich eine nächtliche Bereitschaftsmeldung
nachträglich einem Gerätezustand zuordnen lässt.

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

Beim nativen Tibber-Preisbezug wartet Opti Akku, bis die Tibber-Integration
vollständig geladen und ihr Preisdienst verfügbar ist. Die Bereitschaft wird
bei regulären Aktualisierungen nach frühestens 15 Sekunden erneut geprüft,
ohne Tibber dabei aufzurufen. Je nach Terminierung kann ein weiterer Takt vergehen.
Auch wenn Tibber länger als fünf Minuten zum Starten braucht, beginnt der
Preisabruf danach automatisch; Opti Akku muss nicht neu geladen werden.
Nach einem tatsächlichen Abruffehler beträgt die Wartezeit weiterhin fünf
Minuten. Ohne gültige Preise bleibt die preisabhängige Strategie in Pause.
Für die Meldung fehlender Preise gilt beim Start weiterhin die sechsminütige
Startfrist.

Ist ausschließlich die optionale PV-Erzeugungsquelle ungültig und steht
`sun.sun` auf `below_horizon`, entfallen die HA-Störmeldung und der Push für
Quellenfehler. Der Fehler bleibt in der Diagnose sichtbar. Bei Tageslicht,
unbekanntem Sonnenstand oder weiteren Quellenfehlern gilt die Meldefrist von
60 Sekunden weiterhin. Steuerung und andere Alarme ändern sich dadurch nicht.

Meldet ein erreichbarer Wechselrichter nachts `not_ready`, während der SoC
bei oder unter MinSOC liegt oder der MinSOC-Schutz noch hält, ist das ein
normaler Standby mit leerem Akku (#103), keine Störung. Es entstehen dann
weder HA-Störmeldung noch Push. Der Diagnosesensor
**Wechselrichter-Bereitschaft** bleibt auf `not_ready` und zeigt zusätzlich
das Attribut `standby: night_empty` neben dem rohen `inverter_status`; die
Diagnose führt den Zustand unter `health.standby`. Schreibzugriffe bleiben
bis `ready` gesperrt. `offline`, `recovering`, Tageslicht oder ein nicht
leerer Akku melden weiterhin nach 60 Sekunden.

Ein Neuladen der Integration oder HA-Neustart beginnt eine neue
Vorfallserkennung. Ein weiterhin vorhandener Quellenfehler kann deshalb nach
erneuten 60 Sekunden wieder melden. Häufige Neustarts können diese Meldungen
wiederholen. Der Shadow-Eintrag verschickt keine solchen Störungsmeldungen;
seine Fehler bleiben in den Diagnoseanzeigen sichtbar.

Ist **Strategie berechnen** aktiv und die Alleinsteuerung bestätigt, die
Schreibfreigabe aber 15 Minuten lang aus, meldet Opti Akku, dass der Akku nicht
gesteuert wird. Bei einem absichtlich lesenden 24-Stunden-Vergleich ist das
erwartet. Die Strategie muss für diesen Vergleich eingeschaltet bleiben.
Außerhalb eines solchen Vergleichs die Schreibfreigabe prüfen. Wer dauerhaft
nur beobachten will, schaltet die Strategie in den Optionen ab. Verwirft ein
Neustart eine zuvor aktive Schreibfreigabe, etwa wegen
geänderter Gerätebindung oder ungültiger gespeicherter Einstellungen, steht der
Grund im letzten Fehler, im Log und in der Diagnose unter
`write_restore_blocked`.

Bei unveränderten, aber gültigen Messwerten die
[Altersgrenze der Quelle](configuration.md#unveränderte-externe-messwerte-und-ausfälle)
prüfen. Die Einstellung 0 vertraut ihrer HA-Verfügbarkeit. Tatsächlich
fehlende oder nicht verfügbare Werte bleiben Fehler; ihre Zeitstempel müssen
nicht künstlich erneuert werden.

## Entscheidungen nachvollziehen

Warum die Strategie eine Betriebsart, einen Ziel-Ladestand oder eine
Ladeleistung gewählt hat, zeigen diese Entitäten.

- **Aktiver Modus**: Der Verlauf zeigt, wann die Betriebsart gewechselt hat.
- **Entscheidungsgrund**: der Grund als Text, ebenfalls mit Verlauf. Die
  Attribute nennen die interne Regel (`decision_id`), die Betriebsart nach
  Strategie und Auto-Vorbereitung (`engine_mode`) und Begrenzungen der
  Ladeleistung danach (`charge_power_limits`), etwa durch die
  Auto-Vorbereitung. Weicht der aktive Modus von `engine_mode` ab, hat eine
  Schutzregel oder die manuelle Auswahl übernommen. Bei ausgeschalteter
  Strategie steht dort `Beobachtung` und `decision_id` ist `unknown`.
- **Ziel-Ladestand**: Das Attribut `branch` zeigt das Verhältnis aus erwartetem
  PV-Überschuss bis Sonnenuntergang und Akkukapazität und die gewählte Stufe.
  `(gehalten)` bedeutet, dass die Hysterese die vorige Stufe hält, obwohl die
  aktuelle Rechnung eine andere ergäbe.
- **Geplante Ladeleistung**: Das Attribut `branch` zeigt Prognose-Score,
  SoC-Stufe, manuelle Ladegrenze, Temperaturdrosselung, die Obergrenze
  **Max. Ladeleistung**, die PV-Spitzen-Freigabe und die Balancing-Begrenzung,
  jeweils nur wenn sie greifen. Der Wert nach „Ergebnis“ ist die geplante
  Ladeleistung der Strategie, nicht die gemessene.
- **Opti Strategie Vorschau** (standardmäßig deaktiviert): Der Zustand ist der
  Modus, den die Strategie wählen würde. Die Attribute enthalten Grund,
  Ziel-SoC, Scores und Preisniveau.

Die `branch`-Attribute zeigen nur den aktuellen Stand unter
**Entwicklerwerkzeuge → Zustände**. Sie werden nicht aufgezeichnet, weil sich
der Text mit jedem Prozentpunkt SoC ändert. Ein eigenes Entscheidungsprotokoll
gibt es nicht.

## Planer im Schattenbetrieb

**Opti Schattenplaner Horizont** (`sensor.opti_plan_shadow`, Diagnose,
standardmäßig deaktiviert) rechnet alle paar Minuten einen Plan über die
bekannten Strompreise (Rest heute, morgen sobald veröffentlicht, höchstens
36 h): Wann aus dem Netz laden, wann entladen, wann halten, damit die Kosten
aus Netzbezug, entgangener Einspeisung und Zyklen über den Horizont minimal
werden. Eingaben sind SoC, Kapazität und Grenzen der Anlage, die Preisreihe,
die Solcast-Intervallprognose (konservativ P10, nicht der Median; Idee aus
Issue #155) und das stündliche Lastprofil der Peak-Reserve, ersatzweise der
60-Minuten-Hausverbrauch oder der eingestellte Peak-Verbrauch.

Der Planer **steuert nichts**. Er zeigt als Zustand den Modus, den er jetzt
wählen würde, in der Sprache der Leiter (`Akku Netzladen`, `Akku nur Laden`,
`Akku Dynamisch`, `Akku nur Entladen`, `Akku Pause`). Fehlen Daten, bleibt
der Zustand `unknown` und das Attribut `detail` nennt den Grund
(`price_series`, `pv_forecast_stale`, `horizon_short`, …). Die Attribute
ändern sich mit jeder Auswertung und werden deshalb nicht aufgezeichnet; nur
der Zustandsverlauf landet im Recorder.

Zum Auswerten nach einigen Wochen:

- `deviation` und `totals`: Sekunden je Abweichungsart heute (`today`) und
  seit Beginn (`total`), zeitgewichtet. `planner_grid_charge` heißt: der
  Planer würde aus dem Netz laden, die Leiter nicht; `planner_hold`: der
  Planer würde Energie halten, wo die Leiter entlädt; `planner_discharge`
  umgekehrt; `indifferent`: kein Modus könnte Energie bewegen (Akku leer oder
  voll), zählt nicht als Abweichung. `today_disagreement_percent` fasst das
  zusammen. `executable_mode` mit `planned_charge_w`/`planned_discharge_w`
  ist der Zug, der den Planfluss exakt reproduziert (auch Teilflüsse); der
  Zustand ist die gröbere Einordnung in die Leiter-Sprache.
- `expected_saving_eur`: erwartete Ersparnis des Plans gegenüber reinem
  Eigenverbrauch über den Horizont, mit denselben Prognosen. Das ist keine
  Ersparnis gegenüber der Leiter; die lässt sich nur im Replay bestimmen.
- `soc_target_next_hours`: geplanter SoC am Ende der nächsten zwölf Stunden,
  `first_grid_charge`: Beginn der ersten geplanten Netzladung.
- `terminal_ct_kwh`: Wert, den der Planer einer am Horizontende noch
  gespeicherten kWh beimisst (Anteil am mittleren Preis, gedeckelt auf die
  Kosten einer Nachladung in der günstigsten Stunde).

Die Rechnung läuft in einem Hintergrund-Thread; das Lösen (Hauptplan plus
Prüfplan der Variante B) hat zusammen ein Budget von 1 s, danach
`budget_exceeded`. Aufbau der Slots und Auswertung des Plans liegen außerhalb
(Millisekunden). Neu gerechnet wird höchstens alle zehn Minuten oder sofort,
wenn Preise, Prognose, Batteriegrenzen oder Planer-Optionen sich ändern;
reines Lastrauschen unter 50 W löst keinen neuen Plan aus.
Solange die Entität deaktiviert ist, wird nichts gerechnet und nichts
gespeichert.

Die Integration speichert Schnappschüsse der PV-Prognose (P10 und Median je
Stunde, so weit Solcast sie liefert) und der Preisreihe in
`.storage/opti_akku.<eintrag>.planner_history`, rollierend für 60 Tage. Ein
Eintrag entsteht nur, wenn sich Prognose oder Preisreihe geändert haben
(typisch ein Dutzend PV- und ein bis zwei Preis-Einträge am Tag); Zeitachsen
sind explizit (Startstunde plus Index, Preissegmente mit Rasterlänge), Lücken
bleiben erhalten. Gemessen im Test: 60 synthetische Tage mit 12 PV- und 2
Preisänderungen täglich ergeben rund 530 kB im
eingerückten Store-Format (Zahlenlisten sind als Strings kodiert); gespeichert
wird gebündelt spätestens 15 min nach einer Änderung. Die Datei bleibt lokal und ist der
Export für spätere Replays mit damals verfügbaren Prognosen; das Attribut
`forecast_history` zeigt Umfang, Zeitraum und Lesefehler. Ein defekter
Speicher stört weder Start noch Steuerung. Mit der Option
`planner.history_always: true` wird die Historie auch bei deaktivierter
Entität geschrieben (Standard aus).

Voraussetzung ist eine Solcast-Entität mit `detailedForecast` (halbstündlich)
oder `detailedHourly`; für das Quantil P10 muss `pv_estimate10` enthalten
sein, für den Median `pv_estimate`. Eine Stunde zählt nur, wenn ihre
Intervalle alle 60 Minuten abdecken (beide Halbstunden oder die volle
Stundenzeile); eine nur halb belegte Stunde ist eine Lücke, an der der
Planungshorizont endet, statt die halbe Energie zu lesen. Die Prognose muss
die aktuelle Stunde abdecken; als Alter gilt die letzte Zustandsänderung der
Entität (Grenze aus **Maximales Alter von Prognosen**, Standard zwölf
Stunden), was den tatsächlichen Abrufzeitpunkt bei Solcast nur annähert. Fehlt bei aktivem `grid_check_quantile` das Prüfquantil, gibt es
keine Netzladeempfehlung (`grid_check_p50_missing`).

Ohne Oberfläche einstellbar über die Optionen des Eintrags: `planner.wear_ct_kwh`
(Zyklenkosten, 3 ct/kWh Entladung), `planner.terminal_factor` (0,8),
`planner.quantile` (`p10`), `planner.min_benefit_ct` (Variante A: Plan nur,
wenn sein erwarteter Vorteil gegenüber Eigenverbrauch über den Horizont diesen
Betrag erreicht, sonst Eigenverbrauch; `plan_source` zeigt es) und
`planner.grid_check_quantile` (Variante B, z. B. `p50`: Netzladen nur, wenn
auch der Plan mit dem Median-PV netzlädt; `grid_check` zeigt das Ergebnis).

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
