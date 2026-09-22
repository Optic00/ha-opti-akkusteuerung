# Huawei Solar / LUNA2000 Steuerung

Beta-Kandidat 2026.9-beta8. Aktive Hardwaresteuerung ist weiterhin
experimentell und ohne eigene Hardwareabnahme.
Die vorhandene Huawei-Solar-Integration bleibt verantwortlich für Anmeldung,
Gerätekommunikation und Modbus. Opti Akku ruft deren HA-Entitäten und Aktionen
auf und benötigt keine zusätzlichen Legacy-Helfer oder Blueprints.

## Einrichtung

1. Huawei Solar mit verfügbaren Parameter-Aktoren einrichten. Geprüfte
   Schnittstellenbasis: Integration 2.1.5, SUN2000 mit LUNA2000.
   Dies ist keine Zusage für alle Huawei-Firmwareversionen oder EMMA/LG RESU.
2. In Opti Akku Huawei und den Batterie-Wechselrichter auswählen. Shadow ist
   vorausgewählt und bleibt dauerhaft lesend. Für aktive Steuerung einen
   separaten Standard-Eintrag anlegen; dessen Schreibfreigabe beginnt ebenfalls aus.
3. Messwerte einschließlich SoC, Kapazität, Batterieleistung, AC-Leistung und
   Netzleistung zuordnen. Wh wird in kWh umgerechnet. Vorzeichenrichtung des
   Anlagenzählers bestätigen. Für aktive Steuerung oder eine Strategie-Vorschau
   außerdem eine gültige Batterietemperatur wählen; sie lässt sich im
   Quellenabschnitt ergänzen. Nur reine Shadow-Messwerte ohne Strategie können
   ohne Temperatur eingerichtet werden. Für die Strategie einen vorhandenen
   Hausverbrauchssensor wählen, der alle Erzeuger und Verbraucher an der
   beabsichtigten Messgrenze berücksichtigt. Die SMA-Ein-Wechselrichter-Bilanz
   wird für Huawei nicht angeboten.
4. Acht vorhandene Aktoren zuordnen: Betriebsmodus, Überschuss-PV-Verwendung,
   Netzladen-Schalter, maximale Lade-/Entlade-/Netzladeleistung,
   Netz-Ladeabschalt-SoC und Status erzwungener Ladung/Entladung.
   Die drei Batterie-Entitäten müssen zum selben Batteries-Untergerät gehören.
   Alle Aktoren, Einheiten, Grenzen und benötigten Aktionen werden geprüft.
   Bei fehlendem Schreibzugriff bleibt die Aktivierung gesperrt; es gibt keinen
   stillen Betrieb mit nur einem Teil der angeforderten Modi.
5. Optional Netzladen im Eigenverbrauch für zusätzliche AC-Erzeuger erlauben.
   Diese anlagenabhängige Option ist standardmäßig aus; sie ist kein genereller
   Auftrag zum Netzladen. Hersteller- und Anlagenverhalten im Pilot prüfen.
6. Grenzen, Tarif, Prognose und optionale Funktionen konfigurieren. Andere
   schreibende Strategien erst vor dem Pilot deaktivieren. Alleinigen Writer
   bestätigen und danach den gesonderten Schreibschalter einschalten.

## Modusmatrix

| Strategiemodus | Umsetzung |
| --- | --- |
| Automatisch | Eigenverbrauch, konfigurierte maximale Leistungsgrenzen |
| Dynamisch | Eigenverbrauch mit dynamischer Ladegrenze |
| Pause | Laden, Entladen und Netzladeleistung 0 W; Netzladen aus; Zwangsbetrieb stoppen |
| Nur Laden | Eigenverbrauch, Entladegrenze 0 W |
| Nur Entladen | Eigenverbrauch, Ladegrenze 0 W und Netzladen aus |
| Netzladen / schnell Laden | Ganztägige TOU-Ladeperiode, Entladegrenze 0 W, Lade-/Netzleistungsgrenze und Abschalt-SoC |
| 0.2C Laden | Derselbe TOU-Pfad, Ladeleistung aus Kapazität × 0,2, begrenzt durch Benutzer-/Hardwaremaximum |
| Schnell Entladen | Zeitbegrenzte Huawei-Zwangsentladung mit 30 Minuten Dauer, im regulären Abgleich erneuert |

Netzladen nutzt den dynamischen Strategiewert; schnell Laden den festen
Ladesollwert. Die alten pauschalen 5000-W-Grenzen und die feste 0.2C-Leistung werden nicht
hart übernommen. Gerätegrenzen und Benutzereinstellungen begrenzen die Leistung.
Leistungswerte werden bei gröberen Hardware-Schritten abgerundet. Ein
Netz-Ladeabschaltwert unter der verfügbaren Hardwaregrenze, typischerweise 20 %,
wird abgelehnt, nicht still angehoben. Positive Mindestleistungsfenster sind
SMA-spezifisch; Huawei nutzt die maximalen Leistungsgrenzen.

## Bestätigung und Ausfälle

Moduswechsel begrenzen zunächst beide Richtungen, stoppen erzwungenen Betrieb
und verlassen einen alten TOU-Modus. Freigaben folgen erst nach bestätigten
Restriktionen. Rückmeldungen können verzögert eintreffen: Die Transaktion wartet
über Coordinator-Durchläufe, wiederholt eine unbestätigte Aktion höchstens einmal
nach 15 Sekunden und meldet erst nach Abschluss Erfolg. Jede Phase prüft erneut
Gerätebindung und Gültigkeit der Steuerabsicht. Geänderte bestätigte Restriktionen
während einer Transaktion brechen weitere Freigaben ab.

Der Status einer Zwangsentladung muss nach dem Aufruf neu gemeldet sein. Ein
alter `Discharging`-Text beweist weder eine laufende Entladung noch die Erneuerung.
Auch frisches Konfigurations-Readback ist keine unabhängige Bestätigung der
physikalischen Leistung. Die vorhandene Sperrverletzungsüberwachung bleibt aktiv.

TOU-Tabellen haben in der verwendeten Aktionsschnittstelle kein unabhängig
geprüftes Readback. Die Ladeperiode `00:00-23:59/1234567/+` wird bei Eintritt
hinter Null-Leistungsgrenzen geschrieben; das letzte Minutenintervall und das
konkrete Firmwareverhalten bleiben im Hardwarepilot zu prüfen. Ein erfolgreicher
Aktionsaufruf belegt keine physische Wirkung der Zeitplantabelle.

**Die 30-Minuten-Befristung gilt nur für den Zwangsbefehl.** TOU-Zeitpläne,
Netzladen und Leistungsgrenzen können einen HA-Ausfall überdauern. Geordnetes
Abschalten/Entladen versucht eine begrenzte Pause und meldet fehlende Bestätigung.
Es erfolgt keine behauptete vollständige Wiederherstellung einer unbekannten
Ausgangskonfiguration. Eine noch ausstehende Pause wird vor dem ersten Schreibzugriff gespeichert und
nur nach bestätigtem Cleanup gelöscht. Nach einem Offline-Abschalten versucht
Opti Akku sie bei Wiederverbindung nachzuholen, auch wenn Schreiben ausgeschaltet
bleibt. Dafür müssen Gerätebindung und Allein-Writer-Bestätigung unverändert sein.
Die Bindung der offenen Pause wird gesondert gespeichert; bei Umkonfiguration
bleibt die offene Pause sichtbar und wird niemals auf ein anderes Gerät umgeleitet.
Den alten Gerätepfad wiederherstellen und die Pause nachholen, bevor die Bindung
geändert wird. Ohne erreichbares HA/Huawei kann diese Nachholung nicht stattfinden.
Auch eine bestätigte Pause kann nach HA-Abschaltung als
0-W-Begrenzung bestehen bleiben. Automatik-Rückgabe und Wiederaktivierung der
bisherigen Steuerung müssen im beaufsichtigten Pilot ausdrücklich geprüft werden.

## Prüf- und Freigabeumfang

Synthetische Tests decken Modusziele, Rückleseverzögerung, Wiederholung,
fehlende Aktionen, falsche Gerätebindung, Shadow-Sperre, ungültige Grenzwerte,
Abbruch vor Freigaben, Drift und Shutdown-Pause ab. Die Testbefehle stehen in
[Entwicklung und Prüfung](docs/development.md).
Ein Hardwarepilot mit kleinen Leistungswerten, Richtungskontrolle, TOU-Ausgang,
Neustart/Ausfall und Rückweg ist vor regulärer Nutzung der neuen Umsetzung offen.


## Schnittstellenbelege

Die Nummernrollen werden zusätzlich zur Gerätezugehörigkeit an den konkreten
Huawei-Parametertyp gebunden, damit eine andere W-Zahlentität nicht versehentlich
als Batterielimit verwendet werden kann. Die Namen stimmen mit den
[Huawei-Solar-Entitätsdefinitionen](https://github.com/wlcrs/huawei_solar/blob/main/strings.json)
der untersuchten Huawei-Solar-Version überein. Dies ist eine explizite
Kompatibilitätsprüfung, keine Verwendung privater Transport-APIs.
