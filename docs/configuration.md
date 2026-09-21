## Geführte Einrichtung und Konfiguration

Der native HA-Assistent beginnt mit der Backendwahl
SMA Modbus oder Huawei Solar. Der SMA-Pfad führt durch Verbindung, Anlage, Batteriegrenzen,
Stromtarif (einschließlich vorhandener Tibber-Anbindung), PV-Prognose und Benachrichtigungen. Wallboxen und Balancing werden nur bei Bedarf
abgefragt. Die Zusammenfassung zeigt Quellen, SoC-Grenzen und Shadow-/Standardmodus.
Erst das abschließende Speichern übernimmt die Einstellungen. Es werden keine
externen Helfer oder Automationen angelegt.

Später öffnet **Konfigurieren** die Bereiche Anlage, Batterie, Stromtarif und
PV-Prognose. Unter **Optionale Funktionen** stehen Wallbox, PV-Vorbereitung fürs
Auto, Balancing, Bedarfsprofil, Wirtschaftlichkeitsvergleich und Benachrichtigungen.
**Erweitert und Diagnose** enthält Verbindung, Messquellenvergleich und Feinabstimmung.
Die eigenständige Vergleichskonfiguration bleibt erhalten, um andere Messquellen
vor einer aktiven Umstellung zu prüfen.
Alle Änderungen bleiben bis **Prüfen und speichern** ein Entwurf. Reine
Strategiewerte werden als ein validierter Satz ohne Neuladen übernommen;
Verbindungs- oder Quellenänderungen laden die Integration neu. Vorhandene
Entitäts-IDs bleiben erhalten, auch wenn die angezeigten Namen kürzer werden.
Bei neuen Installationen sind einzelne Zahlen- und technische Schalterentitäten
standardmäßig deaktiviert: Die Konfiguration erfolgt über das Menü. Wer diese
Entitäten für eigene Automationen benötigt, kann sie in der Entitätsverwaltung
aktivieren. Die Hauptfreigabe und Betriebsart bleiben direkt bedienbar. Bestehende
Entitäten und deren Aktivierungszustand werden beim Upgrade nicht verändert. Interne Rechensensoren sind Diagnose und bei neuen
Einträgen standardmäßig deaktiviert. Bereits aktivierte Diagnosen werden beim
Upgrade nicht gegen die Auswahl des Benutzers deaktiviert.

Der Assistent prüft ausgewählte Quellen auf Verfügbarkeit, Alter, Zahlenwerte,
Einheit und zusammengehörige Quellen. Widersprüchliche Euro-/Cent-Angaben werden
abgelehnt. Preisoptimierung kann durch leere Preisfelder ausgelassen werden,
Prognoseplanung durch drei leere Prognosefelder; die Zusammenfassung weist auf
fehlende Quellen hin. Cloud-Anbieter müssen weiterhin vorher in HA eingerichtet
sein. Vorschläge bereits vorhandener kanonischer Quellen sind bearbeitbar.
Bei Modus, Ladestatus und Smart Cost von evcc wird nur geprüft, ob der Zustand
gültig ist. Das Alter des Werts spielt keine Rolle, weil evcc unveränderte Werte
nicht regelmäßig neu senden muss.

Nach der Einrichtung unter **Akku und Leistungsgrenzen** kann die **Prognoseunabhängige Ladegrenze**
vorübergehend aktiviert werden. Dann verwendet die berechnete Ladeleistung die
eingestellte maximale Ladestärke statt der prognoseabhängigen Staffelung.
Temperaturdrosselung und -abschaltung, der obere SoC-Taper ab 97 Prozent sowie
der Balancing-Taper bleiben wirksam. Das gilt sowohl für manuell gewählte als
auch für automatisch gewählte Modi, die die berechnete Ladeleistung verwenden
(**Akku nur Laden**, **Akku Dynamisch**, **Akku Netzladen**).
Nach HA-Neustart oder Neuladen der Integration ist die Option wieder aus; eine
YAML-Migration aktiviert sie nicht automatisch. Nicht zusammen mit Verbindungs-
oder Quellenänderungen einschalten, da diese ein Neuladen auslösen. Die Wattgrenze wird übernommen.
Eine Ladeobergrenze erzwingt kein reines PV-Laden; dafür müssen auch Betriebsart,
Mindestladeleistung und Netzladefreigaben passen. **Strategie berechnen** aus
bleibt eine Pause, auch bei manueller Moduswahl. Die Option ist kein Ersatz für
Schreibfreigabe oder Hauptschalter.

Für ausgeschlossene Verbraucher wie Wallboxen gibt es unter **Anlage und
Messwerte** die Auswahl **Ausschlusslasten mit unverändert gültigen 0 W**.
Hier dürfen nur bereits ausgeschlossene Sensoren gewählt werden, deren
Integration unveränderte Nullwerte nicht regelmäßig meldet und einen
Verbindungsausfall zuverlässig als `unavailable` kennzeichnet. Für diese Quellen
bleiben exakt 0 W oder 0 kW ohne Altersgrenze gültig. Positive Leistungen,
ungültige Einheiten, fehlende oder nicht verfügbare Quellen werden weiterhin
abgewiesen. Hauszähler und zusätzliche Wechselrichter erhalten diese Ausnahme
nicht. Standardmäßig ist die Auswahl leer; ein hängender letzter Nullwert ohne
verlässliche Verfügbarkeitsmeldung ist kein Nachweis für einen ausgeschalteten
Verbraucher. Der Sensor für den rohen Hausverbrauch zeigt die dabei verwendeten
alten Nullwerte im Attribut `stale_zero_sources`. Eine Änderung der Auswahl
beginnt ein neues Verbrauchsprofil, damit unterschiedliche Gültigkeitsregeln
nicht vermischt werden.

### Prognosewerte im Dashboard und bisherige Mapping-Sensoren

Unter **Konfigurieren → PV-Prognose** können die ursprünglichen HA-Sensoren für
heute, morgen und den Rest von heute direkt ausgewählt werden. Die Helfer aus
`opti_mapping.yaml` sind für HACS keine Voraussetzung. Bei direkter Zuordnung
müssen Einheit und benötigte Attribute erhalten sein, insbesondere `estimate10`
für die P10-Mischung. Die Bedarfsprognose verwendet separat ausgewählte
Solcast-Quellen für heute und morgen mit datierten `detailedForecast`-Intervallen
und `pv_estimate10` in kW; diese Quellen werden im Bedarfsprofil konfiguriert.
Eine bestehende Umrechnung oder Zusammenfassung mehrerer Quellen
im Mapping darf nicht ersatzlos entfallen.

Für Dashboards sind diese Werte zu unterscheiden:

| Interner Wert | Anzeige in der Entitätsverwaltung | Bedeutung |
| --- | --- | --- |
| `sensor.opti_forecast_today_kwh` | PV-Prognose heute | Zugeordnete Tagesprognose, auf kWh normalisiert |
| `sensor.opti_forecast_tomorrow_kwh` | PV-Prognose morgen | Zugeordnete Tagesprognose für morgen, auf kWh normalisiert |
| `sensor.opti_forecast_effective_remaining_kwh` | Opti Forecast Effective Remaining kWh | Restprognose nach P10-/Median-Mischung und eingestelltem Optimismus |

Diese Diagnoseentitäten sind standardmäßig deaktiviert. Unter **Einstellungen →
Geräte & Dienste → Entitäten** nach Integration **Opti Akku** filtern, deaktivierte
Entitäten einblenden und den gewünschten Sensor aktivieren. Die internen Namen
in der Tabelle sind keine garantierten HA-Entitäts-IDs. HA bildet diese aus
Geräte- und Entitätsnamen; eine ID kann deshalb zweimal `opti` enthalten.
Die tatsächliche ID steht in den Entitätseinstellungen. Bestehende IDs werden
beim Upgrade nicht umbenannt.

Die Morgenprognose ist keine zusätzliche, bereits gemischte Restprognose. Die
Strategie berücksichtigt ihr P10-Attribut und den Optimismus an den jeweiligen
Berechnungsstellen. Deaktivierte Anzeigeentitäten verhindern diese interne
Berechnung nicht.

Vor dem Entfernen alter Mapping-Sensoren erst die Quellen in HACS umstellen
und gültige Werte prüfen. Anschließend auch Dashboards, andere Automationen und
die noch verwendete Legacy-Steuerung auf Verweise prüfen. Erst nicht mehr
benötigte Mapping-Sensoren entfernen.

### Unveränderte HA-Leistungswerte und Ausfälle

Unter **Anlage und Messwerte** legt **Altersgrenze für HA-Leistungswerte** fest,
wie lange gemappte Leistungen ohne neue Meldung verwendet werden. Standard
bleibt 900 Sekunden. Maßgeblich ist `last_reported`, nicht die letzte Wertänderung.
[HA aktualisiert diesen Zeitstempel bei jeder Meldung](https://developers.home-assistant.io/blog/2024/03/20/state_reported_timestamp/),
auch wenn der Zahlenwert gleich bleibt. Eine ereignisbasierte Integration muss
aber nicht regelmäßig melden. Eine alte Meldung beweist daher keinen Geräteausfall.

**0** wählt ausdrücklich die Verfügbarkeit der HA-Quelle statt einer zusätzlichen
Altersgrenze. Das gilt für gemappten Hausverbrauch, PV-/AC-Leistung, weitere
Wechselrichter und Ausschlusslasten, auch bei positiven Leistungen. Fehlende,
`unknown`/`unavailable`, unplausible Zeitstempel, ungültige Einheiten und Werte
bleiben Fehler. Native Geräteabfragen, Huawei-Batterieeingänge, Zellspreizung,
Preise, PV-Prognosen und die gesonderten Wärmepumpenquellen der Bedarfsprognose
werden dadurch nicht von ihren Prüfungen befreit. Huawei und Zellspreizung
behalten bei 0 die bisherige 900-Sekunden-Grenze.

Nur 0 wählen, wenn die Quellintegration Ausfälle zuverlässig als `unavailable`
meldet. Bei selbst gebauten [Template-Sensoren](https://www.home-assistant.io/integrations/template/#availability)
muss deren `availability` die tatsächliche Quelle berücksichtigen. Ein Template,
das bei fehlenden Daten per `float(0)` weiter Null liefert oder immer verfügbar
bleibt, ist dafür ungeeignet. Ein Zeittrigger, der lediglich denselben alten Wert
neu veröffentlicht, ist ebenfalls kein Nachweis aktueller Gerätedaten.

Bestehende Einstellungen ändern sich beim Update nicht. Eine Änderung dieser
Gültigkeitsregel beginnt ein neues Verbrauchsprofil. Die gezielte Beta-6-Ausnahme
für alte Nullwerte von Ausschlusslasten bleibt verfügbar; bei 0 ist sie nicht
zusätzlich nötig. Vor einem Downgrade auf Beta 6 wieder eine positive Altersgrenze
setzen: Dort bedeutet 0 noch nicht „HA-Verfügbarkeit“ und führt zur Ablehnung
älterer Messwerte.

**Upgrade:** Einstellungen werden erhalten. Ein einzelner ungültiger gespeicherter
Wert setzt nur diesen Wert bzw. ein widersprüchliches Grenzpaar zurück und sperrt
Schreibzugriffe mit einer Diagnose. Schreibfreigaben sind nun an Verbindung und
Seriennummer gebunden. Eine alte gespeicherte Freigabe ohne Gerätebindung muss
nach dem Upgrade einmal bewusst neu aktiviert werden; Shadow bleibt immer lesend.
Bei Gerätewechsel wird die Bestätigung für die alleinige Steuerung zurückgesetzt.
Der Referenz-Ladepreis neuer Einträge startet wie die ursprüngliche Strategie mit
`-1 EUR/kWh` (noch keine Referenz), statt wie bisher mit `0`; gespeicherte Werte
werden nicht geändert.
