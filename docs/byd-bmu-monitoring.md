# BYD-BMU-Monitoring (optional): Zellspannungen, Spreizung und Balancing in Home Assistant

Dieses optionale Modul liest die BMU/BMS-Daten einer BYD Battery-Box Premium HVS aus und macht sie in Home Assistant sichtbar: Einzelmodul-Zellspannungen, Zell-Spreizung, Temperaturen, SoH, Balancing-Status.
Es ist rein beobachtend und hat keinerlei Steuerfunktion.
Das zugehörige HA-Package ist [`packages/byd_bmu.yaml`](../packages/byd_bmu.yaml).

Die Datenquelle ist das Windows-/Linux-Tool **BYD-Logger** (`bydlogc`) von "olli" aus dem Photovoltaikforum.
Das Tool wird hier **nicht** mitverteilt - die Binaries gibt es beim Autor im entsprechenden Forums-Thread.
Danke an olli für das Tool.

## Architektur

```
BYD Battery-Box (BMU, WLAN-Modul, Standard-IP 192.168.16.254)
        │  proprietäres Protokoll, Port 8080, NUR EINE Verbindung
        ▼
bydlogc (Linux-Binary) in Docker-Container oder VM
        │  MQTT, Root-Topic "Battery"
        ▼
MQTT-Broker (z. B. Mosquitto-App in HA)
        │
        ▼
packages/byd_bmu.yaml  →  sensor.byd_* in Home Assistant
```

## Voraussetzungen

1. Eine BYD Battery-Box, deren WLAN-Modul im Heimnetz hängt (das Modul spannt sonst nur einen eigenen Hotspot auf).
2. Ein Docker-Host oder eine VM für `bydlogc` (x86_64, glibc - Alpine/musl funktioniert NICHT).
3. Ein MQTT-Broker, den Home Assistant bereits nutzt.
4. Netz-Routing zur Box (siehe nächster Abschnitt - das ist die häufigste Stolperfalle).

## Netzwerk: die Hairpin-Falle

Die Box nutzt intern die feste IP `192.168.16.254`, erreichbar über ihr WLAN-Modul, das im Heimnetz eine normale Client-IP hat.
Der übliche Weg ist eine statische Route auf dem Router: `192.168.16.254/32` → WLAN-Modul-IP.

**Achtung:** Liegt der abfragende Host im selben Subnetz wie das WLAN-Modul, muss der Router das Paket zurück ins selbe Netz routen (Hairpin).
Manche Gateways (beobachtet mit UniFi) leiten dabei nur das erste Paket eines Flows weiter, senden einen ICMP-Redirect und verwerfen den Rest.
Windows folgt dem Redirect und funktioniert; Linux-Hosts mit aktiviertem IP-Forwarding (jeder Docker-Host!) ignorieren Redirects und scheitern - TCP-Connect klappt, danach kommt nichts mehr.

Zwei erprobte Lösungen:

- **Host-Route auf dem Docker-Host:** `ip route replace 192.168.16.254/32 via <WLAN-Modul-IP>` (persistieren z. B. als if-up-Hook).
- **SNAT-Regel auf dem Router** (eleganter, gilt für alle Clients): Source-NAT für Destination `192.168.16.254/32` auf die Router-LAN-IP.
  Der Rückweg läuft dann symmetrisch über den Router statt über den kaputten Hairpin-Pfad.

## bydlogc als Docker-Container

Beispiel-Setup (Binary selbst besorgen und neben die Dateien legen):

`Dockerfile`:

```dockerfile
FROM debian:bookworm-slim
COPY bydlogc_lin64 /usr/local/bin/bydlogc.dist
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/bydlogc.dist /entrypoint.sh
WORKDIR /data
ENTRYPOINT ["/entrypoint.sh"]
```

`entrypoint.sh` - das Tool beendet sich regulär nur über die Taste "x" auf stdin und schreibt beim Exit seine Konfiguration; der Entrypoint bildet das über ein FIFO nach, damit `docker stop` sauber funktioniert:

```bash
#!/bin/bash
# bydlogc legt cfg + bmsdata NEBEN dem Binary an -> Binary ins Volume kopieren,
# damit alles persistent ist. Beenden regulaer nur ueber Taste "x" auf stdin.
set -u
cp -f /usr/local/bin/bydlogc.dist /data/bydlogc
cd /data

FIFO=/tmp/bydlogc.stdin
rm -f "$FIFO"
mkfifo "$FIFO"
exec 3<>"$FIFO"

term() {
  echo "[entrypoint] Stop-Signal -> sende x an bydlogc"
  printf "x\n" >&3
  for _ in $(seq 1 20); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID"
  fi
}
trap term TERM INT

./bydlogc <&3 &
PID=$!
wait "$PID"
wait "$PID"
exit $?
```

`docker-compose.yml`:

```yaml
services:
  bydlogger:
    build: .
    container_name: bydlogger
    restart: unless-stopped
    volumes:
      - ./data:/data
```

Beim ersten Start erzeugt das Tool eine Default-`bydlogc.cfg` im Arbeitsverzeichnis (die Standard-Batterie-IP 192.168.16.254 passt bereits).
Danach den Container stoppen und in der cfg mindestens setzen (die cfg nur bei gestopptem Tool ändern - es überschreibt sie beim Beenden):

```ini
[BATTERY]
IPADR=192.168.16.254
READINTV=60
INTVinSECONDS=1

[MQTT]
enable=1
BrokerIP=DEIN_MQTT_BROKER
BrokerUser=DEIN_MQTT_USER
BrokerPass=DEIN_MQTT_PASSWORT
Port=1883
ExportModuleData=1
Root=Battery
```

Ein Abfrageintervall von 60 s ist bewusst moderat gewählt, um die BMU zu schonen.

**Ein-Verbindungs-Limit:** Die BMU verträgt genau einen Client.
Be-Connect-App oder BYD-Logger-GUI nie parallel zum Container laufen lassen; für App-Nutzung den Container vorher stoppen.

## MQTT-Topics und HA-Package

Das Tool publisht unter `Battery/BYD/BMS_1/...`: SOC, SOH, Min/Max/AvCellVolt, Power, Status1/2, Balancing, BalanceData, ChargedkWh/DischargedkWh sowie je Modul MinCellVolt/MaxCellVolt/ModuleVolt/MinTemp/MaxTemp.
`packages/byd_bmu.yaml` mappt diese Topics auf `sensor.byd_*`-Entities (`expire_after: 300` als Ausfall-Erkennung) und ergänzt zwei abgeleitete Sensoren:

- `sensor.byd_zellspreizung` (mV, max - min über den ganzen Turm),
- `sensor.byd_zellspreizung_ruhe` (aktualisiert nur bei |Leistung| < 300 W - unter Last ist die Spreizung durch Innenwiderstands-Unterschiede nicht vergleichbar; erst dieser Wert taugt für Wochen-Trends),
- `sensor.byd_temperatur_spreizung` (K, über die Module).

Package nach `packages/` kopieren (Packages-Include vorausgesetzt, siehe README) und HA neu starten.
Hinweis für Setups mit bestehendem top-level `mqtt: !include`: siehe Einbau-Hinweis im Package-Kopf.

## Interpretations-Hinweise (LFP)

- Die Zellspreizung ist nur im Ruhezustand und bei hohem SoC aussagekräftig; im LFP-Flachplateau (ca. 30-70 % SoC) sagt sie wenig über echte Ladungs-Imbalance.
- SoH-Änderungen von Tag zu Tag sind Rauschen - nur der Langzeittrend zählt.
- Das Verhältnis der kWh-Lebenszähler ist KEIN Wirkungsgrad (Standby- und Wandlungsverluste stecken mit drin).
- Sinnvolle Alarm-Startwerte (keine Herstellerangaben): Zellspannung >3,55 V bzw. <2,90 V, Zelltemperatur >45 °C, Modul-Temperaturdifferenz >10 K, Ruhe-Spreizung dauerhaft >50 mV.
  Die BMS-Schutzgrenzen (2,75/3,65 V) sind Notgrenzen, keine Automationsziele.
