# Modbus Register Referenz – SMA STP SE Hybrid

> ## ⚠️ WICHTIGER HAFTUNGSAUSSCHLUSS
>
> **Diese Dokumentation ist eine inoffizielle, community-erstellte Sammlung und wird in keiner Weise von SMA Solar Technology AG begleitet, geprüft oder supportet.**
>
> Die hier aufgeführten Registeradressen wurden durch eigene Tests, Community-Beiträge und eine inoffizielle Antwort des SMA-Supports ermittelt. Es wird **keine Gewähr** für Korrektheit, Vollständigkeit oder Aktualität übernommen. Angaben können sich mit Firmware-Updates ändern.
>
> **Das direkte Beschreiben von Modbus-Registern kann den Wechselrichter, die Batterie oder die gesamte Anlage beschädigen, Garantieansprüche erlöschen lassen oder zu gefährlichen Betriebszuständen führen.**
>
> **Nutzung ausschließlich auf eigene Gefahr. Der Autor übernimmt keinerlei Haftung für Schäden jeglicher Art.**

---

## Getestete Hardware

| Gerät | Firmware | Status |
|---|---|---|
| SMA STP SE 10.0 (Hybrid) | ab ~3.06.xx | ✅ Getestet |
| BYD HVS / HVM Akku | – | ✅ Getestet |
| SMA SBS 2.5 | – | ✅ Schreib-Register bestätigt (Community) – Lese-Register teilweise bekannt |
| SMA SBS 3.7 / 5.0 / 6.0 | 4.7.x | 🔍 Lese-Register gesucht – [Issue #9](https://github.com/Optic00/ha-opti-akkusteuerung/issues/9) |

---

## Verbindungsparameter

| Parameter | Wert |
|---|---|
| Protokoll | Modbus TCP |
| Port | 502 |
| Slave ID | 3 |
| Byte Order | Big Endian |

---

## Lese-Register (Sensoren)

### SMA STP SE Hybrid

| Adresse | SMA Kanal | Datentyp | Einheit | Faktor | Werte / Hinweis |
|---|---|---|---|---|---|
| 30217 | `Operation.GriSwStt` | U32 | – | 1 | `51` = Geschlossen · `311` = Offen · `16777213` = n/a |
| 30775 | `GridMs.TotW` | S32 | W | 1 | Gesamte AC-Wirkleistung |
| 30843 | `Bat.Vol` | U32 | V | 0.01 | Batteriespannung |
| 30845 | `Bat.ChaStt` | U32 | % | 1 | Batterie-SoC |
| 30847 | `Bat.Diag.ActlBatCha` | U32 | – | 1 | Batterie Ladezustand (Status) |
| 30849 | `Bat.Diag.TmpVal` | S32 | °C | 0.1 | Batterietemperatur |
| 30851 | `Bat.Diag.VolMeas` | U32 | V | 0.01 | Gemessene Batteriespannung |
| 30865 | `Metering.GridMs.TotWIn` | U32 | W | 1 | Netzbezug (Import aus Netz) |
| 30867 | `Metering.GridMs.TotWOut` | U32 | W | 1 | Netzabgabe (Einspeisung ins Netz) |
| 30881 | `Operation.PvGriConn` | U32 | – | 1 | `1779` = Getrennt · `1780` = Öffentl. Netz · `1781` = Inselnetz |
| 30953 | `Coolsys.Cab.TmpVal` | **S32** | °C | 0.1 | WR-Innentemperatur – ohne diesen Sensor startet die Modbus-Integration nicht zuverlässig |
| 31061 | `Bat.ChaCtlComAval` | U32 | – | 1 | `1129` = Ja (Steuerung verfügbar) · `1130` = Nein |
| 31393 | `BatChrg.CurBatCha` | U32 | W | 1 | Aktuelle Ladeistung (Momentanwert) |
| 31395 | `BatDsch.CurBatDsch` | U32 | W | 1 | Aktuelle Entladeleistung (Momentanwert) |
| 33003 | `Operation.RunStt` | U32 | – | 1 | `235` = Netzparallelbetrieb · `1463` = Backup · `1469` = Herunterfahren · `2119` = Abregelung |
| 40187 | `Bat.CapacRtgWh` | U32 | Wh | 1 | Batterie Nennkapazität (z.B. 12800 bei BYD HVS 12.8) |
| 40723 | `BatUsDm.BckDmMin` | U32 | % | 1 | Minimale Breite des Ersatzstrombereichs (RW – persistent, nicht zyklisch schreiben) |
| 41255 | `Inverter.WModCfg.WCtlComCfg.WNomPrc` | S16 | % | 1 | Normierte Wirkleistungsbegrenzung durch Anlagensteuerung (RW) |

---

## Schreib-Register (Steuerung)

> ⚠️ Falsche Werte können den WR in unerwünschte Betriebszustände bringen. Anlage beim ersten Schreibversuch beobachten.
>
> ⚠️ Laut SMA sollten **persistierte Parameter** (RW-Register) nicht dynamisch/zyklisch für die Steuerung verwendet werden, da dies den Flash-Speicher belastet. Die unten aufgeführten Register sind als **WO (Write-Only)** oder temporäre Steuerregister eingestuft und können bedenkenlos zyklisch beschrieben werden.

---

### Aktivierungssequenz

Vor Nutzung von Register 40149 muss diese Sequenz ausgeführt werden:

1. `40151` → `[0, 802]` schreiben (Schreibmodus aktivieren)
2. **1–2 Sekunden warten**
3. Steuerbefehl auf `40149` schreiben
4. `40151` → `[0, 803]` schreiben (Normalbetrieb)

| Adresse | SMA Bezeichnung | Wert | Bedeutung |
|---|---|---|---|
| 40151 | – | `[0, 802]` | Externe Steuerung aktivieren |
| 40151 | – | `[0, 803]` | Externe Steuerung deaktivieren / Normalbetrieb |

---

### Sollwert Batterieleistung direkt (40149)

Quelle: Offizielle SMA Support-Antwort (via [Photovoltaikforum, ajay123](https://www.photovoltaikforum.com/thread/215473-begrenzen-der-lade-entladeleistung-byd-mit-stp-se/?postID=4033278#post4033278))

> Positive Werte = Entladen, negative Werte = Laden (laut SMA)  
> In der Praxis hat sich die folgende Vorzeichen-Kodierung über zwei 16-Bit-Wörter bewährt:

| Adresse | SMA Bezeichnung | Richtung | Wert | Formel |
|---|---|---|---|---|
| 40149 | – | **Laden** | `[65535, X]` | `X = 65536 − Ladeleistung_W` |
| 40149 | – | **Entladen** | `[0, X]` | `X = Entladeleistung_W` |

Beispiel: 3000 W Laden → `[65535, 62536]`

> ℹ️ **Hinweis zur Laden-Formel:** Die beiden 16-Bit-Wörter werden vom WR als ein vorzeichenbehafteter S32 gelesen (Laden = negativ). Das korrekte Zweierkomplement für −P W ist `[65535, 65536 − P]`. Eine ältere, in der Praxis ebenfalls genutzte Variante `65535 − P` ergibt **−(P+1) W** (1 W zu viel) – für die Steuerung praktisch irrelevant, aber `65536 − P` ist exakt.

---

### BMS-Leistungsgrenzen (40793–40801)

Quelle: Offizielle SMA Support-Antwort (via [Photovoltaikforum, ajay123](https://www.photovoltaikforum.com/thread/215473-begrenzen-der-lade-entladeleistung-byd-mit-stp-se/?postID=4033278#post4033278))

> ⚠️ **Hinweis:** Die Register 40793, 40797, 40801, 41259 und 40236 tauchen in der offiziellen SMA Modbus-Parameterliste **nicht auf** – sie wurden durch direkten SMA-Support-Kontakt bekannt (ajay123) und sind in der Praxis erprobt. Es handelt sich vermutlich um interne Register die auch der Home Manager verwendet.

Diese Register steuern den dynamischen Betrieb. Der WR regelt dabei **selbstständig den Netzanschlusspunkt** auf den Sollwert `CmpBMS.GridWSpt`. Alle Werte in **Watt**, müssen **zyklisch max. alle 300 s** gesendet werden und **innerhalb von 10 s** gesetzt sein.

| Adresse | SMA Bezeichnung | In offizieller Doku | Bedeutung | Typischer Wert |
|---|---|---|---|---|
| 40793 | `CmpBMS.BatChaMinW` | ❌ | Minimale Ladestärke | `0` |
| 40795 | `CmpBMS.BatChaMaxW` | ✅ | Maximale Ladestärke | z.B. `2560` (= 0.2C bei 12.8 kWh) |
| 40797 | `CmpBMS.BatDschMinW` | ❌ | Minimale Entladestärke | `0` |
| 40799 | `CmpBMS.BatDschMaxW` | ✅ | Maximale Entladestärke | z.B. `5000` |
| 40801 | `CmpBMS.GridWSpt` | ❌ | Netz-Sollwert | `0` |
| 41259 | `CmpBMS.OpMod` | ❌ | Betriebsmodus | siehe unten |

> 💡 Wenn dieses Register-Set verwendet wird, muss die prognosebasierte Akkusteuerung im SunnyPortal/Home Manager deaktiviert sein – der Home Manager nutzt dieselben Register.

---

### Betriebsmodi (41259 / CmpBMS.OpMod)

| Adresse | SMA Bezeichnung | Wert | Modus |
|---|---|---|---|
| 41259 | `CmpBMS.OpMod` | `[0, 303]` | **Akku Pause** – kein Laden, kein Entladen |
| 41259 | `CmpBMS.OpMod` | `[0, 1438]` | **Dynamisch** – WR regelt auf GridWSpt |
| 41259 | `CmpBMS.OpMod` | `[0, 2289]` | **Nur Laden** |
| 41259 | `CmpBMS.OpMod` | `[0, 2290]` | **Nur Entladen** |

---

### Dynamisch-Modus (40236)

| Adresse | SMA Bezeichnung | Wert | Bedeutung |
|---|---|---|---|
| 40236 | – | `[0, 1438]` | Dynamischen Modus aktivieren (alternativ zu 41259) |

---

### Geräte-Neustart (40077)

> ⚠️ Nur im Notfall / zu Diagnosezwecken verwenden.

| Adresse | SMA Bezeichnung | Wert | Bedeutung |
|---|---|---|---|
| 40077 | `Sys.DevRstr` | `[0, 1415]` | Geräteneustart auslösen |

---

### Grid Guard Code (43090) – veraltet

> ℹ️ Der Grid Guard Code (GGC) war früher nötig um erweiterte Schreibzugriffe freizuschalten.  
> **Der GGC gilt zunehmend als deprecated** (Community-Rollout-Meldung ab ~18. März 2025).  
> ⚠️ Die kursierende Versionsangabe „2.16.4.R" entspricht dem **SHM2-Firmware-Schema** – die STP-SE-WR-Firmware nutzt eine andere Nummerierung (3.x). Es ist daher unklar, auf welches Gerät bzw. welche Firmware sich die Deprecation genau bezieht; Angabe mit Vorsicht behandeln.  
> Für die Akkusteuerung über 40149 / 40151 / 40793–40801 war der GGC ohnehin **nie erforderlich**.

| Adresse | Beschreibung | Gerät |
|---|---|---|
| 43090 | SMA Grid Guard Code | STP SE / SHM2 |

---

## SMA Sunny Home Manager 2 (SHM2) – eigene Register

> ⚠️ Die folgenden Register gelten für den **SHM2** als Modbus-Slave (typisch: IP des SHM2, Port 502, Slave 3).  
> Sie sind **nicht identisch** mit den Registern des STP SE Hybrid WR. Wer den WR direkt anspricht, verwendet diese Register **nicht**.

### Verbindungsparameter SHM2

| Parameter | Wert |
|---|---|
| Protokoll | Modbus TCP |
| Port | 502 |
| Slave ID | 3 |
| Firmware (bekannt getestet) | 2.15.7.R · 2.16.4.R |

### SHM2 Lese-Register

| Adresse | Beschreibung | Einheit | Hinweis |
|---|---|---|---|
| 30865 | Netzbezug (Import aus Netz) | W | Summe alle Phasen |
| 30867 | Netzabgabe (Einspeisung ins Netz) | W | Summe alle Phasen |

### SHM2 Schreib-Register

| Adresse | Beschreibung | Einheit | Werte | Hinweis |
|---|---|---|---|---|
| 40016 | Normierte Wirkleistungsbegrenzung (`WMaxLimPct`) | % | 0–100 | 0 = PV-Produktion auf 0 drosseln · 100 = volle Leistung · nur ganzzahlige % |
| 40149 | Batterieladung Sollwert | W | – | Wie STP SE direkt |
| 40151 | Externe Steuerung aktivieren/deaktivieren | – | `[0, 802]` / `[0, 803]` | Wie STP SE direkt |

> 💡 Register 40016 auf dem **SHM2** ist der empfohlene Weg um PV-Produktion bei negativen Strompreisen zu drosseln. Logik: Batterie wird zuerst vollgeladen, erst danach werden die Solarmodule gedrosselt. Ohne SHM2 ist eine vollständige DC-seitige Abschaltung nur mit einem physischen Trennschalter möglich.
>
> 🔧 **Ohne SHM2:** Direkt am WR lässt sich die Wirkleistung über **41255 `WNomPrc`** (normierte Wirkleistungsbegrenzung in %) drosseln – 0 % ≈ keine AC-Abgabe. Damit wird die PV-Produktion AC-seitig begrenzt (bei vollem Akku also faktisch gedrosselt). ⚠️ 41255 ist ein **RW-/persistenter Parameter** – nur **ereignisbasiert** schreiben (z.B. beim Wechsel in/aus dem Negativpreis-Fenster), **nicht zyklisch** (Flash-Verschleiß). Dies ist der wahrscheinlichste Hebel für die geplante „PV-Produktionspause" auf Anlagen ohne SHM2 und entspricht dem Roadmap-Punkt 41255.

---

## SMA SBS (Sunny Boy Storage) – Register

> 🔍 **Teilweise geklärt!** Schreib-Register für SBS 2.5 sind durch Community-Tests identisch mit dem STP SE. Für **SBS 3.7–10** sind die Schreib-Register inzwischen durch ein ioBroker-Projekt belegt (s.u.); die meisten **Lese-Register** dort bleiben aber offen: [Issue #9](https://github.com/Optic00/ha-opti-akkusteuerung/issues/9)

Die offizielle SMA Modbus-Dokumentation für den SBS findet sich unter:  
**https://www.sma.de/produkte/batterie-wechselrichter/sunny-boy-storage-37-50-60** → Downloads → „Parameter und Modbus"

### SBS Schreib-Register – bestätigt identisch mit STP SE

> ✅ **SBS 2.5:** Community-Tests bestätigen dieselben Schreib-Register wie der STP SE Hybrid.  
> ✅ **SBS 3.7–10:** dieselbe Registerfamilie wird im ioBroker-Projekt [Maverick78de/SMA_forecast_charging](https://github.com/Maverick78de/SMA_forecast_charging) produktiv genutzt (Geräte `DevType` 9300–9362, s. Abschnitt unten).

| Adresse | Funktion | STP SE | SBS 2.5 | SBS 3.7–10 |
|---|---|---|---|---|
| 40149 | Batterie-Leistungssollwert (`FedInPwrAtCom`) | ✅ | ✅ | ✅ (Maverick) |
| 40151 | Externe Steuerung (`FedInSpntCom`, 802/803) | ✅ | ✅ | ✅ (Maverick) |
| 40236 | Betriebsmodus (`CmpBMSOpMod`) | ✅ | ✅ | ✅ (Maverick) |
| 40793 | `CmpBMS.BatChaMinW` | ✅ | ✅ | ✅ ¹ (Maverick) |
| 40795 | `CmpBMS.BatChaMaxW` | ✅ | ✅ | ✅ (Maverick) |
| 40797 | `CmpBMS.BatDschMinW` | ✅ | ✅ | ✅ ¹ (Maverick) |
| 40799 | `CmpBMS.BatDschMaxW` | ✅ | ✅ | ✅ (Maverick) |
| 40801 | `CmpBMS.GridWSpt` | ✅ | ✅ | ✅ ² (Maverick) |

> ¹ Min-Leistungsregister (40793/40797) schreibt Maverick nur bei bestimmten Geräten (`DevType` 9324–9326, 9356–9359) und **verzögert** (≈1 s nach den anderen), um eine WR-Überlastung zu vermeiden.  
> ² `GridWSpt` (40801) wird nur bei `DevType ≥ 9300` (SBS-Klasse) geschrieben, ebenfalls verzögert.
>
> ⚠️ **Encoding beachten:** Der ioBroker-Modbus-Adapter schreibt **skalare** Registerwerte (z.B. `CmpBMSOpMod = 2424`, `FedInSpntCom = 803`), während diese HA-Doku teils **Wort-Paare** angibt (z.B. `40236 → [0, 1438]`). Werte sind daher **nicht 1:1** übertragbar – insbesondere ist der OpMod-Wert `2424` ein anderer als unsere bekannten `41259`-Enums (303/1438/2289/2290); seine Bedeutung ist ungeklärt. Vor Übernahme an echter HA-Anlage prüfen.

### SBS Lese-Register – bekannte Adressen (SBS 2.5)

| Adresse | Beschreibung |
|---|---|
| 30529 | Status |
| 30843 | Batteriespannung |
| 30845 | Batterie SoC |
| 30847 | Batterie-Ladestatus |
| 30849 | Batterietemperatur |
| 30851 | Gemessene Batteriespannung |
| 30955 | Leistungsdaten |
| 31061 | `Bat.ChaCtlComAval` (Steuerung verfügbar) |
| 31393 | Aktuelle Ladeleistung |
| 31395 | Aktuelle Entladeleistung |
| 31397 | Batterieparameter |
| 31401 | Batterieparameter |
| 33001 | Betriebsstatus |
| 34113 | Berechnungswert |
| 34661 | Effizienz/Performance |
| 34665 | Effizienz/Performance |

### SBS 3.7–10 Lese-Register – aus ioBroker-Projekt Maverick78de (Community, ungeprüft in HA)

> 🔍 Quelle: [`bat_regelung_2.3.4.js`](https://github.com/Maverick78de/SMA_forecast_charging/blob/master/bat_regelung_2.3.4.js) (Zeilen 40–60). Die Adressen sind dort als Modbus-Datapoints mit SMA-Kanalnamen hinterlegt und werden produktiv genutzt – aber **nicht** an einer HA-`modbus:`-Instanz dieses Repos verifiziert. Wie die ajay123-Register als inoffiziell behandeln.

| Adresse | SMA Kanal (lt. Maverick) | Typ | Beschreibung |
|---|---|---|---|
| 30053 | `DevTypeId` | input | Geräte-Typnummer – SBS-Erkennung (s. Tabelle unten) |
| 30775 | `PowerAC` | input | AC-Leistung (auch beim STP SE) |
| 30845 | `BAT_SoC` | input | Batterie-SoC – **identisch zum STP SE** |
| 30853 | `ActiveChargeMode` | input | Aktives Ladeverfahren (nur Sunny Island + Blei relevant) |
| 30867 | `TotWOut` | input | Einspeiseleistung am Netzanschlusspunkt |
| 31007 | `RmgChaTm` | input | Restladezeit Boost-Ladung (nur Blei-Speicher) |
| 31009 | `SelfCsmpDmLim` | input | Unteres Entladelimit Eigenverbrauch (Saison) – Geräte < 9356 |
| 40035 | `BatType` | holding | Batterietyp: `1785` = Lithium, sonst Blei/PB |
| 40073 | `SelfCsmpBatChaSttMin` | holding | Unteres Entladelimit Eigenverbrauch – **SBS 3.7–10** (statt 31009) |
| 40189 | `WMaxCha` | holding | Max. Ladeleistung des BatWR (auslesbar) |
| 40191 | `WMaxDsch` | holding | Max. Entladeleistung des BatWR (auslesbar) |

**Geräte-Typnummern (`DevType`, Register 30053) lt. Maverick:**

| DevType | Klasse | Besonderheit im Schreibpfad |
|---|---|---|
| < 9300 | ältere Geräte (z.B. Sunny Island) | nutzen `ActiveChargeMode` (30853); kein `GridWSpt` |
| ≥ 9300 | SBS-Klasse | zusätzlich `GridWSpt` (40801) schreiben |
| 9324–9326 | SBS (Untergruppe) | zusätzlich Min-Register 40793/40797 (verzögert) |
| 9356–9362 | **SBS 3.7–10** | Entladelimit über 40073 statt 31009; 9356–9359 zusätzlich Min-Register |

### Noch gesuchte SBS-3.7+-Lese-Register (nicht in Mavericks Skript)

| Funktion | STP SE Adresse | SBS 3.7–10 |
|---|---|---|
| Batterie SoC | 30845 | ✅ **30845** (Maverick) |
| WR-Status | 33003 | ❓ |
| WR-Temperatur | 30953 | ❓ (Maverick liest keine Temperatur) |
| Steuerung verfügbar | 31061 | ❓ |
| Ladeistung aktuell | 31393 | ❓ (Maverick berechnet indirekt) |
| Entladeleistung aktuell | 31395 | ❓ |

### Hinweis zur SMA-Namenskonvention

Die offizielle SMA-Serviceanleitung *TOR Erzeuger Typ A 2019* ([Download SMA](https://files.sma.de/downloads/TORErzeuger_TYP_A_2019_Paraeinst-SG-de-13.pdf)) listet den SBS3.7-10 als unterstütztes Gerät und bestätigt, dass der SBS dieselbe SMA-Objektnamen-Konvention verwendet (`Inverter.WModCfg.*`, `Inverter.VArModCfg.*` usw.). Die Modbus-**Adressen** können trotzdem abweichen – das Dokument enthält nur Parameternamen, keine Registeradressen.

---

## Bekannte Probleme & Hinweise

**Werte werden nach ~1–4 Minuten zurückgesetzt:**  
Der SMA Home Manager überschreibt die Modbus-Werte, wenn die prognosebasierte Akkusteuerung im SunnyPortal aktiviert ist. Dort deaktivieren.

**Ladeleistung fällt alle 6 Minuten kurz auf 0:**  
Shadefix zieht periodisch die Steuerung zurück. In den WR-Einstellungen auf 30 Minuten setzen oder deaktivieren.

**Register-Adressen in HA vs. SMA-Dokumentation:**  
SMA nummeriert Register ab 40001 (1-indexed). HA Modbus verwendet die Adresse direkt – die HA-Konfiguration nutzt dieselben Zahlen wie die SMA-Dokumentation.

---

## Quellen

- **ajay123** im Photovoltaikforum: [Direkte SMA-Support-Antwort mit offiziellen Registernamen](https://www.photovoltaikforum.com/thread/215473-begrenzen-der-lade-entladeleistung-byd-mit-stp-se/?postID=4033278#post4033278) *(Hauptquelle für die BMS-Register)*
- **Skybarks** im Photovoltaikforum: [Hinweis auf offizielle SMA Modbus-Dokumentation](https://www.photovoltaikforum.com/thread/206718-sma-stp10-0-3se-40-welcher-modbus-register-zum-laden-der-batterie/?pageNo=6)
- **Community-Sammlung** im Photovoltaikforum: [SMA Modbus – Welche Register nutzt ihr?](https://www.photovoltaikforum.com/thread/244594-sma-modbus-welche-register-nutzt-ihr/) *(SBS 2.5 Bestätigung, SHM2-Register, GGC-Deprecation)*
- **Maverick78de** auf GitHub: [SMA_forecast_charging](https://github.com/Maverick78de/SMA_forecast_charging) (ioBroker, archiviert 2023) *(SBS-3.7–10-Schreib-/Lese-Register und DevType-Zuordnung – `bat_regelung_2.3.4.js`)*
- Offizielle SMA Modbus-Dokumentation: Über das SMA Service-Portal erhältlich (Registrierung erforderlich)

---

*Letzte Aktualisierung: Mai 2026 – Ergänzungen willkommen via Pull Request oder Issue.*
