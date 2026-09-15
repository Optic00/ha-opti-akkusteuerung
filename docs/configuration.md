## Geführte Einrichtung und Konfiguration

Der native HA-Assistent beginnt mit der Backendwahl
SMA Modbus oder Huawei Solar. Der SMA-Pfad führt durch Verbindung, Anlage, Batteriegrenzen,
Stromtarif und PV-Prognose. Wallboxen und Balancing werden nur bei Bedarf
abgefragt. Die Zusammenfassung zeigt Quellen, Ladestand und Shadow-/Standardmodus.
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

**Upgrade:** Einstellungen werden erhalten. Ein einzelner ungültiger gespeicherter
Wert setzt nur diesen Wert bzw. ein widersprüchliches Grenzpaar zurück und sperrt
Schreibzugriffe mit einer Diagnose. Schreibfreigaben sind nun an Verbindung und
Seriennummer gebunden. Eine alte gespeicherte Freigabe ohne Gerätebindung muss
nach dem Upgrade einmal bewusst neu aktiviert werden; Shadow bleibt immer lesend.
Bei Gerätewechsel wird die Bestätigung für die alleinige Steuerung zurückgesetzt.
Der Referenz-Ladepreis neuer Einträge startet wie die ursprüngliche Strategie mit
`-1 EUR/kWh` (noch keine Referenz), statt wie bisher mit `0`; gespeicherte Werte
werden nicht geändert.
