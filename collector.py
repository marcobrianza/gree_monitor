#!/usr/bin/env python3
"""Read Gree Modbus once, publish one MQTT snapshot, and exit. macOS/Linux, Python 3.9+."""
import argparse
import contextlib
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import math
from pathlib import Path
import re
import signal
import sys
import time
import uuid

BASE = Path(__file__).resolve().parent


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def resolved(base, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def positive(value, name, upper=86400):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= upper:
        raise ValueError(f'{name} must be finite and in (0, {upper}]')
    return value


def load_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding='utf-8'))
    base = path.parent
    cfg['csv'] = resolved(base, cfg['csv'])
    cfg['profiles'] = resolved(base, cfg.get('profiles', 'profiles.json'))
    cfg.setdefault('selection', {'profile': 'monitoring', 'keys': [], 'exclude': []})
    cfg['state_dir'] = resolved(base, cfg['state_dir'])
    cfg['mqtt']['password_file'] = resolved(base, cfg['mqtt']['password_file'])
    cfg['max_scan_seconds'] = positive(cfg['max_scan_seconds'], 'max_scan_seconds')
    cfg['modbus']['timeout_seconds'] = positive(cfg['modbus']['timeout_seconds'], 'Modbus timeout', 60)
    pause = float(cfg['modbus']['request_pause_seconds'])
    if not math.isfinite(pause) or not 0 <= pause <= 10:
        raise ValueError('request_pause_seconds must be in [0, 10]')
    cfg['modbus']['request_pause_seconds'] = pause
    from gree_transport import validate_config
    cfg['connection'] = validate_config(cfg['modbus'])
    mqtt = cfg['mqtt']
    mqtt.setdefault('retain', True)
    if not isinstance(mqtt['retain'], bool):
        raise ValueError('mqtt.retain must be true or false')
    mqtt['port'] = int(mqtt['port'])
    if not mqtt['host'] or not 1 <= mqtt['port'] <= 65535 or not mqtt['username']:
        raise ValueError('MQTT host, port and username are required')
    if not mqtt['topic'] or any(c in mqtt['topic'] for c in ('+', '#', '\x00')):
        raise ValueError('MQTT topic must be a concrete publish topic')
    mqtt['timeout_seconds'] = positive(mqtt['timeout_seconds'], 'MQTT timeout', 60)
    allowed = {'installation', 'runtime', 'telemetry', 'legacy'}
    if not isinstance(cfg['categories'], list) or not cfg['categories'] or not set(cfg['categories']) <= allowed:
        raise ValueError('categories must be a non-empty list of known CSV categories')
    for key in ('site', 'device'):
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ValueError(f'{key} must be a non-empty string')
    return cfg


def load_rows(cfg):
    import leggi_gree as core
    rows = [r for r in core.carica_csv(cfg['csv'])
            if r['access'] != 'WO']
    keys = set()
    for r in rows:
        key = r.get('key')
        if not key or re.fullmatch(r'fc\d+_\d+', key):
            raise ValueError('Each readable CSV point requires a descriptive key; use the supplied v2 CSV')
        if not re.fullmatch(r'[a-z][a-z0-9_]*', key) or key in keys:
            raise ValueError('CSV keys must be unique lowercase identifiers')
        r['key'] = key
        keys.add(key)
    selection = cfg['selection']
    if not isinstance(selection, dict):
        raise ValueError('selection must be an object')
    requested = selection.get('keys', [])
    excluded = selection.get('exclude', [])
    for name, items in (('keys', requested), ('exclude', excluded)):
        if not isinstance(items, list) or not all(isinstance(k, str) for k in items):
            raise ValueError(f'selection.{name} must be a list of field names')
        unknown = set(items) - keys
        if unknown:
            raise ValueError(f'Unknown/disabled point in {name}: ' + ', '.join(sorted(unknown)))
    if requested:
        selected_keys = set(requested)
    else:
        profile = selection.get('profile', 'monitoring')
        if profile == 'all':
            selected_keys = keys
        elif profile == 'operational':
            selected_keys = {r['key'] for r in rows if r['category'] in ('runtime', 'telemetry')}
        elif profile == 'categories':
            selected_keys = {r['key'] for r in rows if r['category'] in cfg['categories']}
        else:
            profiles = json.loads(cfg['profiles'].read_text(encoding='utf-8'))
            if profile not in profiles:
                raise ValueError(f'Unknown read profile: {profile}')
            profile_keys = profiles[profile]['keys']
            if not isinstance(profile_keys, list) or not all(isinstance(k, str) for k in profile_keys):
                raise ValueError('Profile keys must be a list of field names')
            selected_keys = set(profile_keys)
            unknown = selected_keys - keys
            if unknown:
                raise ValueError('Unknown/disabled point in profile: ' + ', '.join(sorted(unknown)))
    rows = [r for r in rows if r['key'] in selected_keys and r['key'] not in excluded]
    if not rows:
        raise ValueError('No readable points selected')
    return rows


@contextlib.contextmanager
def run_lock(state_dir):
    """Keep the lock file; the kernel releases flock on exit, including crashes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'collector.lock').open('a+') as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class ReadOnlyDevice:
    """Expose read methods only; stop starting requests after the scan deadline."""
    def __init__(self, device, deadline):
        self.device, self.deadline = device, deadline

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Scan deadline exceeded; point not read')

    def read_registers(self, *args, **kwargs):
        self.check()
        return self.device.read_registers(*args, **kwargs)

    def read_bits(self, *args, **kwargs):
        self.check()
        return self.device.read_bits(*args, **kwargs)


def scaled_value(raw, row):
    """Apply documented decimal scale exactly, then emit a JSON numeric value."""
    import leggi_gree as core
    interpreted = core.converti(raw, dict(row, scale=1, offset=0))
    value = Decimal(interpreted) * Decimal(str(row['scale'])) + Decimal(str(row['offset']))
    return float(value)


def acquire(cfg, rows):
    import minimalmodbus as mm
    import leggi_gree as core
    from gree_transport import open_device
    core.TIMEOUT_SECONDI = cfg['modbus']['timeout_seconds']
    core.PAUSA_RICHIESTE = cfg['modbus']['request_pause_seconds']
    start = time.monotonic()
    packet = dict(schema_version=2, device=cfg['device'], site=cfg['site'],
                  started_at=utc_now(), duration_seconds=0,
                  selected=len(rows), succeeded=0, failed=0, errors=[], values={})
    errors = {}
    by_address = {(r['function'], r['register']): r for r in rows}

    def record(fc, address, raw, error):
        row = by_address[fc, address]
        key = row['key']
        if error:
            errors[key] = error
            print(f"ERROR {key} {row['label']}: {error}", file=sys.stderr, flush=True)
        else:
            packet['values'][key] = scaled_value(raw, row)
            print(f"OK {key} {row['label']}: {packet['values'][key]:g} {row['unit']}",
                  file=sys.stderr, flush=True)

    device = None
    # The original reader logs to stdout. Keep stdout exclusively machine-readable here.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            device = open_device(cfg['connection'], core)
            core.scansione(ReadOnlyDevice(device, start + cfg['max_scan_seconds']), rows, mm,
                           on_result=record)
        except Exception as error:
            for row in rows:
                if row['key'] not in packet['values'] and row['key'] not in errors:
                    record(row['function'], row['register'], None, str(error))
        finally:
            if device is not None:
                device.serial.close()
    packet['duration_seconds'] = round(time.monotonic() - start, 3)
    packet['succeeded'] = len(packet['values'])
    packet['failed'] = len(errors)
    packet['errors'] = list(errors)
    if errors:
        packet['error_details'] = errors
    return packet


def read_password(cfg):
    path = cfg['mqtt']['password_file']
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('MQTT credentials missing or invalid; run setup_credentials.py') from None
    if not isinstance(data, dict) or not isinstance(data.get('password'), str) or not data['password']:
        raise ValueError('MQTT credentials must contain a non-empty password')
    if path.stat().st_mode & 0o077:
        raise ValueError('MQTT credentials must have permissions 0600')
    return data['password']


def publish_snapshot(cfg, packet, password):
    """QoS 1 confirms broker acceptance, not downstream InfluxDB persistence."""
    import paho.mqtt.client as mqtt
    settings = cfg['mqtt']
    timeout = settings['timeout_seconds']
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id='gree-' + uuid.uuid4().hex[:16],
                         protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.username_pw_set(settings['username'], password)
    client.connect_timeout = timeout
    outcome = {}

    def connected(client, userdata, flags, reason_code, properties):
        outcome['connected'] = not reason_code.is_failure

    client.on_connect = connected
    try:
        client.connect(settings['host'], settings['port'], keepalive=30)
        client.loop_start()
        deadline = time.monotonic() + timeout
        while 'connected' not in outcome and time.monotonic() < deadline:
            time.sleep(0.02)
        if not outcome.get('connected'):
            raise OSError('MQTT connection/authentication failed or timed out')
        info = client.publish(settings['topic'], json.dumps(packet, allow_nan=False, separators=(',', ':')),
                              qos=1, retain=settings.get('retain', True))
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise OSError('MQTT publish could not be queued')
        info.wait_for_publish(timeout=timeout)
        if not info.is_published():
            raise TimeoutError('MQTT publish acknowledgement timed out')
    finally:
        client.disconnect()
        client.loop_stop()


def summary(status, code, **extra):
    print(json.dumps(dict(status=status, exit_code=code, **extra), allow_nan=False), flush=True)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=BASE / 'config.json')
    parser.add_argument('--check', action='store_true', help='validate config/CSV; no device, broker or state changes')
    parser.add_argument('--stdout-only', action='store_true', help='read Modbus and output snapshot without MQTT')
    parser.add_argument('--mqtt-test', action='store_true', help='publish a test packet to topic/test; no Modbus')
    parser.add_argument('--profile', help='monitoring (default), extended, all, operational, categories, or a custom profile')
    parser.add_argument('--keys', action='append', help='read only these comma-separated field names; repeatable')
    parser.add_argument('--exclude', action='append', help='exclude comma-separated field names; repeatable')
    parser.add_argument('--list', action='store_true', help='list selected points without network or state changes')
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    selection = cfg['selection']
    if not isinstance(selection, dict):
        raise ValueError('selection must be an object')
    if args.profile:
        selection['profile'] = args.profile
        selection['keys'] = []
    def split_names(items):
        return [name.strip() for item in items for name in item.split(',') if name.strip()]
    if args.keys is not None:
        selection['keys'] = split_names(args.keys)
        if not selection['keys']:
            raise ValueError('--keys requires at least one field name')
    if args.exclude is not None:
        selection['exclude'] = selection.get('exclude', []) + split_names(args.exclude)
    rows = load_rows(cfg)
    if args.list:
        print(json.dumps([dict(key=r['key'], label=r['label'], function=r['function'],
                               register=r['register'], category=r['category']) for r in rows], indent=2))
        return 0
    if args.check:
        import leggi_gree as core
        return summary('configuration_ok', 0, selected=len(rows), requests=len(list(core.blocchi(rows))), device=cfg['device'],
                       mqtt_host=cfg['mqtt']['host'], topic=cfg['mqtt']['topic'])
    # The independent MQTT test is allowed while device polling is paused.
    if args.mqtt_test:
        password = read_password(cfg)
        cfg['mqtt']['topic'] += '/test'
        cfg['mqtt']['retain'] = False
        publish_snapshot(cfg, {'schema_version': 2, 'test': True, 'started_at': utc_now(),
                               'device': cfg['device']}, password)
        return summary('mqtt_test_published', 0, topic=cfg['mqtt']['topic'])
    with run_lock(cfg['state_dir']) as acquired:
        if not acquired:
            return summary('skipped_busy', 0)
        if (cfg['state_dir'] / 'polling.disabled').exists():
            return summary('skipped_disabled', 0)
        # Detect credential errors before contacting Modbus.
        password = None if args.stdout_only else read_password(cfg)
        packet = acquire(cfg, rows)
        if args.stdout_only:
            print(json.dumps(packet, allow_nan=False), flush=True)
        else:
            try:
                publish_snapshot(cfg, packet, password)
            except Exception:
                # Avoid including library exception text that might contain credentials.
                return summary('mqtt_publish_failed', 3, selected=packet['selected'],
                               succeeded=packet['succeeded'], failed=packet['failed'],
                               message='MQTT delivery not confirmed. Check broker, credentials and ACLs.')
        code = 0 if packet['failed'] == 0 else 2
        if args.stdout_only:
            return code
        return summary('published' if code == 0 else 'published_partial', code,
                       succeeded=packet['succeeded'], failed=packet['failed'],
                       selected=packet['selected'], duration_seconds=packet['duration_seconds'],
                       started_at=packet['started_at'])


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(summary('interrupted', 130))
    except Exception as error:
        # Operational exceptions outside MQTT use file/config/device messages only.
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        sys.exit(summary('failed', 1))
