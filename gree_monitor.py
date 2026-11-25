#!/usr/bin/env python3
"""Serve the Gree dashboard and coordinate Modbus, MQTT and InfluxDB."""
import argparse
from collections import deque
from contextlib import nullcontext
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import signal
from threading import Condition, Thread
import time
from queue import Full, Queue
from uuid import uuid4

import collector
import influx_output
import leggi_gree as core
from gree_transport import open_device
from register_schema import raw_da_valore
from write_setpoint import ALLOWED, CHOICES, execute

BASE = Path(__file__).resolve().parent


def now_ms():
    return time.time_ns() // 1_000_000


class Monitor:
    def __init__(self, config_path, preview):
        self.cfg = collector.load_config(config_path)
        self.rows = collector.load_rows(self.cfg)
        self.write_rows = core.carica_csv(self.cfg['csv'])
        self.period = collector.positive(self.cfg.get('poll_seconds', 60), 'poll_seconds')
        self.preview = preview
        self.condition = Condition()
        self.jobs = deque()
        self.sequence = 0
        self.instance = uuid4().hex
        self.snapshot = self.bus = self.active = self.last_write = self.last_scan = None
        self.verified = {}
        self.stopping = False
        self.outputs = {}
        self.output_status = {}
        self.validate_writer()
        if not preview:
            self.configure_outputs(config_path)

    def validate_writer(self):
        for key, (_, low, _) in ALLOWED.items():
            # Exercise the existing mapping and guard validation without opening Modbus.
            execute(key, low, self.cfg, self.write_rows, lambda _: nullcontext(False),
                    open_device, core, raw_da_valore)

    def configure_outputs(self, path):
        cfg = self.cfg
        if cfg['mqtt'].get('enabled', True):
            password = collector.read_password(cfg)
            self.outputs['mqtt'] = lambda packet: collector.publish_snapshot(cfg, packet, password)
        influx = cfg['influx']
        if influx['enabled']:
            credentials = None
            if influx.get('credentials_file'):
                secret = collector.resolved(path.resolve().parent, influx['credentials_file'])
                if secret.stat().st_mode & 0o077:
                    raise ValueError('InfluxDB credentials require permissions 0600')
                credentials = json.loads(secret.read_text())
                for key in ('username', 'password'):
                    if not isinstance(credentials[key], str) or not credentials[key]:
                        raise ValueError(f'Invalid InfluxDB {key}')
            self.outputs['influx'] = lambda packet: influx_output.publish(influx, packet, credentials)

    def paused(self):
        return self.preview or (self.cfg['state_dir'] / 'polling.disabled').exists()

    def state(self):
        with self.condition:
            now = now_ms()
            return deepcopy(dict(
                now=now, instance=self.instance, snapshot=self.snapshot,
                stale=not self.snapshot or now-self.snapshot['completed_at_ms'] > max(120, 2*self.period)*1000,
                bus=self.bus, polling_enabled=not self.paused(),
                coordinator_ready=not self.preview, writer_ready=not self.preview,
                queue=list(self.jobs), active=self.active, last_write=self.last_write,
                last_scan=self.last_scan, verified_values=self.verified,
                outputs=self.output_status,
            ))

    def submit(self, payload):
        key, value = payload.get('key'), payload.get('value')
        if not isinstance(key, str) or key not in ALLOWED:
            return 400, {'message': 'Setpoint not allowed'}
        _, low, high = ALLOWED[key]
        if type(value) is not int or not low <= value <= high or (
            key in CHOICES and value not in CHOICES[key]
        ):
            return 400, {'message': 'Choose an allowed integer value'}
        with self.condition:
            if self.preview:
                return 503, {'message': 'Writes are paused in preview mode'}
            if len(self.jobs) >= 4 or any(job['key'] == key for job in self.jobs) or (
                self.active and self.active['key'] == key
            ):
                return 409, {'message': 'Queue full or setting already pending'}
            if key == 'operating_mode':
                state = self.snapshot
                power = state['packet']['values'].get('power_switch') if state else None
                readback = self.verified.get('power_switch')
                if readback and (not state or readback['verified_at'] > state['completed_at_ms']):
                    power = readback['value']
                if power != 85:
                    return 409, {'message': 'Turn the heat pump Off before changing mode'}
            self.sequence += 1
            timestamp = now_ms()
            job = dict(id=f'{timestamp}-{self.sequence}', key=key, value=value, queued_at=timestamp)
            self.jobs.append(job)
            self.condition.notify()
            return 202, {'id': job['id'], 'message': 'Queued for writing'}

    def write(self, job):
        try:
            result, _ = execute(job['key'], job['value'], self.cfg, self.write_rows,
                                collector.run_lock, open_device, core, raw_da_valore)
        except Exception:
            logging.exception('Gree writer failed')
            result = {'status': 'write_unconfirmed',
                      'message': 'Write result unavailable. Check the next scan.'}
        with self.condition:
            if result['status'] == 'busy':
                result['message'] = 'Modbus is busy. Try again after the current operation.'
            finished = now_ms()
            self.last_write = {**job, **result, 'finished_at': finished}
            if result['status'] == 'verified':
                self.verified[job['key']] = {'value': result['value'], 'verified_at': finished}
            self.active = self.bus = None

    def scan(self, queues):
        try:
            with collector.run_lock(self.cfg['state_dir']) as acquired:
                if not acquired or self.paused():
                    return
                packet = collector.acquire(self.cfg, self.rows)
            finished = now_ms()
            with self.condition:
                self.snapshot = dict(packet=packet, completed_at_ms=finished,
                                     received_at_ms=finished, retained=False)
                self.last_scan = dict(finished_at=finished, failed=packet['failed'])
            for name, pending in queues.items():
                try:
                    pending.put_nowait(packet)
                except Full:
                    logging.error('%s output is still busy, snapshot skipped', name)
        except Exception:
            logging.exception('Gree scan failed')
        finally:
            with self.condition:
                self.bus = None

    def deliver(self, name, publish, pending):
        while True:
            packet = pending.get()
            try:
                publish(packet)
                status = 'ok'
            except Exception:
                logging.error('%s delivery failed. Check connection and credentials.', name)
                status = 'error'
            with self.condition:
                self.output_status[name] = dict(status=status, updated_at=now_ms())

    def run(self):
        queues = {}
        for name, publish in self.outputs.items():
            pending = queues[name] = Queue(maxsize=1)
            Thread(target=self.deliver, args=(name, publish, pending), daemon=True).start()
        next_scan = time.monotonic()
        while True:
            with self.condition:
                if self.stopping:
                    return
                job = self.jobs.popleft() if self.jobs else None
                if job:
                    self.active = job
                    self.bus = {'kind': 'write', 'job_id': job['id']}
                elif not self.paused() and time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + self.period
                    self.bus = {'kind': 'scan'}
                else:
                    self.condition.wait(1)
                    continue
            if job:
                self.write(job)
            else:
                self.scan(queues)


class Handler(BaseHTTPRequestHandler):
    def send_data(self, code, data, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def reply(self, code, payload):
        self.send_data(code, json.dumps(payload, allow_nan=False).encode(), 'application/json')

    def do_GET(self):
        if self.path in ('/', '/gree'):
            self.send_data(200, self.server.page, 'text/html; charset=utf-8')
        elif self.path == '/gree/api/state':
            self.reply(200, self.server.monitor.state())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != '/gree/api/write':
            self.send_error(404)
            return
        host = self.headers.get('Host')
        if (not host or self.headers.get('Origin') not in (f'http://{host}', f'https://{host}')
            or self.headers.get('X-Gree-Dashboard') != '1'
            or self.headers.get_content_type() != 'application/json'):
            self.reply(403, {'message': 'Same-origin dashboard requests only'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError
            self.connection.settimeout(5)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, OSError):
            self.reply(400, {'message': 'Invalid JSON request'})
            return
        self.reply(*self.server.monitor.submit(payload))

    def log_message(self, format, *args):
        if self.path != '/gree/api/state':
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=BASE / 'config.json')
    parser.add_argument('--check', action='store_true', help='Validate local files without contacting equipment')
    parser.add_argument('--preview', action='store_true', help='Serve the page without reads, writes, or outputs')
    args = parser.parse_args()
    monitor = Monitor(args.config, args.preview)
    web = monitor.cfg['web']
    if args.check:
        print(f'OK: {len(monitor.rows)} registers, {len(ALLOWED)} settings, outputs: {", ".join(monitor.outputs) or "disabled"}')
        return
    with ThreadingHTTPServer((web['host'], int(web['port'])), Handler) as server:
        server.page = (BASE / 'dashboard.html').read_bytes()
        server.monitor = monitor
        worker = Thread(target=monitor.run, daemon=True)
        worker.start()

        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            with monitor.condition:
                monitor.stopping = True
                monitor.condition.notify()
            worker.join()


if __name__ == '__main__':
    main()
