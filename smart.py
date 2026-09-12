"""Optional read-only SATA SMART snapshots, cached for 60 seconds.

Only the configured device is queried. SMART raw attributes are reported as
decoded by smartctl; unknown vendor units are never converted to lifetime/TBW.
"""
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def device_statistics(data):
    """Decode only valid, non-normalized General Statistics (ATA log 04h/01h)."""
    fields = {8: 'power_cycles', 16: 'power_on_hours', 24: 'sectors_written',
              32: 'write_commands', 40: 'sectors_read', 48: 'read_commands'}
    out = {'source': 'ATA Device Statistics 0x04 / page 0x01'}
    for page in data.get('ata_device_statistics', {}).get('pages', []):
        if page.get('number') != 1:
            continue
        for item in page.get('table', []):
            key = fields.get(item.get('offset'))
            flags = item.get('flags', {})
            value = number(item.get('value'))
            if key and flags.get('valid') is True and flags.get('normalized') is False and value is not None and value >= 0:
                out[key] = value
    sector = number(data.get('logical_block_size'))
    if sector is not None and sector > 0:
        out['logical_sector_bytes'] = sector
        for side in ('read', 'written'):
            if 'sectors_' + side in out:
                out['bytes_' + side] = out['sectors_' + side] * sector
    return out


def parse(data, device, exit_code=0):
    if not isinstance(data, dict) or exit_code < 0 or exit_code & 7:
        raise ValueError('SMART command could not read complete device data')
    if data.get('device', {}).get('name') != device:
        raise ValueError('SMART device identity mismatch')
    attrs = {a['id']: a for a in data.get('ata_smart_attributes', {}).get('table', [])}
    counters = {str(i): number(attrs.get(i, {}).get('raw', {}).get('value'))
                for i in (5, 197, 198, 199)}
    passed = data.get('smart_status', {}).get('passed')
    if type(passed) is not bool:
        passed = None
    temperature = number(data.get('temperature', {}).get('current'))
    if temperature is not None and not -50 < temperature < 150:
        temperature = None
    # A passing overall status does not erase nonzero error indicators.
    warning = bool(exit_code & 0xf0) or any(v is not None and v > 0 for v in counters.values())
    state = 'failed' if passed is False or exit_code & 8 else 'warning' if warning else 'passed' if passed else 'unknown'
    return {
        'device': device, 'model': data.get('model_name', device),
        'firmware': data.get('firmware_version'),
        'capacity_bytes': number(data.get('user_capacity', {}).get('bytes')),
        'temperature': temperature, 'passed': passed, 'state': state,
        'power_on_hours': number(data.get('power_on_time', {}).get('hours')),
        'power_cycles': number(data.get('power_cycle_count')),
        'counters': counters,
        'database_matched': data.get('in_smartctl_database') is True,
        'statistics': device_statistics(data),
        'temperature_status': 'reported',
    }


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.device = None
        self.next_read = 0
        self.last = None
        self.error = None

    def snapshot(self):
        device = os.environ.get('MONITOR_SMART_DEVICE', '')
        if not device:
            return {'enabled': False, 'disks': []}
        with self.lock:
            if device != self.device:
                self.device, self.next_read, self.last, self.error = device, 0, None, None
            if time.monotonic() >= self.next_read:
                try:
                    if not re.fullmatch(r'/dev/sd[a-z]+', device):
                        raise ValueError('Invalid configured SATA device')
                    result = subprocess.run(
                        ['/usr/bin/sudo', '-n', '/usr/sbin/smartctl', '-x', '-j', device],
                        capture_output=True, text=True, timeout=4, check=False)
                    disk = parse(json.loads(result.stdout), device, result.returncode)
                    if os.environ.get('MONITOR_SMART_TEMPERATURE_UNVERIFIED') == '1':
                        disk['temperature_status'] = 'unverified'
                    self.last = {'disks': [disk], 'updated_at': datetime.now(timezone.utc).isoformat()}
                    self.error = None
                except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                    self.error = 'SMART 读取失败，未更新；请检查设备连接和采集权限'
                self.next_read = time.monotonic() + 60
            return {'enabled': True, 'disks': [], **(self.last or {}),
                    'stale': self.error is not None, 'error': self.error, 'interval_seconds': 60}


def temperature_sensors(snapshot):
    if snapshot.get('stale'):
        return []  # Do not turn a failed read into a new temperature observation.
    return [dict(id=d['device'].split('/')[-1] + '.smart_temperature',
                 device_id=d['device'].split('/')[-1], device=d['device'].split('/')[-1] + ' · ' + d['model'],
                 group='sata', label='SMART 温度', value=d['temperature'],
                 warn=None, crit=None, assumed=False)
            for d in snapshot.get('disks', []) if d.get('temperature') is not None and d.get('temperature_status') != 'unverified']


collector = Collector()
