# Sicherheitslücken melden

Opti Akku schreibt auf Wechselrichter und Akkus. Sicherheitslücken bitte **nicht**
in einem öffentlichen Issue melden, sondern privat über
[„Report a vulnerability“](https://github.com/Optic00/ha-opti-akkusteuerung/security/advisories/new).

## Was hierher gehört

- Schreibzugriffe trotz ausgeschalteter Schreibfreigabe oder im Shadow-Betrieb
- umgangene Schreibsperren, Sicherheitsgrenzen oder Gerätebindungen
- Zugangsdaten, Seriennummern, Hostnamen oder Messhistorien im Diagnoseexport, in
  Logs oder in Benachrichtigungen; ausgenommen ist der dokumentierte
  Entscheidungsverlauf der letzten 24 Stunden mit Ladestand, Preis und
  Entscheidungsgrund sowie die Ladestände des aktuellen und letzten Peak-Zeitraums
  aus dem Betriebsbericht ([Diagnose herunterladen](docs/operation.md#diagnose-herunterladen))

Fehlentscheidungen der Strategie, falsche eigene Einstellungen, Herstellerfirmware
und Modbus ohne Authentifizierung im lokalen Netz sind keine Sicherheitslücken
dieses Projekts. Solche Fälle bitte als normales Issue melden.

## Hilfreiche Angaben

Version, Anbindung (SMA oder Huawei), Betriebszustand, Schritte zur Reproduktion
und die beobachtete Wirkung. Keine echten Zugangsdaten mitschicken. Zum
Nachstellen keine Schreibfreigabe an einer produktiven Anlage einschalten.

## Unterstützte Versionen

Korrekturen gibt es nur für die jeweils neueste Version, einschließlich Betas.

## Ablauf

Opti Akku ist ein Hobbyprojekt ohne zugesagte Reaktionszeiten oder Prämien. Ich
bestätige die Meldung, stimme die Behebung mit dir ab und veröffentliche Details
erst, wenn eine korrigierte Version verfügbar ist.
