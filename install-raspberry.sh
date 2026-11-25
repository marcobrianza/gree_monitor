#!/bin/sh
set -eu
cd "$(dirname "$0")"
target=/opt/gree_monitor
account=gree-monitor
service=gree_monitor.service
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
if [ ! -f "$target/config.json" ] && [ ! -f config.json ]; then
  echo 'Create config.json before installing.' >&2
  exit 1
fi
if ! id "$account" >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$account"
fi
install -d -m 0755 "$target"
python3 -m venv "$target/.venv"
"$target/.venv/bin/python" -m pip install -r requirements.txt
install -m 0644 collector.py gree_monitor.py gree_transport.py influx_output.py leggi_gree.py register_schema.py write_setpoint.py dashboard.html registri_gree.csv profiles.json requirements.txt "$target/"
if [ ! -f "$target/config.json" ]; then
  install -o root -g "$account" -m 0640 config.json "$target/config.json"
fi
install -d -o "$account" -g "$account" -m 0750 "$target/state"
if [ -d secrets ]; then
  install -d -o "$account" -g "$account" -m 0700 "$target/secrets"
  for secret in mqtt.json influx.json; do
    if [ -f "secrets/$secret" ] && [ ! -e "$target/secrets/$secret" ]; then
      install -o "$account" -g "$account" -m 0600 "secrets/$secret" "$target/secrets/$secret"
    fi
  done
fi
runuser -u "$account" -- "$target/.venv/bin/python" "$target/gree_monitor.py" --check
install -m 0644 "$service" "/etc/systemd/system/$service"
systemctl daemon-reload
systemctl enable "$service"
systemctl restart "$service"
systemctl is-active "$service"
