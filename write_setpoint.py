"""Validate, write and read back whitelisted Gree operating settings."""
import contextlib
import sys

ALLOWED = {'power_switch': (42, 85, 170),
           'operating_mode': (2, 1, 5),
           'room_heating_setpoint': (12, 18, 30),
           'dhw_tank_setpoint': (13, 40, 60),
           'heating_water_setpoint': (10, 20, 60),
           'temperature_control_mode': (50, 0, 1),
           'room_temperature_differential': (32, 1, 5),
           'dhw_temperature_differential': (31, 1, 25),
           'heating_temperature_differential': (30, 1, 10),
           'standby_water_pump_control_mode': (83, 0, 2),
           'standby_water_pump_speed': (84, 6, 10),
           'standby_water_pump_interval': (85, 5, 120),
           'standby_water_pump_duration': (86, 1, 10)}
CHOICES = {'power_switch': {85, 170},
           'operating_mode': {1, 2, 3, 4, 5},
           'temperature_control_mode': {0, 1}}


def execute(key, value, cfg, rows, lock, opener, core, converter):
    if key not in ALLOWED:
        raise ValueError('Setpoint not allowed')
    address, low, high = ALLOWED[key]
    if (isinstance(value, bool) or not isinstance(value, int) or
            not low <= value <= high or
            (key in CHOICES and value not in CHOICES[key])):
        raise ValueError('Expected an allowed integer value')
    row = next(r for r in rows if r['key'] == key)
    if (row['function'], row['register'], row['write_function'], row['access'],
        row['scale'], row['offset']) != (3, address, 16, 'RW', 1, 0):
        raise ValueError('Unexpected CSV mapping; write blocked')
    if key == 'operating_mode' and (
            row['guard_function'], row['guard_register'], row['guard_value']) != (3, 42, 85):
        raise ValueError('Unexpected mode guard; write blocked')
    raw = converter(value, row)
    with lock(cfg['state_dir']) as acquired:
        if not acquired:
            return {'status': 'busy', 'key': key}, 75
        core.TIMEOUT_SECONDI = min(3.0, cfg['modbus']['timeout_seconds'])
        device = None
        attempted = False
        try:
            with contextlib.redirect_stdout(sys.stderr):
                device = opener(cfg['connection'], core)
                if key == 'operating_mode':
                    power = device.read_registers(42, 1, functioncode=3)[0]
                    if power != 85:
                        return {'status': 'failed', 'key': key,
                                'message': 'Turn the heat pump Off before changing mode.'}, 2
                before = device.read_registers(address, 1, functioncode=3)[0]
                if before != raw:
                    attempted = True
                    device.write_registers(address, [raw])
                actual = device.read_registers(address, 1, functioncode=3)[0]
            if actual != raw:
                return {'status': 'write_unconfirmed', 'key': key,
                        'requested': value, 'readback': actual,
                        'message': 'Readback differs from requested value. No retry.'}, 2
            return {'status': 'verified', 'key': key, 'value': actual,
                    'changed': before != raw}, 0
        except Exception as error:
            return {'status': 'write_unconfirmed' if attempted else 'failed',
                    'key': key, 'message': str(error), 'requested': value}, 2
        finally:
            if device is not None:
                device.serial.close()
