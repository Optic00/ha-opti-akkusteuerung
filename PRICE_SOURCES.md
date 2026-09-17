# Datierte Preisquellen

Der ausgewählte Preissensor braucht weiterhin einen endlichen numerischen Zustand
und eine zur Konfiguration passende Einheit `EUR/kWh` oder `ct/kWh` einschließlich
der bereits unterstützten Schreibweisen. `price_max_age` prüft seine Meldefrische.

Für Preisplanung liefern `today` und optional `tomorrow` vollständige Tageslisten.
Die expliziten Zeitstempel bestimmen den Kalendertag in der HA-Zeitzone, nicht der
Attributname. Nach Mitternacht kann daher die zuvor als `tomorrow` gelieferte
Liste verwendet werden, bis der Anbieter seine Attribute rotiert. Gestern wird
nicht als heute umgedeutet. Doppelte Tage, Überschneidungen und ungültige Listen
werden abgelehnt. Jeder Eintrag hat dieses Schema:

```json
{"start": "2026-09-13T00:00:00+02:00", "end": "2026-09-13T01:00:00+02:00", "price": 0.25}
```

`start` und `end` sind ISO-8601-Zeitpunkte mit explizitem Offset oder `Z`; intern
werden auch zeitzonenbehaftete Python-`datetime`-Werte akzeptiert. Als Preisfeld
gelten in bestehender Priorität `total`, `price`, `value`. Alle Preise verwenden
die Einheit des Sensors; negative Preise bleiben erlaubt.

Ein Tag muss lückenlos und chronologisch von Mitternacht bis zur nächsten
Mitternacht abgedeckt sein. Ein Raster hat einheitlich 60 oder 15 Minuten reale
Dauer. Bei Sommerzeitwechseln ergeben sich 23/25 Stunden beziehungsweise 92/100
Viertelstunden; die doppelte Stunde benötigt ihre unterschiedlichen Offsets.
Eine ungültige oder falsch datierte Liste verwirft den gesamten Preisplan mit
`invalid_price_series`, auch wenn der Sensor gerade einen neuen Skalar meldet.

Bestehende Zahlenlisten oder Dictionaries ohne beide Intervallgrenzen sind
absichtlich nicht mehr gültig. Der Anbieter beziehungsweise eine vorgeschaltete
Quellintegration muss die tatsächlichen Lieferzeitpunkte bereitstellen. Sie aus
`now()`, dem aktuellen Sensordatum oder `last_updated` zu ergänzen behebt den
Fehler nicht und ist kein zulässiger Migrationsweg.

Bei fehlendem Plan werden Preisniveau und Peak-Reserve ungültig. Der Standard-
Rückfall hält einen alten Entlade- oder Netzlademodus nicht fest, sondern wechselt
auf `Akku Dynamisch`. Weiterhin vorrangige Schutz-, SoC- und andere unabhängig
gültige Strategiebedingungen bleiben wirksam; es gibt keine pauschale Pause.
Eine weiterhin gültige aktuelle Einzelpreisquelle bleibt unabhängig verfügbar,
einschließlich bereits vorhandener Regeln für sehr niedrige Einzelpreise. Die
Korrektur beweist nicht, dass Einzelpreis und Tagesplan aus derselben Anbieter-
Revision stammen. Physische Gerätewirkung ist durch die synthetischen Tests
nicht nachgewiesen.

## Bestehende Tibber-Integration

Im Stromtarif-Schritt kann stattdessen **Tibber** ausgewählt werden. Der Assistent
liest über `tibber.get_prices` aus dem bereits in HA angemeldeten Konto. Es gibt
keinen zusätzlichen Token, keinen Templatehelfer und keine zweite Tibber-Anmeldung.
Der erste Anschluss unterstützt ein Konto mit einem Zuhause und ausschließlich
explizit bestätigte EUR/kWh-Tarife. HA liefert weder Währung noch unverwechselbare
Home-IDs; Nicknames müssen eindeutig sein. Mehrere Konten oder mehrere zurück-
gelieferte Homes werden abgelehnt. Gleiche Nicknames mehrerer Homes lassen sich
in dieser HA-Serviceantwort nicht zuverlässig erkennen; solche Konten sind nicht
unterstützt. Der gewählte Home-Key und die HA-Kontoeintrags-ID bleiben fest gebunden.
Umbenennen oder Austauschen verlangt erneute Konfiguration.

Aktueller Preis und Planung stammen aus demselben vollständig datierten Snapshot.
Intervallenden werden nur aus einem lückenlos nachgewiesenen Tagesraster gebildet;
fehlende Zeitpunkte werden nicht aufgefüllt. Der Cache liegt ausschließlich im
Speicher. Erfolgreiche Abrufe werden spätestens nach 30 Minuten beziehungsweise
vor dem eingestellten Höchstalter erneuert. Das maximale Preisalter muss für
Tibber mindestens 60 Sekunden betragen. Fehler verlängern keine Gültigkeit; der
Abruf wird nach fünf Minuten erneut versucht. Ein gültiger Cache kann solange
weiterverwendet werden, wie Alter, Kontobindung und zeitliche Abdeckung passen.
HA kann selbst gecachte Anbieterpreise liefern: Die Empfangszeit belegt nur den
Serviceabruf. Datierte Abdeckung wird unabhängig geprüft.

Abrufe laufen außerhalb der Gerätesperre und werden beim Entladen abgebrochen.
Unmittelbar vor einem Schreibbefehl werden Preisalter und aktuelle Intervall-
abdeckung nochmals geprüft. Ein zwischenzeitlicher Intervallwechsel verwirft
stattdessen den veralteten Befehl. Ohne gültigen Tibber-Snapshot sind sowohl
aktueller Preis als auch Planung ungültig.

## Wirtschaftlichkeitsvergleich für Akku-Arbitrage

Im Optionsmenü lässt sich eine rein informative Vergleichsrechnung aktivieren.
Alle Annahmen müssen ausdrücklich eingetragen werden: Akkuanteil am Kaufpreis,
angesetzter Wertverlust des Akkus über die angenommenen Zyklen, Zyklenzahl,
nutzbare Kapazität, Lade- und
Entladewirkungsgrad sowie eine zusätzliche Mindestmarge. Es gibt keine verdeckten
Standardwerte. Der Wertverlust ist der Anteil des eingetragenen Akku-Kaufpreises,
der über die angenommene Zyklenzahl als verbraucht angesetzt wird.

Aus dem angesetzten Verschleißwert wird zuerst der Preis je kWh Akkudurchsatz
berechnet. Ein zusätzlicher Lade- und Entladezyklus enthält diesen Durchsatz
zweimal. Für den aktuellen Ladepreis zeigt der Sensor anschließend den mindestens
nötigen höheren Preis und den daraus folgenden Preisabstand:

```text
p_hoch_min = (p_niedrig / eta_laden + 2 * c_durchsatz + marge) / eta_entladen
```

Die Marge ist dabei wie die Durchsatzkosten auf eine kWh Akkuenergie bezogen und
steht deshalb innerhalb der Wirkungsgradkorrektur.

Die Anzeige bepreist nur einen zusätzlichen Netz-Arbitragezyklus. Sie bewertet
keine ohnehin stattfindende PV-Nutzung und behauptet keine genaue Akku-Lebensdauer.
Sie ändert weder bestehende Mindestspreads noch Lade-, Entlade- oder
Reserveentscheidungen. Eine spätere aktive Nutzung braucht zuerst reale Vergleiche
mit Kosten, Wirkungsgraden und tatsächlich verschobener Energie.
Bei vollständig deaktivierter Strategie lautet der Status `strategy_disabled`,
weil Preisquellen in diesem Betriebszustand nicht eingelesen werden.
