# Gree Monitor

Part of a home monitoring setup built around three independent Python programs. Deye Monitor reads a solar inverter, Gree Monitor reads and controls a heat pump, and MQTT Monitor collects readings published by devices such as Shelly. Each has a local web dashboard and its own Python environment.

Companion projects: [Deye Monitor](https://github.com/marcobrianza/deye_monitor) · [MQTT Monitor](https://github.com/marcobrianza/mqtt_monitor).

## Host and dependencies

- Python 3.10+ and the packages listed in `requirements.txt`. Run on macOS or Linux for local use and testing. On Raspberry Pi with a 64-bit Debian-based OS, the included systemd unit supports unattended operation.
- An MQTT broker such as Mosquitto 2.x, normally on TCP 1883. The broker is required for MQTT Monitor, and is optional for Deye and Gree when MQTT publication is disabled.
- InfluxDB **1.x** on TCP 8086 for historical storage. The examples target its v1 HTTP API and assume the configured database already exists. Switching off InfluxDB logging leaves the live dashboard available. Grafana is optional and can query InfluxDB using InfluxQL.
- Example configurations preserve private LAN addresses from the original setup. Set your Modbus, MQTT and InfluxDB endpoints before starting on a different network. Actual credentials belong in local files ignored by Git.

## How it works

The Python process polls a Gree heat pump through a Modbus TCP gateway or an RS-485 adapter. The CSV register map and JSON profiles define what to read. It serves the dashboard at `/gree`, queues supported setpoint changes, and verifies writes by reading the setting back. It can record telemetry directly in InfluxDB and publish the same telemetry to MQTT. The dashboard has no authentication. Only expose the control interface on a trusted LAN or private VPN.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.default.json config.json
.venv/bin/python gree_monitor.py --check
.venv/bin/python gree_monitor.py --preview
```

Open http://127.0.0.1:8767/gree. Preview mode serves the interface without accessing the heat pump or writing data. For live operation, configure the gateway, disable the preview flag, and stop any previous Modbus poller. Set `mqtt.enabled` and `influx.enabled` independently. To enable MQTT, create `secrets/mqtt.json` with a JSON object containing a `password` string, set the file mode to 0600, and set the MQTT username in `config.json`. For authenticated InfluxDB, set `influx.credentials_file` to a private JSON file with `username` and `password` strings.

## Raspberry Pi

On Raspberry Pi OS or Debian, install `python3-venv` if needed. After configuring `config.json` and any local secrets, run:

```sh
sudo bash install-raspberry.sh
sudo systemctl status gree_monitor.service
```

The installer creates `gree-monitor`, installs in `/opt/gree_monitor`, and uses a dedicated virtual environment. On an existing deployment the installed configuration and credentials take precedence. Keep the previous collector stopped when starting this service against the same device.
