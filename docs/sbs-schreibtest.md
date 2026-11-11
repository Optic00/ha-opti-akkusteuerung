# SMA Sunny Boy Storage: Schreibtest-Plan

Dieser Plan bereitet die Steuerung des Sunny Boy Storage 3.7/5.0/6.0 vor
([#170](https://github.com/Optic00/ha-opti-akkusteuerung/issues/170)). Er folgt
der Hardware-Abnahme des STP SE in
[#151](https://github.com/Optic00/ha-opti-akkusteuerung/issues/151). Nichts davon
ist gebaut: Das Profil **SMA Sunny Boy Storage (lesender Test)** liest weiterhin
nur und weist jeden Schreibversuch ab.

Jede Aussage ist markiert:

- **SMA**: steht in der SMA-Modbus-Liste
  [MODBUS-HTML_SBS3.7-6.0-10_GG10-V13](https://files.sma.de/downloads/MODBUS-HTML_SBS3.7-6.0-10_GG10-V13.zip)
  (gilt ab Firmware 3.12.33.R mit Länderdatensatz ab 2018, SMA Grid Guard 10.0)
  oder der Parameterliste
  [PARAMETER-HTML_SBS3.7-6.0-10_GG10_V14](https://files.sma.de/downloads/PARAMETER-HTML_SBS3.7-6.0-10_GG10_V14.zip)
  (ab Firmware 4.04.03.R). Beide verlinkt von der
  [SMA-Produktseite](https://www.sma.de/produkte/batterie-wechselrichter/sunny-boy-storage-37-50-60).
- **Gerät**: am SBS 5.0 aus #170 gemessen (Diagnose vom 10.11.2026).
- **STP SE**: Verhalten, das Opti Akku am STP SE nutzt; am SBS ungeprüft.
- **Annahme**: nicht belegt, im Test zu klären.

## Befund aus der Diagnose vom 10.11.2026

| Register | Rohwert | Bedeutung laut SMA |
|---|---|---|
| 30051 | 8007 | Geräteklasse Batterie-Wechselrichter |
| 30053 | 9358 | SBS5.0-10 |
| 33003 `Operation.RunStt` | 16777213 | Information liegt nicht vor |
| 33001 `Operation.StandbyStt` | 2531 | Energiesparmodus |
| 30845 `Bat.ChaStt` | 0 | Ladestand 0 % |
| 31393 / 31395 | 0 / 0 | keine Lade- oder Entladeleistung |
| 30851 `Bat.Vol` | 27 | 0,27 V (FIX2) |
| 30849 `Bat.TmpVal` | 240 | 24,0 °C |
| 31061 `Bat.ChaCtlComAval` | 1130 | Steuerung der Batterieladung über Kommunikation: Nein |
| 40035 `Bat.Typ` | 1785 | Lithium-Ionen |
| 40187 `Bat.CapacRtgWh` | 7700 | 7,7 kWh Nennkapazität |
| 40189 / 40191 | 5000 / 5300 | max. Lade-/Entladeleistung des Batteriestellers in W |
| 40073 `SelfCsmp.BatChaSttMin` | nicht verfügbar | laut SMA RW, 0 bis 100 % |

Der Schnappschuss stammt aus einem Moment mit leerem Akku im Energiesparmodus.
`not_ready` war dort richtig. Ob der SBS im Betrieb 235 in 33003 meldet, zeigt
die Verlaufsgeschichte der Entität „Wechselrichter-Bereitschaft“ und ihres
Attributs `inverter_status` (**Annahme**, bis Kalrael das bestätigt).

Laut **SMA** überschneiden sich die Codelisten nicht:

- 33003 `Operation.RunStt`: 235 Netzparallelbetrieb, 1463 Backup,
  1469 Herunterfahren, 1855 Stand-Alone, 2119 Abregelung, 16777213 nicht verfügbar
- 33001 `Operation.StandbyStt`: 1394 Warte auf gültiges AC-Netz,
  2531 Energiesparmodus, 16777213 nicht verfügbar
- 40029 `Operation.OpStt`: 303 Aus, 569 Eingeschaltet, 1295 Standby,
  1795 Verriegelt, 16777213. In der Liste für Länderdatensätze vor 2018
  ([MODBUS-HTML_SBS3.7-6.0-10-V13](https://files.sma.de/downloads/MODBUS-HTML_SBS3.7-6.0-10-V13.zip))
  fehlen 33001 und 33003; dort trägt 40029 die Codes 235, 2119, 1467 und weitere.

Bereit für Schreibzugriffe sind wie beim STP SE nur 235 und 2119. 2531 bleibt
nicht bereit. Für den aktiven Betrieb muss 2531 bei leerem Akku nachts wie die
Nacht-Bereitschaft aus #103 behandelt werden, sonst meldet Opti Akku jede Nacht
einen Verbindungsfehler.

## Steuerregister laut SMA

WO heißt nur beschreibbar: Ein Lesezugriff liefert eine Modbus-Exception, eine
Rücklesung gibt es nicht. Die Wirkung lässt sich nur an Messwerten prüfen.

| Register | Objekt | Zugriff | Werte | Opti am STP SE |
|---|---|---|---|---|
| 40151 | `WCtlComAct` Wirk-/Blindleistungsregelung über Kommunikation | WO | 802 aktiv, 803 inaktiv | ja |
| 40149 | `WSpt` Wirkleistungsvorgabe | WO | S32 in W, kein Bereich angegeben | ja, negativ = Laden |
| 40236 | `CmpBMS.OpMod` Betriebsart des BMS | WO | 303 Aus, 308 Ein, 1438 Automatik, 2289 Laden, 2290 Entladen, 2424 Voreinstellung | Dynamisch |
| 41259 | `CmpBMS.OpMod` (zweite Adresse) | WO | wie 40236 | Pause, nur Laden, nur Entladen, Automatisch |
| 40793 / 40795 | `CmpBMS.BatChaMinW` / `BatChaMaxW` | WO | U32 in W | ja |
| 40797 / 40799 | `CmpBMS.BatDschMinW` / `BatDschMaxW` | WO | U32 in W | ja |
| 40801 | `CmpBMS.GridWSpt` Sollwert Netzaustausch | WO | S32 in W | ja, immer 0 |
| 30835 / 40210 | `WModCfg.WMod` Betriebsart Wirkleistungsvorgabe | RO / RW | 303 Aus, 1077 W, 1078 %, 1079 externe Vorgabe | nur gelesen |
| 41193 | Rückfallverhalten externe Wirkleistungsvorgabe | RW | 2506 Werte beibehalten, 2507 Rückfallwerte übernehmen | nur gelesen |
| 41195 | Timeout externe Wirkleistungsvorgabe | RW | 1 bis 1800 s, Voreinstellung 600 s (Liste vor 2018: 5 bis 86400 s) | nur gelesen |
| 41215 | Rückfallwert der Wirkleistungsvorgabe | RW | −500000 bis 500000 W, Voreinstellung 0 W | nur gelesen |
| 44035 / 44037 | Rückfallwert min./max. Wirkleistung | RW | −6050 bis 6050 W, Voreinstellung −3680 / 3680 W | nur gelesen |
| 40073 | Untere Entladegrenze Eigenverbrauch | RW | 0 bis 100 % | nicht genutzt |

Alle Steuerregister, die Opti Akku am STP SE schreibt, gibt es laut **SMA** auch
am SBS unter derselben Adresse und mit denselben Codes. Das
[evcc-Template `sma-sbs-modbus`](https://github.com/evcc-io/evcc/blob/master/templates/definition/meter/sma-sbs-modbus.yaml)
für SBS 3.7/5.0/6.0 steuert nur über 40236 (2424, 2289) und 40793 bis 40801,
nicht über 40149/40151. Ob der SBS die Folge 802 → 40149 → 803 so umsetzt wie der
STP SE, ist **Annahme**.

Ab der nächsten Testversion liest der Registerschnappschuss zusätzlich 30059
(Firmware), 30201, 40029, 30955, 31057, 30219, 30233, 30835, 41187, 41193, 41195,
41215, 44035 und 44037, alle laut SMA lesbar. Damit ist V0 ohne eigenes Werkzeug
möglich.

## Vorbedingungen

1. **Nur ein Schreiber.** Eigene Skripte und Automationen sind während des Tests
   aus. Offen: Ob der Sunny Home Manager 2.0 oder der Sunny Tripower X als
   Systemmanager den SBS steuert, und ob einer davon dieselben Parameter setzt
   (**Annahme**, Frage an Kalrael).
2. **Steuerung über Kommunikation verfügbar.** 31061 stand im Energiesparmodus
   auf 1130 (Nein). Vor V1 muss 31061 im Betrieb 1129 (Ja) zeigen. Was 1130
   auslöst, sagt die SMA-Liste nicht (**Annahme**: Standby oder fehlende Freigabe).
3. **Rückfallverhalten bekannt.** 41193, 41195, 41215, 44035, 44037 lesen und im
   Protokoll festhalten (44035/44037 sind FIX2, Rohwert ÷ 100 = W). Nicht ändern
   ohne Absprache.
4. **Betriebsstatus 235** in 33003 zu Testbeginn.
5. **Unabhängiger Rückweg.** SMA-App oder Webinterface des SBS offen. Vor dem
   ersten Schreibtest legt Kalrael fest und probiert einmal aus, wie er den SBS
   ohne Opti Akku in den Normalbetrieb bringt, etwa mit seiner bisherigen Lösung.
   Welcher Weg am SBS wirkt, ist **Annahme**.
6. **Ladestand zwischen 30 und 80 %**, damit Laden und Entladen nicht an den
   BMS-Grenzen enden.

## Kodierung für ein eigenes Modbus-Werkzeug

Alle Steuerwerte sind laut **SMA** 32 Bit breit und belegen zwei Register.
Opti Akku und evcc schreiben sie mit Funktion 16 (Write Multiple Registers) an
Unit ID 3, höheres Wort zuerst, unter den Adressen der SMA-Liste. Ein einzelnes
16-Bit-Register zu schreiben reicht nicht.

| Wert | Wörter (hex) |
|---|---|
| 0 | `0000 0000` |
| 500 | `0000 01F4` |
| −500 | `FFFF FE0C` |
| 303 / 1438 / 2289 / 2290 / 2424 | `0000 012F` / `0000 059E` / `0000 08F1` / `0000 08F2` / `0000 0978` |
| 802 / 803 | `0000 0322` / `0000 0323` |

## Ablauf

Jeder Versuch höchstens 15 Minuten (außer V7), Vorgaben höchstens 500 W. Nach
jedem Versuch der Rückweg aus Vorbedingung 5 oder V2, dann 5 Minuten warten.

Beobachten, in dieser Reihenfolge priorisiert:

- 31393 / 31395 Lade- und Entladeleistung, 30775 AC-Leistung des SBS
- Netzbezug und Einspeisung am Sunny Home Manager
- 30955 `Bat.OpStt` (2291 Standby, 2292 Laden, 2293 Entladen), 30845 Ladestand
- 33003 / 33001 Status, 30219 Grund der Leistungsbegrenzung
- Ereignismeldungen des SBS, besonders 8708 „Timeout in communication for
  active power limitation“ laut
  [SMA-Betriebsanleitung, Ereignismeldungen](https://manuals.sma.de/SBSxx-10/en-US/391460235.html)

Abbruch sofort und Rückweg, wenn

- die Leistung in die falsche Richtung geht oder mehr als 200 W über der Vorgabe liegt,
- in einem Entladeversuch am Netzanschlusspunkt mehr als 100 W eingespeist werden,
- in einem Ladeversuch der Netzbezug um mehr als 700 W steigt (V5 darf bis 500 W
  aus dem Netz laden, das ist der erwartete Effekt),
- ein Schreibzugriff mit Exception oder Timeout endet. Ein Timeout kann trotzdem
  angekommen sein; der Zustand gilt dann als unbekannt, bis der Rückweg gewirkt hat,
- der SBS eine Ereignismeldung zeigt, 33003 nicht mehr 235 ist oder der Ladestand
  die Grenzen aus Vorbedingung 6 verlässt,
- eine der Beobachtungsgrößen ausfällt oder länger als 30 s nicht aktualisiert wird.

Rückweg nach einem Abbruch: Antwortet Modbus zuverlässig und steht 33003 auf 235,
erst N, dann V2. Sonst zuerst der unabhängige Rückweg aus Vorbedingung 5, ohne
weitere Modbus-Schreibzugriffe. Danach keine weiteren Schritte am selben Tag ohne
Rücksprache in #170.

Zwei Stufen:

- **Stufe A, Registertest von Hand.** Kalrael schreibt die Werte mit seinem
  vorhandenen Modbus-Werkzeug. Das klärt das Geräteverhalten, bevor Opti Akku
  schreibt. Er entscheidet selbst, ob er das machen will.
- **Stufe B, Testversion mit aktivem SBS-Pfad.** Opti Akku schreibt Modus für
  Modus, in derselben Reihenfolge. Erst nach Stufe A.

**N (Sollwert neutralisieren):** 40151=802, sofort 40149=0, dann 40151=803.
N steht vor dem ersten Schreibtest, weil niemand weiß, was in 40149 noch steht,
und im Rückweg nach einem Abbruch, solange Modbus zuverlässig antwortet. Grund: Am STP SE bleibt ein Sollwert in 40149 unter 803
stehen und wirkt beim nächsten 802 kurz weiter (#151, V5a).

| Schritt | Ziel | Schreibfolge (Stufe A) |
|---|---|---|
| V0 nur lesen | Vorbedingungen 2 bis 4 erfassen | keine; Diagnose im Betrieb tagsüber herunterladen |
| V1 Pause | Laden und Entladen gesperrt, einmal beim Laden, einmal beim Entladen | N, 40793=0, 40795=0, 40797=0, 40799=0, 40801=0, 41259=303 |
| V2 Automatisch | zurück in den Normalbetrieb | 40151=803, 40793=0, 40795=Wert aus 40189, 40797=0, 40799=Wert aus 40191, 40801=0, 41259=1438, dann 40151=802, sofort 40149=0, nach 2 s 40151=803 |
| V3 nur Laden | Entladen gesperrt, Laden bis 500 W | wie V1, aber 40795=500, 41259=2289 |
| V4 nur Entladen | Laden gesperrt, Entladen bis 500 W | wie V1, aber 40799=500, 41259=2290 |
| V5 schnell Laden | Ladung mit fester Leistung | V2 bis 41259=1438, dann 40151=802, sofort 40149=0, nach 2 s 40149=−500; nach 120 s erneuern: 40151=802, nach 2 s 40149=−500; Ende: 40149=0, 40151=803 |
| V6 schnell Entladen | Entladung mit fester Leistung | V2 bis 41259=1438, dann 40151=802, sofort 40149=0, nach 2 s 40149=500; nach 120 s erneuern: 40151=802, nach 2 s 40149=500; Ende: 40149=0, 40151=803 |
| V7 Ausfall | Verhalten nach Kommunikationsverlust | siehe unten |

Die Folgen sind nach dem Muster des STP-SE-Pfads in `sma.py` gebildet
(**STP SE**, vereinfacht; dort 0,5 s zwischen den Registern, und die
CmpBMS-Folge vor 802 nur beim Wechsel aus einem Sperrmodus sowie bei jedem
Automatisch-Befehl, auch bei Wiederholungen). Dass der SBS sie
genauso umsetzt, ist **Annahme**. Insbesondere:

- Das Vorzeichen von 40149 (negativ = Laden) ist am SBS **Annahme**; V5 kommt vor
  V6 und bricht bei falscher Richtung ab.
- Der Treiber schreibt 803 vor jede CmpBMS-Folge, weil CmpBMS-Werte unter 802
  verworfen werden. Das steht als Grundlage im STP-SE-Code, ein unabhängiger
  Hardwarebeleg liegt hier nicht vor.
- Für den Normalbetrieb schreibt Opti Akku 41259=1438, das evcc-Template dagegen
  40236=2424 (Voreinstellung). Was davon den SBS wirklich in seinen Normalbetrieb
  bringt, klärt V2. Bis dahin ist V2 kein belegter Rückweg.

Je Schritt festhalten: Uhrzeit jedes Schreibvorgangs, Antwort des Geräts
(Exception ja/nein), Zeit bis zur Wirkung, eingeschwungene Leistung,
Abweichung zur Vorgabe, Netzbezug und Einspeisung.

### V7 Kommunikationsausfall

Laut **SMA** gilt für die externe Wirkleistungsvorgabe: Nach 41195 Sekunden ohne
Vorgabe greift 41193, also Werte beibehalten (2506) oder Rückfallwerte übernehmen
(2507, mit 41215 und 44035/44037). Ob das auch für die CmpBMS-Fenster und die
BMS-Betriebsart gilt, steht nicht in der Liste (**Annahme**, zentraler Punkt von
V7).

V7 nur, wenn die in V0 gelesenen Werte das zulassen:

- 41193=2506, oder 41193=2507 mit Rückfallwerten, die zusammen höchstens 500 W
  Lade- oder Entladeleistung ergeben können. Sonst erst in #170 besprechen.
- 41195 höchstens 900 s. Die Beobachtung dauert 41195 plus 5 Minuten; erst danach
  zählt „keine Reaktion“ als Ergebnis. Bei längerem Timeout kein V7 ohne
  Absprache über eine Änderung von 41195.

Ablauf: Zuerst nach einem vollständigen V3-Befehl den Modbus-Client beenden und
beobachten, ob der SBS die Sperre hält, in den Normalbetrieb fällt oder die
Rückfallwerte übernimmt. Danach dasselbe mit V5 (aktive Vorgabe über 40149).
Die Abbruchkriterien gelten weiter.

Opti Akku erneuert Befehle alle 120 Sekunden (**STP SE**). Ein Timeout unter
120 s plus Reserve würde zwischen zwei Erneuerungen auslösen.

Bekannte Lücke wie beim STP SE: Die Sicherheits-Pause nach einem abgebrochenen
Schreibvorgang prüft den Betriebsstatus. Steht der SBS dann im Energiesparmodus
(2531), schlägt sie fehl.

## Was im Code für einen aktiven SBS-Pfad fehlt

Skizze, nicht gebaut. Der Schreibpfad von `SmaDevice` in `sma.py` nutzt nur
Register, die der SBS laut **SMA** ebenfalls hat. Naheliegend ist daher, ihn
wiederzuverwenden statt einen zweiten zu schreiben.

1. **Identität und Leistungsgrenzen.** `SmaDevice` kennt nur STP-SE-Modelle
   (`MODEL_NAMES`, `MODEL_POWER_W`, Geräteklasse 8009). Für den SBS:
   Geräteklasse 8007, Typen 9356/9358/9359, Lade- und Entladegrenze getrennt aus
   40189/40191 statt einer gemeinsamen Nennleistung.
2. **Status.** 33003 zuerst, 33001 nur ohne Wert aus 33003, wie jetzt im
   Leseprofil. Für Länderdatensätze vor 2018 eine Entscheidung zu 40029.
   `allowed_statuses` bleibt 235/2119.
3. **Schreibvoraussetzungen.** 31061 vor jeder Transaktion lesen und bei 1130
   nicht schreiben (fail closed). 41195 und 41193 beim Start lesen; ist der
   Timeout kürzer als das Erneuerungsintervall plus Reserve, nicht freigeben
   oder das Intervall anpassen.
4. **Energiesparmodus.** Mit 2531 bleibt `write_ready` falsch (`recovery.py`),
   der Treiber verweigert Schreibzugriffe. Nachts mit leerem Akku kann Opti Akku
   damit kein Netzladen starten. Ob ein Schreibzugriff den SBS aus 2531 weckt,
   ist offen und braucht einen eigenen Versuch; bis dahin ist das eine
   dokumentierte Einschränkung. 2531 pauschal als bereit zu werten deckt der
   Beleg nicht.
5. **Nacht-Bereitschaft.** `SMA_STANDBY_STATUS_CODES` in `alerts.py` um 2531
   erweitern, mit denselben Bedingungen wie bei #103. Das unterdrückt nur den
   Alarm, nicht die Einschränkung aus Punkt 4.
6. **Messwerte.** `SbsDevice.async_read` beibehalten: kein PV-Eingang,
   PV-Erzeugung aus einer HA-Quelle, Akkuleistung aus 31393/31395.
7. **Profil und Einrichtung.** Ein aktives SBS-Profil neben dem lesenden
   `sma_sbs`. Die heutige Sperre in `__init__.py` und `config_flow.py`
   (immer Shadow, keine Übernahme in die aktive Steuerung) gilt weiter für das
   lesende Profil; das aktive Profil durchläuft wie der STP SE erst den
   Shadow-Abschluss.
8. **Modusauswahl.** Erst die Modi anbieten, die in Stufe A bestanden haben.
   `supported_modes` je Profil gibt es schon.
9. **Tests.** Fake-Gerät mit SBS-Identität für jeden freigegebenen Modus,
   Rückfall bei 2531 vor und während einer Transaktion, 31061=1130, kurzer
   Timeout in 41195, Grenzen aus 40189/40191.

Offen bleibt die Abstimmung mit dem Sunny-Island-Profil aus PR #203, das dieselbe
Geräteklasse erweitert.
