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
   **Akku Opti-Automatik** der Shadow-Instanz einschalten, damit sie echte
   Entscheidungen berechnet. Dies erteilt im Shadow-Modus keine Schreibrechte.
3. Auf der Geräteseite **24-Stunden-Shadow-Aufzeichnung starten** drücken.
4. **Shadow-Aufzeichnung** zeigt Status, Start, feste Deadline, Stichproben,
   Quellenfehler, Abweichungen, Einstellungsänderungen und Beobachtungslücken.

Die Aufzeichnung schreibt höchstens alle 15 Sekunden eine Stichprobe in
`<config>/opti_akku_shadow/<Sitzungs-ID>.jsonl`. Diese privaten Betriebsdaten
gehören nicht ins Repository und liegen nicht unter `www`. Jede neue Sitzung
bekommt eine eigene Datei; erneutes Drücken während einer laufenden Sitzung
verlängert sie nicht. Ein HA-Neustart erhält die ursprüngliche Deadline.

Nach 24 Stunden endet nur die Aufzeichnung. Die Integration bleibt strikt
lesend. `completed` bedeutet Fristende, nicht lückenlose Datenabdeckung.
Abweichende Modi sind zunächst Vergleichsbefunde: Die neue Instanz muss
Mittelwerte und Hysterese erst aufbauen und übernimmt alte Helferzustände
nicht automatisch. Ein Shadow-Test belegt keine physische Wirkung der
Schreibregister und ersetzt keinen späteren begleiteten Schreibtest.

### Schreibender Betrieb

Die Geräteseite enthält die Strategieparameter als Zahlen und Schalter. Die
Vorgaben sind konservativ. Netzladen und Balancing über das Netz sind zunächst
ausgeschaltet.

Die eigentliche Steuerung hat drei getrennte Ebenen:

1. Der Schalter **Akku Opti-Automatik** ist die Hauptfreigabe für die Steuerung,
   einschließlich der manuellen Modi. Ist er aus, gilt immer Pause.
2. In den Integrationsoptionen muss bestätigt werden, dass keine zweite
   Steuerung parallel auf den Wechselrichter schreibt.
3. Erst der Schalter **Schreibzugriffe freigeben** erlaubt Modbus-Schreibzugriffe.

Die Bestätigung während der Einrichtung aktiviert keine Schreibzugriffe. Bei
einem neuen Setup ist die Schreibfreigabe aus. Eine gespeicherte Freigabe wird
nur wiederhergestellt, solange die Single-Writer-Bestätigung weiter gesetzt ist.

Bei aktivierter Strategie stehen für SMA über **Betriebsart** `Strategie`
und neun manuelle Modi zur Auswahl. Ohne Strategie ersetzt `Beobachtung` den
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

Beim Ausschalten versucht die Integration, den Wechselrichter in Pause zu
setzen. Das ist ein Best-Effort-Vorgang. Hardware-, Netzwerk- oder
Protokollfehler können den Stopp verhindern. Auch eine bestätigte Pause ist
keine nachgewiesene dauerhafte Sperre. Ob und wann der Wechselrichter nach einem
Timeout oder HA-Ausfall seine Eigenregelung übernimmt, wurde nicht unabhängig
nachgewiesen. Der Diagnosesensor **Rückgabe an Gerätesteuerung** zeigt diese
fehlende Fähigkeit ausdrücklich als **Nicht unterstützt** an.
