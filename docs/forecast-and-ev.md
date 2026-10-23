### Bedarfsprognose als Beobachtung (Vorschau)

Unter **Konfigurieren → Optionale Funktionen → Bedarfsprofil und Peak-Reserve** lässt sich eine unabhängige
Vergleichsrechnung einschalten. Sie bleibt standardmäßig aus und verändert weder
Strategieeingaben noch Sollwerte oder Schreibfreigabe. Die gesonderte Option
„Stundenprofil in der aktiven Strategie verwenden“ beeinflusst dagegen die
Peak-Reserve und den Resttages-Score; sie ist standardmäßig aus.
Der Sensor **Bedarfsprognose** zeigt Lernstatus, Prognosegrundlagen,
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
abgeschlossenen Vergleiche bleiben auch nach einem Neustart sichtbar. Ein laufender
Vergleich überlebt Neustarts mit ausgewiesener Datenlücke. Das misst die
Verbrauchsprognose, nicht hypothetisch vermiedenen Netzbezug.

Die separat zugeordnete Wärmepumpen-Leistung verwendet dieselbe **Altersgrenze
für externe Messwerte** wie die übrigen Leistungsquellen (Standard 900 Sekunden).
Mit 0 kann ein unveränderter verfügbarer Wert weiter verwendet werden; die
Quellintegration muss einen Ausfall selbst als `unavailable` melden. Ungültige
Werte und Einheiten bleiben ausgeschlossen. Eine Wärmepumpen-Leistung unter 0
oder über dem Hausverbrauch wird weiterhin verworfen. Temperaturquellen und
datierte PV-Prognosen behalten ihre eigenen Altersgrenzen.

### Historische Vorbelegung der Bedarfsprognose (0.5.2b1)

Im Optionsmenü „Bedarfsprofil und Peak-Reserve“ lassen sich ein historischer
Hausverbrauchssensor sowie Außen- und Warmwasser-Ist-/Solltemperatur auswählen.
Der historische Sensor muss denselben Verbrauchsumfang wie der aktuelle
Hausverbrauch abbilden, beispielsweise ebenfalls ohne Wallbox. Bei bestehender
Legacy-Quellenzuordnung wird deren Hausverbrauchssensor verwendet.
Enthält der aktuelle und historische Hausverbrauch die Wallbox, kann im
Bedarfsprofil „Hausverbrauch enthält Wallbox-Ladung“ aktiviert werden. Dann
werden die konfigurierten EV-Leistungsquellen beim Live-Lernen, beim
Genauigkeitsvergleich und beim Import abgezogen. Bereits bereinigte Hausquellen
brauchen diese Option nicht. Jeder EV-Leistungsquelle muss ein Ladestatus
zugeordnet sein. Bei gültigem Ladestatus „aus“ gilt die EV-Last als 0 W, auch
wenn der Leistungswert seit dem letzten Laden unverändert oder nicht verfügbar
ist. Bei „an“ muss die Leistung gültig sein und die eingestellte Altersprüfung
bestehen. Fehlender oder ungültiger Ladestatus verhindert die Lernprobe.
Beim Import wird eine Stunde ohne EV-Statistik ausgelassen.
Die Aktivierung oder eine Änderung der EV-Quellenzuordnung setzt bisher gelernte
Profilwerte und Genauigkeitsdaten zurück; eine frühere historische Vorbelegung
muss neu importiert werden. Das gilt auch beim Wechsel des Ladestatus-Sensors.
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
Nach dem Einlesen läuft ein angenommener Import auch dann zu Ende, wenn der
aufrufende Dialog oder Dienstaufruf abgebrochen wird. Bis dahin bleibt das
bisherige Profil aktiv und ein weiterer Import gesperrt. Ändern sich die
Quellen oder wird der Eintrag beendet, wird das neue Profil nicht übernommen.

### Messquellenvergleich und Wiederanlauf (0.5.2b2)

Unter **Erweitert und Diagnose** aktiviert „Messquellen und Bilanz vergleichen“ eine rein lesende
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

Für den aktiven Resttages-Score wird dasselbe gelernte Onlineprofil bis zum
Sonnenuntergang integriert, sobald das Fenster vollständig aus Onlinewerten
besteht und mindestens 20 Minuten aktuelle Messabdeckung vorliegen. Eine kurze
Warmwasser- oder Heizlast wird dadurch als Teil ihres gelernten Stundenfensters
bewertet und nicht mit dem 60-Minuten-Mittel auf den ganzen Resttag verlängert.
Eine tatsächlich länger erhöhte Grundlast bleibt über den gemessenen
Profilaufschlag berücksichtigt. Solange das Profil lernt, historische oder feste
Ersatzwerte benötigt, Warmwasser zeitlich nicht zugeordnet werden kann oder ein
Eingang fehlt, bleibt automatisch die bisherige 60-Minuten-Hochrechnung aktiv.
Bei aktivierter Option „Hausverbrauch enthält Wallbox-Ladung“ verwenden die
60-Minuten-Berechnungen von Tages-, Morgen- und Sonnentag-Score eine gesonderte,
um gültige EV-Leistung bereinigte Hauslast. Bereits ausgeschlossene EV-Quellen
werden nicht erneut abgezogen. In den Anlagenmodi außerhalb von `legacy`
bleiben die Mittelung zeitgewichtet und die konfigurierte Mindestlast erhalten.
Bei fehlendem Ladestatus, ungültiger Ladeleistung oder einem Abzug größer als
die Hauslast dient deren gültiger unbereinigter 60-Minuten-Wert als Ersatz,
sonst die gültige aktuelle Hauslast. Ohne gültige aktuelle Hauslast ist der
Score nicht verfügbar. Das gilt auch ohne EV-Option: Fehlt
`sensor.opti_house_consumption_w`, sind Tages-, Morgen- und Sonnentag-Score
nicht verfügbar, statt mit einer erfundenen Last von 400 W zu rechnen.
Ansonsten gilt ohne EV-Option weiterhin die bisherige Berechnung.
`reserve_plan` zeigt `sun_day_score`, `long_horizon`, `horizon_reason` und
`score_load_error`. Ein neuer Eintrag startet bei gültigem Score zwischen den
Schaltschwellen mit kurzem Horizont; ein vorhandener langer Horizont bleibt
in diesem Bereich erhalten. Ein fehlender Score wählt weiterhin 36 Stunden.
Nachts (`sun.sun` unter dem Horizont) schwankt der Sonnentag-Score nur mit
der Hauslast, die Prognose selbst ändert sich nicht. Ein bereits langer
Horizont wird deshalb nachts nicht mehr abgeschaltet; ein kurzer wird nachts
weiterhin nur bei Score ≤ 1 lang. Die Abschaltschwelle (Score ≥ 3) wirkt erst
wieder nach Sonnenaufgang. Ein Neueintrag ohne Vorzustand startet nachts wie
bisher.

Morgen- und Sonnentag-Score teilen die Tagesprognose seit Issue #124 durch den
Tagesbedarf statt durch die auf 24 Stunden hochgerechnete letzte Stunde. Die
Engine führt dafür ein 24-Stunden-Mittel der Last im Minutenraster
(`sensor.opti_house_consumption_24h_w`, mit EV-Option
`sensor.opti_forecast_house_24h_w`) aus derselben Quelle wie die
60-Minuten-Kette: in den Anlagenmodi die um ausgeschlossene Verbraucher
bereinigte Basislast, im Legacy-Modus die Hauslast. Das Mittel bildet den
Nenner, sobald es belastbar ist (`coverage_ok`: mindestens die Hälfte der
erwarteten Minutensamples im Fenster und keine Lücke über zwei Stunden,
einschließlich der Zeit seit dem letzten Sample). Davor, nach einem Neustart
ohne gespeicherte Historie, nach einer längeren Messlücke oder bei ungültiger
EV-bereinigter Last gilt die bisherige 60-Minuten-Hochrechnung. Die
konfigurierte Mindestlast (`forecast_min_load_w`) begrenzt den Nenner wie in
der 60-Minuten-Kette nach unten. Es gibt keinen Kalibrierfaktor: Score 1
bedeutet weiterhin, dass die Prognose weniger als 15 % des Tagesbedarfs
deckt, Score 3 mindestens 25 %. Die Attribute `load_source`,
`tagesmittel_24h_w` und `tagesbedarf_w` der beiden Scores zeigen, welche Last
gerade im Nenner steht.

Der Nenner ändert sich nachts damit nur noch langsam; mit ihm ändern sich auch
die Reserve-Zweige (`Score < 3`) und `binary_sensor.opti_pv_reichtag` (10/9),
die weiterhin direkt am Score hängen. Der Nacht-Latch schützt ausschließlich
den bereits langen Peak-Horizont und bleibt als Rückfall für die
Hochrechnungsphase bestehen. Im Replay einer Referenzanlage (September 2025 bis
Oktober 2026, beide Prognosemodi) sanken die nächtlichen Umschaltungen des
langen Horizonts von 21 auf 5 und die nächtlichen Score-Kippstellen an der
Schwelle 3 von 176 auf 47 bei gleichen Nettokosten (±0,2 EUR); der Anteil
der Nachtschritte mit Score unter 3 stieg von 13,6 auf 18,2 %, weil das
Tagesmittel über der reinen Nachtlast liegt. Für die 24-Stunden-Statistiken
speichert die Engine stündliche Aggregate (Erstes/Letztes Sample, Summe,
Anzahl, größte Lücke) statt der Minutensamples; Mittel und Abdeckung werden
daraus nach einem Neustart rekonstruiert.

Für bereits als Peak klassifizierte Zeitfenster über 60 ct/kWh kommt ein
zusätzlicher Extrempreispuffer hinzu. Er beträgt
`0,25 × clamp((Preis - 60) / 40, 0, 1)` der bisherigen Lastannahme: bis
60 ct/kWh null, bei 80 ct/kWh 12,5 % und ab 100 ct/kWh 25 %. Der vorhandene
Profilaufschlag von 20 % bleibt dabei bestehen. Der neue Zusatz berücksichtigt
nur die tatsächlich verbleibende Dauer der extrem teuren Phase und wird mit
deren Ablauf abgebaut. Er ist auf zehn SoC-Punkte begrenzt; die gesamte Reserve
bleibt zusätzlich am Max-SoC gedeckelt.

Bei einem aktuellen Preis bis 60 ct/kWh bleibt der noch anstehende Extrembedarf
samt Zusatzpuffer vor vorzeitiger Peak-Entladung geschützt, auch wenn die volle
Reserve keinen Platz für einen weiteren Zuschlag lässt. Während der teuren Phase
darf die normale Peaklogik diese Energie nutzen. Balancing, Ladedeckel und der
Vorrang einer laufenden Autoladung bleiben erhalten.

Der Extrempreispuffer ist eine Sicherheitsheuristik und kein statistisch
kalibrierter Prognosefehler. Er erteilt keine Netzladefreigabe und verspricht kein
Nachladen während der teuren Spitze. Ist Prognose-Netzladen bereits freigegeben und das bestehende günstige
Ladefenster geeignet, kann das Peak-Vorladen auch den Zusatzpuffer auffüllen.
Dadurch sind bis zu zehn SoC-Punkte mehr Netzladung möglich; die bisherigen
Preis-, Ladefenster- und Abschaltgrenzen gelten weiter.

Bei selbst zugeordneten Preisentitäten müssen aktueller Preis und Preisreihe
denselben Tarif und dasselbe aktuelle Zeitfenster abbilden. Ein Stundenmittel als
aktueller Preis passt nicht zu einem abweichenden Viertelstundenpreis: Dann kann
die Freigabe für die teure Phase zu spät erfolgen. Der direkte Tibber-Bezug liefert
beide Werte aus derselben Preisreihe.

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
Profilstunden, Prognosepuffer, den tatsächlich addierten Extrempreispuffer in kWh
und SoC-Punkten sowie den Rückfallgrund. Profilwechsel löschen keine Lernhistorie.
Ein Abschalten der Profiloption stellt die feste Lastannahme wieder her.

Die Entladesperre beginnt bei der Reserve, nicht schon drei Prozentpunkte darüber.
Nach dem Halten wird erst oberhalb von Reserve plus zwei Prozentpunkten freigegeben.
Während der jeweils vorgesehenen teuren Stunden wird reservierte Energie weiterhin
freigegeben.

### Profilvergleich und aktive Resttagesnutzung

Bei deaktiviertem Bedarfsprofil fehlt das Attribut `strategy_comparison` künftig
auch im Shadow-Modus, statt einen leeren Vergleich mit Status `disabled` zu liefern.
Eigene Vorlagen sollten den fehlenden Schlüssel als deaktivierten Vergleich behandeln.
Das 24-Stunden-Shadow-Journal bleibt davon unabhängig nutzbar.

Bei eingeschaltetem Bedarfsprofil ergänzt der Bedarfsbericht einen Vergleich für
den Resttag-Score, den Morgen- und Sonnentag-Score sowie den Ziel-SoC. Ohne die
gesonderte aktive Profiloption bleibt dieser Vergleich strikt passiv. Ist die
aktive Profiloption eingeschaltet und das Onlineprofil vollständig bereit,
verwendet der Resttages-Score die stundenweise integrierte Last bis
Sonnenuntergang. Morgen- und Sonnentag-Score sowie der Ziel-SoC-Vergleich bleiben
passiv und unverändert.

Vollständig gelernte Stunden und mindestens 20 Minuten aktuelle Profilabdeckung
ergeben den Status `ready`. Historische Recorder-Werte, der konfigurierte
Ersatzverbrauch oder eine noch aufwärmende aktuelle Abdeckung ergeben `learning`.
Die profilbasierte Last bleibt dann sichtbar, Score und Zielkandidat jedoch nicht.
Fehlende Pflichtdaten werden je Vergleich als `data_missing` ausgewiesen. Ein
fehlender Solcast-Zeitverlauf (`no_pv_timing`) verhindert diesen Vergleich nicht,
weil er nur die vorhandenen Tages- und Restprognosen benötigt. Ein fälliger, aber
noch nicht zeitlich eingeplanter Warmwasserbedarf bleibt dabei als
`dhw_timing_unknown` in der Lernphase sichtbar.

Fällt die aktuelle Messabdeckung länger aus, verwendet der aktive Resttages-Score
wieder die bisherige 60-Minuten-Hochrechnung. Nach mindestens 20 Minuten
vollständiger aktueller Abdeckung wechselt er zurück auf das Onlineprofil. Dieser
bewusste Rückfall kann den Score an der Umschaltgrenze verändern.

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

### Schattenvergleich der Peak-Vorladeschwelle

Ist die Arbitrage-Anzeige konfiguriert und liegt ein aktueller Preis vor, vergleicht
sie bei aktiver Peak-Reserve und erlaubtem Netzladen zusätzlich zwei Schwellen: den
festen Netzlade-Spread (`opti_netzlade_spread_ct`, Standard 10 ct) und die aus
Zykluskosten und Wirkungsgraden hergeleitete Mindest-Spitzenpreis-Schwelle. Das
Attribut `precharge_comparison` nennt den aktuellen Spread, beide Schwellen und das
Urteil `both_allow`, `both_block`, `fixed_only` oder `derived_only`. `fixed_only`
heißt: Nur die feste Schwelle würde vorladen, die hergeleitete nicht.
`derived_only` heißt das Gegenteil. Verglichen wird ausschließlich das
Spread-Kriterium (`compares: spread_criterion_only`), nicht SoC-Grenzen oder
Ladefenster. Der Vergleich beeinflusst weder Modus noch Parameter oder
Entscheidungs-ID; die tatsächliche Vorladung folgt unverändert der festen Schwelle.

Unter `totals` werden die Sekunden je Urteil zeitgewichtet summiert, dazu die
Sekunden mit aktivem Vorladen und davon die, in denen die hergeleitete Schwelle
blockiert hätte. Lücken über 90 Sekunden, der erste Durchlauf nach einem Neustart
und rückwärts laufende Uhren zählen nicht. Ändert sich eine Annahme der
Arbitrage-Anzeige oder der feste Spread, starten die Zähler neu.

### Schattenvergleich Peak-Reserve gegen Refill-Reserve

Bei eingeschalteter Bedarfsprognose vergleicht das Unterattribut
`refill_reserve_comparison` der Bedarfsprognose die Haltegrenze der aktiven
Peak-Reserve (`hold_threshold_soc`, ersatzweise `planned_reserve_soc` des
Reserveplans) mit der Refill-Reserve `suggested_reserve_soc`. Die Refill-Reserve
deckt den gesamten Bedarf bis zur belastbaren PV-Deckung, die Peak-Reserve nur die
teuren Slots; beide haben also unterschiedliche Horizonte. Verglichen wird daher
nur, ob der aktuelle SoC an der jeweiligen Grenze halten würde
(`compares: hold_floor_only`). Das ist ein reiner Grenzvergleich: Halteband,
Preisstufen und weitere Bedingungen der Strategie bleiben außen vor. Er zeigt also,
wo die Grenzen auseinanderliegen, nicht welche richtig ist, und beeinflusst weder
Modus noch Parameter, Entscheidungs-ID oder Reserve. `peak_source` nennt, welcher
Wert als Peak-Grenze diente: `hold_threshold` gibt es nur, während die Strategie
bei aktivem Schreiben tatsächlich hält, sonst gilt `planned_reserve`. Die Summen
weisen beide Quellen getrennt aus. Er läuft nur bei aktiver Peak-Reserve und
ausgereiftem Refill-Profil; sonst nennt `status` den Grund (`not_applicable`,
`forecast_unavailable`, `profile_not_ready`, `soc_missing`, `soc_invalid`).

Das Urteil lautet `both_hold`, `both_release`, `peak_only` oder `refill_only`.
`peak_only` heißt: Nur die Peak-Reserve würde halten, die Refill-Reserve nicht.
`refill_only` heißt: Nur die Refill-Reserve würde halten, etwa weil sie höher
liegt als die Peak-Grenze. Unter `totals` stehen die Sekunden je Urteil, der
mittlere Abstand `mean_difference_soc` (Peak minus Refill) und
`disagreement_percent`. Lücken über 90 Sekunden, der erste Durchlauf nach einem
Neustart und rückwärts laufende Uhren zählen nicht; ein Fehler im Vergleich
unterbricht den Zeitzähler, ebenso eine ausgeschaltete Bedarfsprognose. Die
Summen liegen nur im lokalen Speicher der Integration.

### Erwartete Energie über der AC-Grenze

Die Diagnose-Entität `sensor.opti_clipping_forecast_kwh` (standardmäßig
deaktiviert) schätzt, wie viel PV-Energie heute noch oberhalb der
AC-Überschussgrenze anfällt. Grundlage ist das `detailedForecast` der
PV-Prognose für heute, ersatzweise der Solcast-Quelle aus dem Bedarfsprofil.
Je Halbstunde zählt der Anteil über der Grenze; der Zustand ist P50 ab jetzt,
P10, P90 und der ganze Tag stehen in den Attributen. Die P10- und P90-Werte
sind Summen der Halbstunden-Perzentile, keine Perzentile der Tagesenergie, und
zeigen daher nur eine grobe Spannweite. Dazu kommen freie
Kapazität bis zum maximalen Ladestand und die rechnerische Lücke
(`reserve_shortfall_kwh`). Ohne gesetzte Grenze bleibt der Wert `unavailable`
mit Status `not_configured`.

Solcast rechnet mit der im Solcast-Konto hinterlegten AC-Leistung. Ist dort die
Wechselrichterleistung eingetragen, ist die Prognose bereits gekappt und die
Schätzung zu niedrig; mehrere Halbstunden auf demselben Spitzenwert setzen dann
`forecast_capped_suspected`. Die Schätzung steuert nichts. Sie dient als
Datengrundlage für eine spätere, abschaltbare Ladeführung vor der Mittagsspitze
(#137).

### Schattenvergleich: PV-Ladung bis zum Preisfenster zurückhalten

Die Diagnose-Entität `sensor.opti_negative_price_hold` (standardmäßig
deaktiviert) prüft in jedem Zyklus, ob es sich lohnen würde, PV-Ladung bis zu
einem Preisfenster unter der Einspeisevergütung am selben Tag zurückzuhalten
(#136). Sie steuert nichts; die bestehende Netzlade-Regel bei schlechter
Prognose bleibt unverändert.

Das Fenster ist der erste noch nicht beendete zusammenhängende Zeitraum heute
mit einem Preis unter `opti_einspeiseverguetung_ct`. Vor dem Fenster lautet das
Urteil `hold`, wenn der Prognose-Score mindestens 3 beträgt, der Ladestand über
dem Mindest-SoC und einer aktiven Peak-Reserve liegt und der P10-Überschuss ab
Fensterbeginn bis Mitternacht nach Abzug der aktuellen Hauslast (60-Minuten-Wert)
und 90 % Ladewirkungsgrad mindestens das 1,2-Fache der bis zum maximalen
Ladestand fehlenden Energie deckt. Sonst nennt das Urteil den Grund:
`no_window`, `in_window`, `forecast_weak`, `min_soc`, `peak_reserve`, `full`
oder `insufficient_pv`. Ist die Peak-Reserve aktiv, ihr Wert aber unbekannt,
gibt es kein Urteil (`data_missing`). Benötigt wird das `detailedForecast` der
PV-Prognose für heute, ersatzweise die Solcast-Quelle aus dem Bedarfsprofil.

Unter `totals` werden zeitgewichtet summiert: Sekunden mit Urteil `hold` und im
Fenster, die vor einem bekannten Fenster und während `hold` geladene Energie,
sowie im Fenster geladene und eingespeiste Energie. Lücken über 90 Sekunden und
der erste Durchlauf nach einem Neustart zählen nicht; eine geänderte
Einspeisevergütung oder SoC-Grenze startet die Summen neu. Die Summen liegen nur
im lokalen Speicher der Integration in Home Assistant. Daraus lässt sich
abschätzen, wie viel Ladung eine zuschaltbare Regel in das Fenster verschieben
würde, bevor sie Schreibrechte bekommt.

### Schattenvergleich: Peak-Rangfolge für L1 und L2

Peak-Leiter L1 entlädt in jedem Slot mit Preisniveau `VERY_EXPENSIVE`. Das
Niveau ist nur ein Perzentil über heute und morgen. Bei wenig Inhalt kann L1 den
Akku deshalb an einer Abendspitze leeren, obwohl vor dem nächsten Nachladen eine
teurere Morgenspitze folgt. Die Diagnose-Entität `sensor.opti_peak_rank`
(standardmäßig deaktiviert) prüft in jedem Zyklus, ob eine Preisrangfolge die
Energie stattdessen zurückhalten würde. Sie steuert nichts; die Entladefreigabe
von L1 und L2 bleibt unverändert.

Berücksichtigt werden der laufende Slot und die kommenden Peak-Slots bis zum
nächsten belastbaren Nachladen. Peak-Slots sind wie bei
`sensor.opti_peak_reserve_soc` Slots mit Midrank-Perzentil ab 0,6 und mindestens
`opti_peak_min_aufschlag_ct` über dem Tiefstpreis im Horizont. Die Grenze ist
das Ende des PV-Horizonts der Peak-Reserve (`horizont_ende`) oder, falls früher
und `opti_prognose_netzladen` eingeschaltet ist, der erste spätere Slot, in dem
die Strategie aus dem Netz laden würde: Peak-Vorladen mit
`opti_netzlade_spread_ct` Abstand zum Durchschnitt der folgenden Peak-Slots im
Ladefenster oder ein Preis unter der Einspeisevergütung bei Prognose-Score
unter 3. Wie im Template gilt dabei die Sicht ab Beginn der vollen Stunde
dieses Slots; ein Nachladen nach dem letzten Peak-Slot zählt nicht. Die
Peak-Einstufung selbst stammt aus der aktuellen Sicht. Diese Grenze setzt voraus, dass Netzladen auch an oder unter dem
Mindest-SoC Vorrang hat (#154). Wie viel dabei nachgeladen wird, bildet der
Vergleich nicht ab.

Jeder Slot bekommt die erwartete Hauslast aus dem Peak-Lastprofil
(`sensor.opti_peak_load_profile`), ersatzweise `opti_peak_verbrauch_kw`. Der
nutzbare Inhalt über dem Mindest-SoC mit 90 % Entladewirkungsgrad wird vom
teuersten Slot abwärts verteilt; `threshold_price_ct` ist der Preis des letzten
bedienten Slots. Liegt der laufende Slot unter den bedienten, lautet das Urteil
während L1 oder L2 `discharge`, sonst `hold`. Bei jeder anderen Entscheidung
lautet es `not_peak`; `rank_discharges` zeigt dann trotzdem das Ergebnis der
Rangfolge. `reason` begründet das Urteil, `refill_at` und `refill_kind`
(`pv_horizon`, `grid_precharge`, `grid_below_feed_in`) nennen die Grenze.
Verbrauch zwischen den Peak-Slots ist nicht modelliert, und das Urteil hat keine
Hysterese.

Unter `totals` werden zeitgewichtet summiert: Sekunden mit L1 oder L2
(`peak_seconds`), davon mit Urteil `discharge` und `hold`, sowie die in beiden
Fällen tatsächlich entladene Energie. `discharged_while_hold_kwh` ist also die
Energie, die die Rangfolge für spätere, teurere Slots zurückgehalten hätte.
Lücken über 90 Sekunden und der erste Durchlauf nach einem Neustart zählen
nicht; ein geänderter Mindest-SoC, Spread, Mindestaufschlag, eine geänderte
Einspeisevergütung oder ein umgeschaltetes Netzladen starten die Summen neu.
Die Summen liegen nur im lokalen Speicher der Integration.

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
Vorrang eines ladenden Autos bleiben übergeordnet. Nach einer Sicherheitspause
muss der Entladeschutz erneut die höhere Eintrittsschwelle erreichen.

### PV-Vorbereitung fürs Auto (0.5.2b4)

Unter **Konfigurieren → Optionale Funktionen → PV-Vorbereitung fürs Auto** kann der Hausakku bei niedrigem
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
