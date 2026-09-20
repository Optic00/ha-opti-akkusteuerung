## Geführte Einrichtung und Konfiguration

Der native HA-Assistent beginnt mit der Backendwahl
SMA Modbus oder Huawei Solar. Der SMA-Pfad führt durch Verbindung, Anlage, Batteriegrenzen,
Stromtarif (einschließlich vorhandener Tibber-Anbindung), PV-Prognose und Benachrichtigungen. Wallboxen und Balancing werden nur bei Bedarf
abgefragt. Die Zusammenfassung zeigt Quellen, SoC-Grenzen und Shadow-/Standardmodus.
Erst das abschließende Speichern übernimmt die Einstellungen. Es werden keine
externen Helfer oder Automationen angelegt.

Später öffnet **Konfigurieren** ein Menü mit einzeln bearbeitbaren Bereichen.
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

Unter **Batterie und Leistungsgrenzen** kann die **Prognoseunabhängige Ladegrenze**
vorübergehend aktiviert werden. Dann verwendet die berechnete Ladeleistung die
eingestellte maximale Ladestärke statt der prognoseabhängigen Staffelung.
Temperaturdrosselung und -abschaltung, der obere SoC-Taper ab 97 Prozent sowie
der Balancing-Taper bleiben wirksam. Das gilt sowohl für manuell gewählte als
auch für automatisch gewählte Modi, die die berechnete Ladeleistung verwenden
(**Akku nur Laden**, **Akku Dynamisch**, **Akku Netzladen**).
Nach HA-Neustart oder Neuladen der Integration ist die Option wieder aus; eine
YAML-Migration aktiviert sie nicht automatisch. Die Wattgrenze wird übernommen.
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

**Upgrade:** Einstellungen werden erhalten. Ein einzelner ungültiger gespeicherter
Wert setzt nur diesen Wert bzw. ein widersprüchliches Grenzpaar zurück und sperrt
Schreibzugriffe mit einer Diagnose. Schreibfreigaben sind nun an Verbindung und
Seriennummer gebunden. Eine alte gespeicherte Freigabe ohne Gerätebindung muss
nach dem Upgrade einmal bewusst neu aktiviert werden; Shadow bleibt immer lesend.
Bei Gerätewechsel wird die Bestätigung für die alleinige Steuerung zurückgesetzt.
Der Referenz-Ladepreis neuer Einträge startet wie die ursprüngliche Strategie mit
`-1 EUR/kWh` (noch keine Referenz), statt wie bisher mit `0`; gespeicherte Werte
werden nicht geändert.
