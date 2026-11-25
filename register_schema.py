"""Register CSV standard v2, compatible with CPython and CircuitPython.
Zero-based protocol addresses; value = interpreted raw * scale + offset.
Writes require access=RW/WO and are always explicit.
"""
import json
import math

BASE_COLUMNS = 'enabled,address,function,register,label,type,scale,offset,unit'.split(',')
EXTRA_COLUMNS = 'access,min,max,step,choices,write_function,guard_function,guard_register,guard_value'.split(',')
COLUMNS = BASE_COLUMNS + EXTRA_COLUMNS


def number(value):
    value = str(value).strip()
    return int(value, 16) if value.lower().startswith('0x') else int(value)


def csv_records(text):
    # CSV supports commas, doubled quotes, CRLF and multiline fields.
    record, field, quoted, i = [], '', False, 0
    text = text.lstrip('\ufeff')
    while i < len(text):
        char = text[i]
        if char == '"':
            if quoted and i+1 < len(text) and text[i+1] == '"':
                field += '"'; i += 1
            else:
                quoted = not quoted
        elif char == ',' and not quoted:
            record.append(field.strip()); field = ''
        elif char in '\r\n' and not quoted:
            if char == '\r' and i+1 < len(text) and text[i+1] == '\n':
                i += 1
            record.append(field.strip()); field = ''
            if any(record) and not record[0].startswith('#'):
                yield record
            record = []
        else:
            field += char
        i += 1
    if quoted:
        raise ValueError('Unclosed CSV quotes')
    if field or record:
        record.append(field.strip())
        if any(record) and not record[0].startswith('#'):
            yield record


def normalize_row(fields):
    row = dict(fields)
    row['enabled'] = str(row['enabled']).strip().lower() not in ('0','false','no','')
    for key in ('address','function','register'):
        row[key] = number(row[key])
    for key in ('scale','offset'):
        row[key] = float(row[key])
        if not math.isfinite(row[key]):
            raise ValueError('Non-finite scale/offset')
    row['type'] = row['type'].lower()
    if row['type'] == 'bool':
        row['type'] = 'bit'
    if not 1 <= row['address'] <= 247 or not 0 <= row['register'] <= 65535:
        raise ValueError('Address 1..247; register 0..65535')
    if row['function'] not in (1,2,3,4):
        raise ValueError('Read function: 1/2 bit, 3/4 word')
    if row['type'] not in ('bit','int16','uint16'):
        raise ValueError('Type: bit/int16/uint16')
    if (row['function'] in (1,2)) != (row['type'] == 'bit'):
        raise ValueError('FC01/02 require bit; FC03/04 require int16/uint16')
    if row['type'] == 'bit' and (row['scale'] != 1 or row['offset'] != 0):
        raise ValueError('Bit: scale=1 and offset=0')
    row['access'] = (row.get('access') or 'RO').upper()
    if row['access'] not in ('RO','WO','RW'):
        raise ValueError('access: RO/WO/RW')
    for key in ('min','max','step'):
        text = row.get(key, '')
        row[key] = None if text in ('',None) else float(text)
        if row[key] is not None and not math.isfinite(row[key]):
            raise ValueError(key+' is not finite')
    if row['min'] is not None and row['max'] is not None and row['min'] > row['max']:
        raise ValueError('min > max')
    if row['step'] is not None and row['step'] <= 0:
        raise ValueError('step must be positive')
    choices = row.get('choices') or {}
    if isinstance(choices,str):
        choices = json.loads(choices)
    if not isinstance(choices,dict):
        raise ValueError('choices must be a JSON object')
    row['choices'] = {}
    for key, label in choices.items():
        value = float(key)
        if not math.isfinite(value):
            raise ValueError('Non-finite option')
        row['choices'][str(key)] = str(label)
    row['write_function'] = number(row['write_function']) if row.get('write_function') not in ('',None) else (15 if row['function']==1 else 16)
    if row['access'] != 'RO':
        if row['function'] not in (1,3) or row['scale'] == 0:
            raise ValueError('Writes require FC01 coils or FC03 words with a non-zero scale')
        if row['write_function'] not in ((5,15) if row['function']==1 else (6,16)):
            raise ValueError('Incompatible write function')
    guard = [row.get(k) for k in ('guard_function','guard_register','guard_value')]
    if any(v not in ('',None) for v in guard):
        if any(v in ('',None) for v in guard):
            raise ValueError('Incomplete write guard')
        for key in ('guard_function','guard_register','guard_value'):
            row[key] = number(row[key])
        if row['guard_function'] not in (1,2,3,4) or not 0 <= row['guard_register'] <= 65535 or not 0 <= row['guard_value'] <= 65535:
            raise ValueError('Invalid write guard')
    else:
        for key in ('guard_function','guard_register','guard_value'):
            row[key] = None
    return row


def load_registers(path):
    rows, errors, seen = [], [], set()
    try:
        with open(path,'r') as file:
            records = iter(csv_records(file.read()))
            try:
                header = next(records)
            except StopIteration:
                header = []
            if not set(BASE_COLUMNS).issubset(header) or len(set(header)) != len(header):
                raise ValueError('Missing or duplicate CSV headers')
            for line, values in enumerate(records,2):
                try:
                    if len(values) != len(header):
                        raise ValueError('Column count does not match the header')
                    row = normalize_row(dict(zip(header,values)))
                    key = (row['address'],row['function'],row['register'])
                    if key in seen:
                        raise ValueError('Duplicate point (address/function/register)')
                    seen.add(key); rows.append(row)
                except (ValueError,TypeError,KeyError) as error:
                    errors.append('CSV row %d: %s' % (line,error))
    except (OSError,ValueError) as error:
        errors.append(str(error))
    return rows, errors


def converti(raw,row):
    value = int(raw)
    if row['type'] == 'int16' and value & 0x8000:
        value -= 65536
    return value*row['scale'] + row['offset']


def description(value,row):
    for key,label in row['choices'].items():
        if float(key) == value:
            return label
    return ''


def policy(row):
    if row.get('access','RO') == 'RO':
        return None
    if row['choices']:
        return {'choices':row['choices']}
    if row['type']=='bit':
        return {'choices':{'0':'OFF','1':'ON'}}
    low,high = (-32768,32767) if row['type']=='int16' else (0,65535)
    edges = (low*row['scale']+row['offset'],high*row['scale']+row['offset'])
    return {'min':row['min'] if row['min'] is not None else min(edges),
            'max':row['max'] if row['max'] is not None else max(edges),
            'step':row['step'] or abs(row['scale'])}


def raw_da_valore(value,row):
    if policy(row) is None:
        raise ValueError('Read-only point (RO)')
    value = float(str(value).strip().replace(',','.'))
    if not math.isfinite(value) or not row['scale']:
        raise ValueError('Invalid value/scale')
    if row['choices'] and not any(float(key)==value for key in row['choices']):
        raise ValueError('Option not allowed')
    if row['min'] is not None and value < row['min'] or row['max'] is not None and value > row['max']:
        raise ValueError('Value outside the CSV limits')
    raw = (value-row['offset'])/row['scale']
    integer = round(raw)
    if abs(raw-integer) > 1e-7:
        raise ValueError('Value cannot be represented with the register scale')
    if row['step']:
        steps = (value-(row['min'] if row['min'] is not None else row['offset']))/row['step']
        if abs(steps-round(steps)) > 1e-7:
            raise ValueError('Value does not match the CSV step')
    low,high = (-32768,32767) if row['type']=='int16' else (0,65535)
    if row['type']=='bit':
        low,high = 0,1
    if not low <= integer <= high:
        raise ValueError('Value outside the register data type range')
    return integer & 65535
