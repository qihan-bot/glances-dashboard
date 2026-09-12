#!/usr/bin/env python3
"""Optional DDR5 module temperatures from SPD5118 hubs over SMBus.

Kernels before 6.11 have no spd5118 hwmon driver, so a root-only helper
(this file, installed by tools/setup_spd.py as /usr/local/sbin/monitor-spd-temps)
reads the current temperature and the hardware limit registers of every
SPD5118 hub found on SMBus adapters and prints JSON. Only SMBus read
transactions are issued; no page register or EEPROM byte is written. The
dashboard calls it through one exact sudo command when MONITOR_SPD_TEMPS=1.
Slot assignment of a hub address is not derived from SPD contents, so labels
carry the bus address and mark the slot as unverified.
"""
import ctypes
import fcntl
import glob
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HELPER = '/usr/local/sbin/monitor-spd-temps'
COMMAND = ['sudo', '-n', HELPER]
DEVICE = '内存 · DDR5 SPD5118 温度传感器'
I2C_SLAVE, I2C_SMBUS = 0x0703, 0x0720
SMBUS_READ, BYTE_DATA, WORD_DATA = 1, 2, 3
ADDRESSES = range(0x50, 0x58)
REG_TYPE, REG_TS_CONFIG, REG_TEMP = 0x00, 0x1A, 0x31
LIMITS = {'high': 0x1C, 'low': 0x1E, 'crit': 0x20, 'lcrit': 0x22}  # MR28, MR30, MR32, MR34
DEVICE_TYPE = (0x51, 0x18)


class _Data(ctypes.Union):
    _fields_ = [('byte', ctypes.c_uint8), ('word', ctypes.c_uint16), ('block', ctypes.c_uint8 * 34)]


class _Msg(ctypes.Structure):
    _fields_ = [('read_write', ctypes.c_uint8), ('command', ctypes.c_uint8),
                ('size', ctypes.c_uint32), ('data', ctypes.POINTER(_Data))]


def smbus_read(fd, reg, size):
    d = _Data()
    fcntl.ioctl(fd, I2C_SMBUS, _Msg(SMBUS_READ, reg, size, ctypes.pointer(d)))
    return d.word if size == WORD_DATA else d.byte


def temperature(reg):
    """SPD5118 temperature word: 11-bit two's complement in bits 12..2, 0.25 degree steps."""
    v = (reg >> 2) & 0x7FF
    if v & 0x400:
        v -= 0x800
    return v * 0.25


def smbus_adapters():
    out = []
    for p in sorted(glob.glob('/sys/class/i2c-adapter/i2c-*'), key=lambda s: int(s.rsplit('-', 1)[1])):
        try:
            name = Path(p, 'name').read_text().strip()
        except OSError:
            continue
        if name.startswith('SMBus'):
            out.append((os.path.basename(p), name))
    return out


def scan():
    """Read every SPD5118 hub on SMBus adapters; addresses without one are skipped."""
    modules = []
    for bus, adapter in smbus_adapters():
        try:
            fd = os.open(f'/dev/{bus}', os.O_RDWR)
        except OSError:
            continue
        try:
            for addr in ADDRESSES:
                try:
                    fcntl.ioctl(fd, I2C_SLAVE, addr)
                    if (smbus_read(fd, REG_TYPE, BYTE_DATA), smbus_read(fd, REG_TYPE + 1, BYTE_DATA)) != DEVICE_TYPE:
                        continue
                    entry = {'bus': bus, 'adapter': adapter, 'addr': addr,
                             'ts_enabled': not smbus_read(fd, REG_TS_CONFIG, BYTE_DATA) & 1,
                             'temp': temperature(smbus_read(fd, REG_TEMP, WORD_DATA))}
                    for key, reg in LIMITS.items():
                        entry[key] = temperature(smbus_read(fd, reg, WORD_DATA))
                    modules.append(entry)
                except OSError:
                    continue
        finally:
            os.close(fd)
    return modules


class Reader:
    """Cached helper output; remembers module identities so failures stay visible."""

    def __init__(self, command=COMMAND, ttl=5.0):
        self.command, self.ttl = command, ttl
        self.lock = threading.Lock()
        self.at, self.modules, self.error, self.known = None, None, 'not read yet', []

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            if self.at is not None and now - self.at < self.ttl:
                return self.modules, self.error
            self.at = now
            try:
                r = subprocess.run(self.command, capture_output=True, timeout=10)
                if r.returncode != 0:
                    raise RuntimeError(f'exit {r.returncode}')
                modules = json.loads(r.stdout)['modules']
                if not isinstance(modules, list):
                    raise ValueError('modules missing')
                self.modules, self.error = modules, None
                self.known = [(m['bus'], m['addr']) for m in modules]
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, RuntimeError) as e:
                self.modules, self.error = None, str(e)
            return self.modules, self.error


reader = Reader()


def limit(v, lo=0, hi=125):
    return v if isinstance(v, (int, float)) and lo < v < hi else None


def sensors(env=None, spd_reader=None):
    env = os.environ if env is None else env
    if env.get('MONITOR_SPD_TEMPS', '').strip().lower() not in ('1', 'true', 'yes'):
        return []
    rd = spd_reader or reader
    modules, error = rd.snapshot()
    sampled_at = datetime.now(timezone.utc).isoformat()
    out = []
    items = modules if modules is not None else [{'bus': b, 'addr': a} for b, a in rd.known]
    for m in items:
        bus, addr = m['bus'], m['addr']
        value, quality, note, warn, crit = None, 'unavailable', '', None, None
        if modules is None:
            note = f'SPD 读取失败（{error}），未更新'
        elif not m.get('ts_enabled', True):
            note = 'SPD5118 温度传感器已禁用，未更新'
        elif limit(m.get('temp'), -40, 125) is None:
            note = f'SPD 温度 {m.get("temp")} 超出有效范围，未更新'
        else:
            value, quality = m['temp'], 'reported'
            warn, crit = limit(m.get('high')), limit(m.get('crit'))
            if warn is not None and crit is not None and warn > crit:
                warn = None
        out.append({
            'id': f'spd.{bus}.0x{addr:02x}', 'device_id': 'spd', 'device': DEVICE, 'group': 'memory',
            'label': f'内存条 @{bus} 0x{addr:02X}（槽位未核实）', 'value': value, 'warn': warn, 'crit': crit,
            'assumed': False, 'quality': quality, 'note': note, 'sampled_at': sampled_at,
            'source': {'driver': 'monitor-spd-temps', 'path': f'/dev/{bus}', 'label': f'0x{addr:02x} MR49/MR50'},
        })
    return out


def main():
    print(json.dumps({'modules': scan(), 'sampled_at': datetime.now(timezone.utc).isoformat()}))


if __name__ == '__main__':
    main()
