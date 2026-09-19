### Bedarfsprognose als Beobachtung (Vorschau)

Unter **Konfigurieren → Bedarfsprofil und Peak-Reserve** lässt sich eine unabhängige
Vergleichsrechnung einschalten. Sie bleibt standardmäßig aus und verändert weder
Strategieeingaben noch Sollwerte oder Schreibfreigabe. Die gesonderte Option
„Stundenprofil für aktive Peak-Reserve verwenden“ beeinflusst dagegen die
Reserveplanung; sie ist standardmäßig aus.
Der Sensor **Bedarfsprognose (Beobachtung)** zeigt Lernstatus, Prognosegrundlagen,
Reservevorschlag und die aktuelle Strategie-Reserve. Das sind unterschiedliche
Horizonte: Die neue Rechnung betrachtet den gesamten Bedarf bis zur PV-Deckung,
die bestehende Strategie priorisiert teure Zeitfenster. Eine kleinere Zahl ist
allein noch kein Nachweis einer besseren Strategie.

- Die Integration lernt zeitgewichtet bis zu 42 Tage nach lokaler Stunde und
  Wochentag. Zwei passende Wochentage oder drei vergleichbare Werk-/Wochenendtage
  je benötigter Stunde sind nötig. Fehlende Historie wird ausdrücklich als
  **Lernphase** mit vorläufigen Zahlen ausgewiesen. Es gibt keinen automatischen
  Import alter Recorder-Daten.
- Ein separater elektrischer Wärmepumpenzähler muss im gewählten Hausverbrauch
  enthalten sein. Die Rechnung zieht ihn vom übrigen Verbrauch ab und lernt ihn
  separat. Die Warmwasser-Aktivmeldung trennt dessen Anteil von Raumheizung.
  Ohne diesen Zähler bleibt die Wärmepumpe im Gesamtprofil; ihre Zusatzlast lässt
  sich dann nicht getrennt prognostizieren.
- Sommermodus trennt Sommer- und Winterhistorie. Aktiver Heiz-/Warmwasserbetrieb
  berücksichtigt die aktuelle Wärmepumpenleistung für maximal eine weitere Stunde.
  Das ist noch kein thermisches Gebäudemodell und keine Wetter-/Heizlastprognose.
- Über mindestens 20 Minuten beobachtete zusätzliche Grundlast erhöht die
  Prognose. Dieser Zuschlag klingt über vier Stunden ab. Er erkennt die Last,
  nicht das Gerät; ein vergessener Server wird nicht automatisch ausgeschaltet.
- Optional zeigen ein Bedarfssignal oder Isttemperatur unter Soll minus 2 K
  erwartetes Warmwasser an. Dafür sind Wärmepumpenzähler, Warmwasser-Aktivmeldung
  und **gemessene elektrische kWh je Zyklus** erforderlich. Null heißt unbekannt.
  Bereits prognostizierte Warmwasserenergie wird angerechnet, Raumheizung nicht.
  Künftige Zeitprogramme und Speicherverluste werden noch nicht automatisch erkannt.
- Für eine zeitliche Reserve sind Solcast-Sensoren heute/morgen mit datiertem
  `detailedForecast` und `pv_estimate10` nötig. Tages-kWh allein reichen nicht.
  Verwendet wird P10 als konservatives Szenario; eine Garantie ist das nicht.
  PV-Deckung gilt erst bei mindestens einer zusammenhängenden Stunde mit 120 %
  des erwarteten Verbrauchs und wenigstens 0,5 kWh Nettoüberschuss. Damit wird
  ein rechnerisch passendes, aber energetisch bedeutungsloses Schwachlichtfenster
  nicht als Wiederaufladebeginn gewertet.
- Der Vorschlag deckt das größte kumulierte Energiedefizit bis dahin, mit
  20 % plus 0,2 kWh Puffer, 90 % Entladewirkungsgrad und eingestelltem Mindest-SoC.
  Eine über der nutzbaren Kapazität liegende Anforderung wird separat ausgewiesen.
  Zusätzlich zeigt der Bericht den danach innerhalb des ab Berechnungszeitpunkt
  maximal 24-stündigen Horizonts verfügbaren Nettoüberschuss, mit 90 %
  Ladeeffizienz, und ob dieser den errechneten
  Batteriebedarf innerhalb der nutzbaren Min-/Max-SoC-Spanne wieder auffüllen
  könnte. Potenziell verfügbare und tatsächlich speicherbare Energie werden
  getrennt gezeigt. Zwischenzeitliche Defizite werden vom aufgebauten Überschuss
  abgezogen. Ende und Länge des tatsächlich abgedeckten Prognosehorizonts
  bleiben sichtbar; ein negatives Ergebnis gilt nur für diesen Zeitraum. Diese
  Angaben verändern den Reservevorschlag nicht.
  Es wird keine aktive Entladung und kein bewusstes Einspeisen aus dem Akku ausgelöst.

Vor einer späteren Nutzung für aktive Reserveentscheidungen ist ein realer
Vergleich über mehrere typische Wetter- und Verbrauchstage nötig.

Die Lernhistorie liegt versioniert im privaten Integration-Store. Datenlücken und
Neustarts werden nicht interpoliert; Quellenwechsel setzen nur diese Historie
zurück. Fehler dieser Vergleichsrechnung pausieren die aktive Steuerung nicht.
Die umfangreichen Prognoseattribute werden nicht bei jedem Takt in den HA-Recorder
kopiert. Ein eingefrorener Prognosewert wird bis zum vorhergesagten PV-Beginn mit dem
tatsächlichen Hausverbrauch verglichen. Fehlende Messzeit wird ausgewiesen; bei
Lücken wird kein vollständiger Prognosefehler behauptet. Die letzten sieben
abgeschlossenen Vergleiche bleiben während der Laufzeit sichtbar; ein laufender
Vergleich überlebt Neustarts mit ausgewiesener Datenlücke. Das misst die
Verbrauchsprognose, nicht hypothetisch vermiedenen Netzbezug.

### Historische Vorbelegung der Bedarfsprognose (0.5.2b1)

Im Optionsmenü „Bedarfsprofil und Peak-Reserve“ lassen sich ein historischer
Hausverbrauchssensor sowie Außen- und Warmwasser-Ist-/Solltemperatur auswählen.
Der historische Sensor muss denselben Verbrauchsumfang wie der aktuelle
Hausverbrauch abbilden, beispielsweise ebenfalls ohne Wallbox. Bei bestehender
Legacy-Quellenzuordnung wird deren Hausverbrauchssensor verwendet.
Die Konfigurationsschaltfläche „Bedarfsprognose: Historie importieren“ liest einmalig
bis zu 42 Tage aus dem lokalen HA-Recorder. Sie benötigt eine aktivierte Beobachtung.
Bei einem separat konfigurierten Wärmepumpen-Leistungsmesser ist dieser Import
vorerst nicht verfügbar, da Stundenmittel keine zuverlässige thermische Aufteilung erlauben.

Importiert werden nur abgeschlossene Tage vor dem ersten live beobachteten Tag.
Wiederholungen ersetzen die historische Vorbelegung, sie addieren keine Messungen.
Live gelernte Werte, jüngste Grundlast, laufender Genauigkeitsvergleich und aktive
Akkusteuerung bleiben getrennt. Datenlücken werden nicht mit null gefüllt.
Stundenstatistiken liefern keine belegte Messabdeckung; daher bleiben ihre Prognosen
als historische Vorbelegung gekennzeichnet und ergeben alleine kein „profile_ready“.
Pro Prognoseintervall sind Datenherkunft, Zahl der Vergleichstage und unbekannte
Saisonkontexte sichtbar. Fehlende alte Sommer-/Winterzustände bleiben unbekannt.

Sommermodus und Wochentag können Vergleichstage eingrenzen. Optional kann die aktuelle
Außentemperatur ähnliche historische Stunden innerhalb von 3 °C bevorzugen, wenn
mindestens drei Vergleichstage vorhanden sind. Das gilt nur für die ersten vier
Prognosestunden, verwendet keine zukünftigen Isttemperaturen und ist standardmäßig
ausgeschaltet. Es ist kein Wetter- oder Gebäudemodell. Historische Heiz-/Warmwasserflags
und Warmwasser-Ist/Soll werden zunächst als Kontext gespeichert; sie bestimmen noch
keine zukünftigen Laufzeiten oder Strommengen. Die separaten Kontextfelder lösen
keine Warmwasser-Nachladung aus. Die bestehende kalibrierte Warmwasserfunktion bleibt
unverändert. Höhere aktuelle Grundlast wird nach ausreichender jüngster Messabdeckung
auch bei historischer Vorbelegung berücksichtigt.

Der Import benötigt weder Recorder-Schreibzugriff noch einen Neustart als eigene
Aktion und startet keinen zusätzlichen Batterie-Schreibzyklus. Er ersetzt keine
Auswertung an späteren, beim Lernen unbenutzten Tagen.

### Messquellenvergleich und Wiederanlauf (0.5.2b2)

Das optionale Menü „Messquellen und Bilanz vergleichen“ aktiviert eine rein lesende
Diagnose. Es ersetzt weder die aktive Hausquelle noch deren Frischeprüfung. Wählbar
sind die momentane Gesamt-Hausleistung, der Momentanwert nach Lastabzug, weitere
Wechselrichter, ausgeschlossene Lasten und bis zu acht zusätzliche Beobachtungsquellen.
Der Netzzähler muss den gesamten betrachteten Anlagenumfang erfassen.

Der Vergleich zeigt getrennt die native Gesamtbilanz, den Wert nach Lastabzug und
einen eigenen zeitgewichteten 60-Minuten-Mittelwert gegenüber dem Legacy-Mittelwert.
Eine im Legacy-System vorhandene Untergrenze ist explizit einstellbar. Unterschiedliche
Interpolation und zeitversetzte Messungen können auch bei korrekter Bilanz Abweichungen
verursachen. Fehlende oder veraltete zusätzliche Wechselrichter/Lastwerte werden nicht
zu null umgedeutet. Ist der Wallbox-Leistungswert veraltet, entfällt der Vergleich
nach Lastabzug; die unabhängige Gesamtbilanz kann weiter ausgewertet werden.

Für 48 Stunden bleiben Fünf-Minuten-Aggregate mit ausgewiesener Vergleichsabdeckung
lokal erhalten. Übergänge und grobe 15-Minuten-Stichproben enthalten Werte sowie
last_reported/last_updated/last_changed der ausgewählten Quellen. Maximal 256
Schnappschüsse werden gespeichert. Lücken über 90 Sekunden werden nicht überbrückt.
Die Details werden nicht bei jedem Aktualisierungszyklus erneut in den Recorder
geschrieben. Nach Neustart wird der aktuelle 60-Minuten-Vergleich neu aufgebaut.
HA-Meldezeitstempel sind kein Beweis einer neuen physikalischen Messung; ein Template
mit Zeittrigger kann auch alte Eingangswerte erneut melden. Daher immer die gesamte
relevante Datenkette betrachten. Die Beobachtung verlängert keine Fristen und setzt
keine Quellzeitstempel künstlich zurück.

Nach SMA-Verbindungsfehlern oder ungültigem Betriebsstatus zeigt ein eigener Sensor
den Wiederanlauf. Zwei aufeinanderfolgende Lesezyklen mit zugelassenem Betriebsstatus
sind erforderlich, bevor frisch berechnete Befehle wieder angewendet werden. Der
zusätzliche Schutz ersetzt keine Identitäts-, Quellen- oder Transaktionsprüfung des
Treibers. Huawei behält seinen vorhandenen Ablauf. Erwartetes InverterNotReady während
eines Wiederanlaufs gehört zur Verbindungsstörung. Echte Schreibfehler, Schreibstillstand
und Sperrverletzungen bleiben unverzögert alarmiert; bestehende Quellenwarnungen werden
nicht unterdrückt. „Wartet auf Betriebsbereitschaft“ bedeutet keine bestätigte Pause.


### Profilbasierte Peak-Reserve (0.5.2b3)

Die optionale aktive Berechnung verwendet für jedes teure Zeitfenster einen
passenden Stundenverbrauch aus dem gelernten oder importierten Hausprofil.
Profilwerte erhalten 20 % Prognosepuffer; fehlende Stunden verwenden den
konfigurierten Ersatzverbrauch. Preisfenster, Wiederaufladehorizont,
Entladewirkungsgrad und Min-/Max-SoC bleiben gleich. Der beobachtende Vorschlag
„gesamter Bedarf bis PV“ ersetzt die Peak-Reserve nicht.

Heizungs- und Warmwasserverbrauch ist im Hausprofil enthalten. Aktiver Heizbetrieb
setzt für die aktuelle und folgende Stundenperiode mindestens den aktuellen
Hausverbrauch beziehungsweise Ersatzverbrauch an, ohne einen zweiten Heizbedarf
zu addieren. Unbekannte konfigurierte Heizsignale oder ein erkannter, nicht
quantifizierter Warmwasserbedarf verhindern die Profilabsenkung. Soll-/Isttemperatur
beschreibt nur den aktuellen Bedarf; zukünftige Zeitprogramme und Sollwertwechsel
werden nicht vorweggenommen. Ein separater Wärmepumpenzähler und temperaturbasierte
Vergleichstagsauswahl werden für die aktive Peak-Berechnung noch nicht genutzt.
Eine vollständige zukünftige Heizbedarfsprognose ist damit noch nicht vorhanden.

Die Reserveplanung zeigt den tatsächlich für Peakstunden angesetzten Mittelwert,
Profilstunden, Prognosepuffer und Rückfallgrund. Profilwechsel löschen keine
Lernhistorie. Ein Abschalten der Profiloption stellt die feste Lastannahme wieder her.

Die Entladesperre beginnt bei der Reserve, nicht schon drei Prozentpunkte darüber.
Nach dem Halten wird erst oberhalb von Reserve plus zwei Prozentpunkten freigegeben.
Während der jeweils vorgesehenen teuren Stunden wird reservierte Energie weiterhin
freigegeben.

### Passiver Profilvergleich für die PV-Strategie

Bei eingeschaltetem Bedarfsprofil ergänzt der Bedarfsbericht einen strikt passiven
Vergleich für den Resttag-Score, den Morgen- und Sonnentag-Score sowie den Ziel-SoC.
Das gilt im Shadowmodus und parallel zu einer aktiven Steuerung. Der Vergleich
ändert keine Strategie-Entität, keine Reserve, keinen Modus und keinen
Schreibparameter. Der aktive Rechenweg bleibt unverändert; der Vergleich stellt
dessen Ergebnis einem Kandidaten gegenüber, der den erwarteten Verbrauch
stundenweise integriert.

Vollständig gelernte Stunden und mindestens 20 Minuten aktuelle Profilabdeckung
ergeben den Status `ready`. Historische Recorder-Werte, der konfigurierte
Ersatzverbrauch oder eine noch aufwärmende aktuelle Abdeckung ergeben `learning`.
Die profilbasierte Last bleibt dann sichtbar, Score und Zielkandidat jedoch nicht.
Fehlende Pflichtdaten werden je Vergleich als `data_missing` ausgewiesen. Ein
fehlender Solcast-Zeitverlauf (`no_pv_timing`) verhindert diesen Vergleich nicht,
weil er nur die vorhandenen Tages- und Restprognosen benötigt. Ein fälliger, aber
noch nicht zeitlich eingeplanter Warmwasserbedarf bleibt dabei als
`dhw_timing_unknown` in der Lernphase sichtbar.

Der Resttag reicht exakt bis zum heutigen Sonnenuntergang. Morgen und Sonnentag
verwenden ganze lokale Kalendertage einschließlich 23- oder 25-stündiger
Zeitumstellungstage. Eine kurzfristige Zusatzgrundlast klingt über höchstens vier
Stunden linear ab. Separat gemessene, gerade aktive Heizung oder Warmwasser setzt
höchstens in der ersten Stunde eine Untergrenze und wird nicht doppelt addiert.

Der Ziel-SoC-Vergleich verwendet dieselben Stufengrenzen und dieselbe Hysterese wie
die aktive Vorlage. Er beginnt bei der vor dem aktuellen Rechenschritt aktiven
Stufe und zeigt daher einen einzelnen vergleichbaren Schritt, keine fortlaufende
alternative Regelung. Sein Horizont reicht je nach Sonnenuntergang mindestens
0,5 und höchstens 12 Stunden voraus. Fehlt der nächste Sonnenuntergang, bleibt der
Vergleich `data_missing`; die aktive Vorlage verwendet in diesem Fall weiterhin
ihren bisherigen Sechs-Stunden-Ersatz. Bei freigegebenem Netzladen gilt auch im
Vergleich Max-SoC.
Der Support-Export enthält hiervon nur Status und Grundcodes, keine Energiewerte,
Zeitfenster, Profile oder Quellnamen.

### Passiver Energierestwert

Wenn Bedarfsprofil und Arbitrage-Anzeige eingeschaltet sind, ergänzt die
Arbitrage-Anzeige eine passive Restwertkurve für vorhandene Akkuenergie. Sie
ordnet die prognostizierte, nicht durch PV gedeckte Hauslast bis zum nächsten
belastbaren PV-Wiederaufladebeginn nach Strompreis:
Die erste verfügbare Kilowattstunde ersetzt damit den teuersten erwarteten
Netzbezug, weitere Energie schrittweise günstigere Restlast. Vom vermiedenen
Netzpreis werden die eingetragenen Entlade-Durchsatzkosten und der
Entladewirkungsgrad berücksichtigt.

Angezeigt werden unter anderem Prognose- und Preisabdeckung, Restlast,
Knickpunkt der Kurve sowie der rechnerische Wert des aktuellen und eines voll
nutzbaren Akkus innerhalb der eingestellten Min-/Max-SoC-Spanne. Historische
Ersatzprofile, ein fehlender Wiederaufladebeginn oder ein unvollständiger
Preishorizont bleiben als `learning` sichtbar. Die Rechnung extrapoliert keine
fehlenden Preise, bewertet keine Einspeisung aus dem Akku und gibt noch keinen
Soll-SoC vor. Standardmäßig verändert sie weder Halten, Laden, Entladen noch
eine bestehende Preis- oder Reserveentscheidung.

Optional kann **Restwert als automatischen Entladeschutz verwenden** aktiviert
werden. Der Schutz benötigt zusätzlich eine aktivierte, vollständig gelernte
Bedarfsprognose und steht zunächst nur für den SMA-Adapter zur Verfügung. Nach
einem Neustart müssen auch bei wiederhergestelltem Profil zunächst wieder
mindestens 20 Minuten gültige Verbrauchsmesswerte vorliegen. Der Schutz greift
ausschließlich bei bekannten automatischen Entladeentscheidungen. Ist die spätere
Grenzenergie mindestens um die eingetragene Marge plus 0,5 ct/kWh
wertvoller als die aktuell vermiedene Netzenergie nach Entladekosten,
setzt er `Akku nur Laden`. Zum Lösen gilt eine um 1 ct/kWh versetzte Schwelle.
Damit führen kleine Prognoseänderungen nicht alle 15 Sekunden zu einem
Moduswechsel. Der Schutz übernimmt keine Mindestlade- oder Entladeleistung und
startet kein Netzladen. Fehlende, ältere als 90 Sekunden oder nicht mehr zu SoC,
Kapazität und Min-/Max-SoC passende Berechnungen lassen die bisherige
Strategieentscheidung unverändert. Schutz- und Ladeentscheidungen sowie der
Vorrang eines ladenden Autos bleiben übergeordnet.

### PV-Vorbereitung fürs Auto (0.5.2b4)

Unter **Konfigurieren → PV-Vorbereitung fürs Auto** kann der Hausakku bei niedrigem
Fahrzeug-Ladezustand vorhandenen PV-Überschuss früher aufnehmen. Standardmäßig aus.
Einmalig Fahrzeug-SoC (%) und ein verlässliches Signal „Auto lädt“ auswählen;
Standardwerte: Ladebedarf unter 40 %, Hausakku vorbereiten bis 80 % (höchstens
konfigurierter Max-SoC). Ein optionaler Schalter für längere Abwesenheit sperrt
nur die Vorbereitung. Fahrzeuganwesenheit, Rückkehrzeiten und tägliche Freigaben
sind für die einfache Schwellenregel nicht nötig.

Optional kann ein `input_datetime` oder Zeitstempel-Sensor für die Abfahrt
gewählt werden. Zusammen mit Fahrzeugziel, nutzbarer Fahrzeugkapazität,
verfügbarer AC-Ladeleistung und Ladewirkungsgrad zeigt der Bericht benötigte
Akku- und AC-Energie, mittlere erforderliche Leistung, spätesten Ladestart und
eine einfache Machbarkeit bei durchgehend verfügbarer Ladeleistung. Eine reine
Uhrzeit gilt täglich; in einer nicht vorhandenen Sommerzeitstunde wird sie auf
die nächste gültige Ortszeit verschoben, in der doppelten Stunde gilt das erste
Vorkommen. Bis zum Ziel-SoC nutzt die PV-Vorbereitung das Abfahrtsziel statt der
einfachen Bedarfsschwelle. Ein abgelaufener absoluter Zeitpunkt bleibt im
Bericht sichtbar und fällt für die Vorbereitung auf die normale Schwelle zurück.
Die Integration steuert damit weiterhin keine Wallbox und kann den Ziel-SoC zur
Abfahrt nicht garantieren.

Die zusätzliche PV-Ladung startet nach einer Minute mit mindestens 300 W
Überschuss vor Hausakkuladung und bleibt ab 100 W aktiv. Laufende Hausakkuladung
zählt zum verfügbaren Überschuss, Akkuentladung wird abgezogen. Bei der einfachen
Schwellenregel endet der Fahrzeugbedarf ab Schwelle plus fünf Prozentpunkten;
bei aktiver Abfahrtsplanung endet er am eingestellten Fahrzeugziel. Nach erreichtem
Hausziel startet die Vorbereitung erst zwei Prozentpunkte darunter erneut.
Neustarts und Messlücken setzen die kurze Bestätigungszeit zurück. Der SoC benötigt ein HA-Reportingalter
von höchstens 24 Stunden; das ist kein Beweis eines neuen Fahrzeugabrufs, wenn
dessen Integration zwischengespeicherte Werte wiederholt meldet.

Die normale prognosebasierte Überschussstrategie wird um eine nachgelagerte
PV-Vorbereitung ergänzt. Preisreserve, Schutz- und Balancingentscheidungen bleiben
vorrangig. Die Funktion wählt nur PV-Ladung ohne Entladung, nie einen Netzlademodus;
Mindestladeleistung wird dabei null, die Ladeobergrenze höchstens der ermittelte
Überschuss. Sie senkt ein ohnehin höheres normales Ladeziel nicht ab.

Bei aktivem oder unbekanntem Fahrzeug-Ladesignal pausieren normale Hausakkuladung
und automatische Entladung, auch nachts. Höher priorisierte Schutz-, Balancing-, Reserve- und Preisladungen
bleiben möglich, insbesondere Vorladen vor einem Preispeak oder bei negativen
Preisen. Hausakku und Fahrzeug können dann gleichzeitig aus dem Netz laden;
die EV-Funktion ist keine globale Netzbezugs- oder Anschlussleistungsbegrenzung. Die bestehende Wallboxregelung bleibt zuständig für das Auto.
Mit aktivierter Funktion wird im Automatikbetrieb keine Mindestentladeleistung
erzwungen. Nach Ende der Vorbereitung deckt der Hausakku wieder normalen
Hausverbrauch, statt gezielt ins Netz entladen zu werden. Manuelle Betriebsarten
werden von dieser optionalen Funktion nicht verändert. Es gibt keine Garantie
für unterbrechungsfreie Prioritätswechsel zwischen zwei Geräteabfragen.

Der Diagnosesensor zeigt Vorbereitung, Ladebedarf, Datenlücken und Vorrangregeln.
Im Shadowmode ist dies nur eine berechnete Entscheidung. Keine zusätzliche
Wärmepumpen-, Fahrzeug- oder Wallboxsteuerung und kein Versprechen, dass die
verbleibende PV den Fahrzeugbedarf vollständig deckt.
