# SMA Sunny Boy Storage: Lesetest

Das Profil **SMA Sunny Boy Storage (lesender Test)** ist der erste Schritt zur
SBS-Unterstützung ([#170](https://github.com/Optic00/ha-opti-akkusteuerung/issues/170)).
Es liest den SBS nur aus. Ein Eintrag mit diesem Profil läuft immer im
Shadow-Modus, kennt keinen Steuermodus und lässt sich nicht in eine aktive
Steuerung übernehmen. Jeder Schreibversuch wird abgewiesen.

## Warum ein Lesetest

Das SMA-Modbus-Profil verwendet für denselben Parameter bei allen SMA-Geräten
dieselbe Registeradresse, aber nicht jedes Gerät bietet jedes Register an. Das
Profil liest deshalb die Adressen, die beim STP SE belegt sind, und ergänzt
Register aus der Community (SBS 2.5, ioBroker-Projekt Maverick78de für
SBS 3.7 bis 10). Ob Werte, Vorzeichen und Skalierung am SBS stimmen, ist noch
nicht geprüft. Das soll dieser Test klären.

## Einrichten

1. Am SBS Modbus TCP aktivieren (Port 502). Opti Akku schlägt Unit ID 3 vor, die SMA-Voreinstellung.
2. In Home Assistant Opti Akku hinzufügen, „SMA Modbus“ wählen und als
   Geräteprofil **SMA Sunny Boy Storage (lesender Test)** einstellen.
3. Bei den Quellen die AC-Leistung des Sunny Tripower unter „Weitere
   Wechselrichter: AC-Ausgangsleistung“ eintragen. PV-Erzeugung gesamt kommt aus einer
   vorhandenen PV-Entität, zum Beispiel aus der SMA-Integration des Tripower.
   Der SBS hat keinen PV-Eingang.

## Was zu vergleichen ist

Bitte über einige Stunden mit Laden und Entladen gegen die SMA-App oder das
Webinterface des SBS vergleichen:

| Opti-Wert | Register | Vergleich mit |
|---|---|---|
| Ladestand (SoC) | 30845 | Ladezustand in der SMA-App |
| Akkuleistung | 31393 / 31395 | Lade- und Entladeleistung, Vorzeichen: Laden positiv |
| Akkutemperatur | 30849 | Batterietemperatur |
| Akkukapazität | 40187 | Nennkapazität der Batterie |
| Wechselrichter-AC-Leistung | 30775 | AC-Leistung des SBS, besonders das Vorzeichen beim Laden |
| Netzbezug / Einspeisung | 30865 / 30867 | Sunny Home Manager |
| Hausverbrauch | Bilanz | Verbrauch im Sunny Home Manager |

## Was ich zurück brauche

Die Diagnose-Datei des Eintrags (Geräte & Dienste, Opti Akku, drei Punkte,
„Diagnose herunterladen“). Sie enthält unter `device` Profil, Gerätetyp,
Statusregister und unter `register_probe` die Rohwerte aller Testregister.
Hosts, Seriennummern und Entitätsnamen sind nicht enthalten. Dazu eine kurze
Notiz, welche Werte zur SMA-App passen und welche nicht.

Eigene Skripte oder Automationen, die den SBS steuern, dürfen während des
Lesetests weiterlaufen. Vor einem späteren Schreibtest müssen sie abgeschaltet
sein.
