# SMA Sunny Island: Lesetest

Das Profil **SMA Sunny Island (lesender Test)** ist der erste Schritt zur
Sunny-Island-Unterstützung ([#194](https://github.com/Optic00/ha-opti-akkusteuerung/issues/194)).
Es liest den Sunny Island nur aus. Ein Eintrag mit diesem Profil läuft immer im
Shadow-Modus, kennt keinen Steuermodus und lässt sich nicht in eine aktive
Steuerung übernehmen. Jeder Schreibversuch wird abgewiesen.

## Warum ein Lesetest

Das SMA-Modbus-Profil verwendet für denselben Parameter bei allen SMA-Geräten
dieselbe Registeradresse, aber nicht jedes Gerät bietet jedes Register an. Das
Profil liest deshalb die Adressen, die beim STP SE belegt sind, und ergänzt
Register aus drei Quellen für den Sunny Island: dem evcc-Template
`sma-si-modbus`, der Registerliste `SIx.xx-12ff` aus dem ioBroker-Projekt
Maverick78de und den Angaben aus #194 zu einem SI 6.0H-13. Ob Werte, Vorzeichen
und Skalierung am Sunny Island stimmen, ist mit Opti Akku noch nicht geprüft.
Das soll dieser Test klären.

Die Steuerregister 40149, 40151, 40236, 40795, 40799 und 40801 werden nur
zurückgelesen. So zeigt die Diagnose, ob das Gerät sie anbietet. Geschrieben
wird nichts.

## Einrichten

1. Am Sunny Island Modbus TCP aktivieren (Port 502). Opti Akku schlägt Unit ID 3 vor, die SMA-Voreinstellung.
2. In Home Assistant Opti Akku hinzufügen, „SMA Modbus“ wählen und als
   Geräteprofil **SMA Sunny Island (lesender Test)** einstellen.
3. Bei den Quellen die AC-Leistung der PV-Wechselrichter unter „Weitere
   Wechselrichter: AC-Ausgangsleistung“ eintragen. PV-Erzeugung gesamt kommt aus
   einer vorhandenen PV-Entität. Der Sunny Island hat keinen PV-Eingang.

Netzbezug und Einspeisung liest das Profil aus dem Sunny Island. Dafür muss der
Zähler am Netzanschlusspunkt dem Sunny Island seine Werte liefern.

Meldet Opti Akku bei der Einrichtung einen Verbindungsfehler, obwohl der Sunny
Island über Modbus erreichbar ist, bitte in #194 melden. Opti Akku wertet ein
Statusregister ohne jede Antwort als Verbindungsproblem. Beantwortet der Sunny
Island Register 33003 gar nicht statt mit einem Fehlercode, scheitert daran die
Einrichtung, und das Profil braucht eine Anpassung.

## Was zu vergleichen ist

Bitte über einige Stunden mit Laden und Entladen gegen Sunny Portal oder das
Webinterface des Sunny Island vergleichen:

| Opti-Wert | Register | Vergleich mit |
|---|---|---|
| Ladestand (SoC) | 30845 | Ladezustand der Batterie |
| Akkuleistung | 31393 / 31395, sonst 30775 mit umgekehrtem Vorzeichen | Lade- und Entladeleistung, Vorzeichen: Laden positiv |
| Akkutemperatur | 30849 | Batterietemperatur |
| Akkukapazität | 40187 | Nennkapazität der Batterie |
| Wechselrichter-AC-Leistung | 30775 | AC-Leistung des Sunny Island, besonders das Vorzeichen beim Laden |
| Netzbezug / Einspeisung | 30865 / 30867 | Zähler am Netzanschlusspunkt |
| Hausverbrauch | Bilanz | Verbrauch im Sunny Portal |
| Gerätestatus | 33003, sonst 40029, sonst 33001 | Betriebszustand |

Liefert der Sunny Island die Register 31393 und 31395 nicht, zeigt die
Akkuleistung die AC-Leistung aus 30775 mit umgekehrtem Vorzeichen. Das ist die
Leistung am Wechselrichter, nicht an der Batterie. Welche Register geantwortet
haben, steht in der Diagnose.

Opti Akku liest den Gerätestatus bei jeder Abfrage in dieser Reihenfolge und
nimmt das erste Register mit einem echten Wert. Als bereit gelten 235 und 2119
wie beim STP SE sowie 569 („eingeschaltet“ aus Register 40029). Andere Werte
zeigt Opti Akku als nicht bereit an, bis ihre Bedeutung am Gerät bestätigt ist.

## Was ich zurück brauche

Die Diagnose-Datei des Eintrags (Geräte & Dienste, Opti Akku, drei Punkte,
„Diagnose herunterladen“). Sie enthält unter `device` Profil, Gerätetyp,
Statusregister und unter `register_probe` die Rohwerte aller Testregister.
Hosts, Seriennummern und Entitätsnamen sind nicht enthalten. Dazu eine kurze
Notiz, welche Werte zum Sunny Portal passen und welche nicht.

Eigene Skripte, Automationen oder evcc, die den Sunny Island steuern, dürfen
während des Lesetests weiterlaufen. Vor einem späteren Schreibtest müssen sie
abgeschaltet sein.
