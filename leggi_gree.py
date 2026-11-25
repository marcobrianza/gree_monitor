#!/usr/bin/env python3
"""Gree Modbus RTU/TCP monitor with console output and a local web interface. Python 3.9+."""
from pathlib import Path
import argparse
import csv
import math
import sys
import time

# DEFAULT SETTINGS — serial port from the previously tested XY-MD tool.
PORTA_SERIALE = '/dev/tty.usbserial-BH00MM4K'
INDIRIZZO_MODBUS = 1  # Default controller address in the Versati V1.5 protocol.
BAUD_RATE = 9600
PARITA = 'N'
STOP_BITS = 1
TIMEOUT_SECONDI = 1.5
INTERVALLO_SECONDI = 60  # Delay after a complete scan.
FILE_CSV = Path(__file__).with_name('registri_gree.csv')
PAUSA_RICHIESTE = 0.10
PORTA_WEB = 8081
MODALITA = 'tcp'  # 'serial' or 'tcp'
IP_GATEWAY = '192.168.13.32'
PORTA_GATEWAY = 502
REFRESH_AUTOMATICO = False  # Wait for a manual read at startup.

from register_schema import number as numero, load_registers, converti, description

def carica_csv(path):
    rows, errors = load_registers(path)
    if errors:
        raise ValueError('; '.join(errors))
    for row in rows:
        row['category']=row.get('category') or ('telemetry' if row['access']=='RO' else 'runtime')
        row['domain']=row.get('domain') or 'General'
        if row['category'] not in ('installation','runtime','telemetry','legacy'):
            raise ValueError(f"Invalid CSV category: {row['category']}")
    rows = [row for row in rows if row['enabled']]
    if not rows:
        raise ValueError('No enabled CSV rows')
    return rows

def blocchi(rows):
    """Group documented consecutive addresses without crossing gaps."""
    groups = {}
    for r in rows:
        if r['access']=='WO':
            continue
        groups.setdefault(r['function'], set()).add(r['register'])
    for fc, addresses in sorted(groups.items(), reverse=True):
        block = []
        for a in sorted(addresses):
            if block and (a != block[-1] + 1 or len(block) >= 60):
                yield fc, block
                block = []
            block.append(a)
        if block:
            yield fc, block

def leggi(device, fc, start, count):
    time.sleep(PAUSA_RICHIESTE)
    if fc in (1,2):
        return device.read_bits(start, count, functioncode=fc)
    return device.read_registers(start, count, functioncode=fc)

def scansione(device, rows, mm, on_result=None, on_request=None):
    values, errors = {}, {}
    def result(fc, address, raw=None, error=''):
        if error:
            errors[fc, address] = error
        else:
            values[fc, address] = raw
        if on_result:
            on_result(fc, address, raw, error)
    def request(fc, start, count):
        message = f'FC{fc:02d} registers {start}..{start+count-1} ({count} points)'
        print(f'  TX {message} — waiting for response...', flush=True)
        if on_request:
            on_request(message)
        return leggi(device, fc, start, count)
    for fc, addresses in blocchi(rows):
        try:
            data = request(fc, addresses[0], len(addresses))
            if len(data) != len(addresses):
                raise OSError('Incomplete response')
            for a, raw in zip(addresses, data):
                result(fc, a, raw)
        except mm.SlaveReportedException as e:
            print(f'  RX Modbus exception: {e}; trying individual points.', flush=True)
            for a in addresses:
                try:
                    result(fc, a, request(fc, a, 1)[0])
                except (mm.ModbusException, OSError) as e:
                    result(fc, a, error=str(e))
        except (mm.ModbusException, OSError) as e:
            for a in addresses:
                result(fc, a, error=str(e))
    return values, errors

def stampa(rows, values, errors):
    print('\n' + time.strftime('%Y-%m-%d %H:%M:%S'))
    for r in rows:
        if r['access']=='WO':
            print(f"FC{r['function']:02d} {r['register']:3d} | {r['label']} | Write only (WO)")
            continue
        key = r['function'], r['register']
        prefix = f"FC{r['function']:02d} {r['register']:3d} | {r['label']:<48}"
        if key not in values:
            print(f"{prefix} | ERROR: {errors.get(key, 'no data')}")
            continue
        raw = values[key]
        value = converti(raw, r)
        desc = description(value,r)
        warning = ''
        if r['unit'] == '°C' and not -50 <= value <= 150:
            warning = ' [out of range: check map/sensor]'
        print(f"{prefix} | {value:g} {r['unit']} {desc} | raw={raw} (0x{raw:04X}){warning}")
    print(f'Read {len(values)} points; {len(errors)} errors.', flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=PORTA_SERIALE)
    parser.add_argument('--address', type=int, default=INDIRIZZO_MODBUS)
    parser.add_argument('--baud', type=int, default=BAUD_RATE)
    parser.add_argument('--csv', type=Path, default=FILE_CSV)
    parser.add_argument('--interval', type=float, default=INTERVALLO_SECONDI)
    parser.add_argument('--once', action='store_true', help='one scan only')
    parser.add_argument('--check', action='store_true', help='validate CSV without opening a connection')
    parser.add_argument('--console', action='store_true', help='console only, no web server')
    parser.add_argument('--web-port', type=int, default=PORTA_WEB)
    parser.add_argument('--auto', action=argparse.BooleanOptionalAction, default=REFRESH_AUTOMATICO)
    parser.add_argument('--mode', choices=('serial','tcp'), default=MODALITA)
    parser.add_argument('--host', default=IP_GATEWAY)
    parser.add_argument('--tcp-port', type=int, default=PORTA_GATEWAY)
    args = parser.parse_args()
    if not 1 <= args.tcp_port <= 65535:
        parser.error('TCP port must be between 1 and 65535')
    if not 1 <= args.web_port <= 65535:
        parser.error('Web port must be between 1 and 65535')
    if not 1 <= args.address <= 247 or args.address == 126:
        parser.error('Allowed addresses: 1..247, excluding 126 in the Gree protocol')
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error('Interval must be finite and >= 0')
    rows = carica_csv(args.csv)
    if args.check:
        print(f'Valid CSV: {len(rows)} enabled points, {len(list(blocchi(rows)))} blocks.')
        return 0
    if not args.console and not args.once:
        import gree_web
        gree_web.serve(sys.modules[__name__], args, rows)
        return 0
    try:
        import minimalmodbus as mm
        import serial
    except ImportError:
        print('Install dependencies: python3 -m pip install minimalmodbus pyserial', file=sys.stderr)
        return 1
    print('Gree R290 V2.5 map, module 1; legacy V1.5 points retained. Verify compatibility on the unit.')
    print(f'{args.port} | {args.baud} 8{PARITA}{STOP_BITS} | slave {args.address} | read only')
    print('INDIRIZZO_MODBUS/--address overrides the CSV address column.')
    from gree_transport import open_device, initial_config
    device = open_device(initial_config(args),sys.modules[__name__])
    try:
        while True:
            values, errors = scansione(device, rows, mm)
            stampa(rows, values, errors)
            if args.once:
                return 0 if values and not errors else 2
            time.sleep(args.interval)
    finally:
        device.serial.close()

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nStopped.')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
