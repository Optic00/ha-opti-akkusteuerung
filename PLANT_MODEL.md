# Anlagenbilanz und externe Quellen

Opti Akku bleibt eine Integration mit getrenntem Gerätezugriff, Quellennormalisierung, Anlagenbilanz, Laststatistik und optionaler Strategie. Herstellerintegrationen für weitere Messwerte bleiben verwendbar. Es sind keine alten Opti-Packages oder manuell angelegten Rechenhelfer erforderlich; Tarif- und Prognoseanbieter bleiben externe HA-Integrationen.

## Messgrenze

Der SMA-Treiber liest die AC-Ausgangsleistung über Register 30775. Darin ist die Wirkung der Batterie bereits enthalten. Bei passender gemeinsamer Messgrenze gilt:

`Hausverbrauch = Summe AC-Wechselrichter + Netzbezug - Einspeisung`

Batterieleistung nicht nochmals addieren. PV-DC-Erzeugung und AC-Ausgang sind unterschiedliche Größen und dürfen nicht als identische Beiträge summiert werden. Vorzeichen, zeitliche Aktualität und Messgrenze müssen für sämtliche Quellen stimmen.

Im Assistenten entweder einen vollständigen vorhandenen Hausverbrauchssensor wählen oder für SMA die passende native Bilanz konfigurieren. Die einfache Einzelwechselrichter-Bilanz ist nur korrekt, wenn dessen Netzzähler alle relevanten Erzeuger und Verbraucher erfasst. Zusätzliche AC-Erzeuger und ausdrücklich ausgeschlossene Lasten benötigen gültige HA-Entitäten. Konfigurierte fehlende Werte sind keine 0-W-Messung.

Huawei verwendet vorhandene Entitäten seiner Herstellerintegration und einen expliziten externen Hausverbrauch. Die SMA-Bilanz wird dort nicht angeboten. Details zur experimentellen Schreibsteuerung: [Huawei](HUAWEI_CONTROL.md).

## Statistik und Umstellung

Rohleistung und geglättetes Lastprofil erfüllen unterschiedliche Zwecke. Ein bereits gemittelter Legacy-Hausverbrauch wird bei erneuter Mittelung zusätzlich geglättet; das ist keine feste zweistündige Verzögerung. Vor Quellenwechseln Verbrauchsumfang, etwa enthaltene Wallboxen oder Wärmepumpe, prüfen. Eine veränderte Messgrenze macht alte Lernhistorie möglicherweise unpassend.

Unter **Messquellen und Bilanz vergleichen** lassen sich native Bilanz, Lastabzug und zeitgewichtete Mittelwerte zunächst nur beobachten. Dieser Vergleich ersetzt weder die aktive Quelle noch deren Frischeprüfung. Zeitstempel einer HA-Meldung beweisen keine neue physikalische Messung. Die detaillierten Grenzen stehen unter [Bedarfsprofil und Messquellenvergleich](docs/forecast-and-ev.md).

Die Geräteadapter besitzen spezifische Fähigkeiten. Ein weiteres Gerät wird nicht allein aufgrund derselben Marke oder ähnlicher Registernamen als schreibkompatibel betrachtet. Die spätere Trennung in Adapter und Strategie bleibt möglich, ist aber kein zweites Installationsprodukt in dieser Beta.
