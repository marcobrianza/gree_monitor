"""Write the same InfluxDB 1.x series as the Gree Node-RED adapter."""
from base64 import b64encode
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def tag(value):
    return value.replace('\\', '\\\\').replace(' ', '\\ ').replace(',', '\\,').replace('=', '\\=')


def line_protocol(packet):
    tags = f",device={tag(packet['device'])},site={tag(packet['site'])}"
    health = {
        'selected_points': packet['selected'],
        'successful_points': packet['succeeded'],
        'failed_points': packet['failed'],
        'scan_duration_seconds': packet['duration_seconds'],
    }
    lines = []
    for measurement, fields in [('gree', packet['values']), ('gree_acquisition', health)]:
        if fields:
            values = ','.join(f'{key}={float(value)!r}' for key, value in fields.items())
            lines.append(f'{measurement}{tags} {values}')
    return '\n'.join(lines).encode()


def publish(settings, packet, credentials=None):
    query = {'db': settings['database']}
    if settings.get('retention_policy'):
        query['rp'] = settings['retention_policy']
    headers = {'Content-Type': 'text/plain; charset=utf-8'}
    if credentials:
        token = f"{credentials['username']}:{credentials['password']}".encode()
        headers['Authorization'] = 'Basic ' + b64encode(token).decode()
    request = Request(f"{settings['url'].rstrip('/')}/write?{urlencode(query)}",
                      data=line_protocol(packet), headers=headers)
    with urlopen(request, timeout=5) as response:
        if response.status != 204:
            raise OSError(f'Unexpected InfluxDB HTTP status {response.status}')
