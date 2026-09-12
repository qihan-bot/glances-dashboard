#!/usr/bin/env python3
"""Optional fan duty and tachometer readings from an ITE embedded controller (I2EC).

ITE ECs (IT5571 verified) expose their internal RAM through the Super I/O
"I2EC" indirect interface at ports 0x2E/0x2F. The PWM module sits at 0x1800:
CTR (0x1801) is the PWM cycle length, DCR1/DCR2 (0x1803/0x1804) the duty
registers, and F1/F2 tachometer period counters at 0x181E-0x1821 (larger =
slower). This file doubles as the root-only helper /usr/local/sbin/monitor-fan-i2ec
installed by tools/setup_fan.py; the dashboard calls it through one exact sudo
command when MONITOR_FAN_I2EC=1. Only reads are issued: D2DAT is never written
while the data register (0x12) is selected, so EC RAM is never modified.

Duty comes from DCR/(CTR+1). RPM needs the tachometer clock, which is not
verified for this chip; set MONITOR_FAN_TACH_HZ after calibrating against the
BIOS hardware monitor or a known fan speed (RPM = 60 * Hz / (2 * count)).
Without it the raw count is reported and no RPM is claimed.
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

HELPER = '/usr/local/sbin/monitor-fan-i2ec'
COMMAND = ['sudo', '-n', HELPER]
IDX, DAT = 0x2E, 0x2F
CHIPS = {0x5571: 'ITE IT5571'}
REG_CTR = 0x1801
FANS = ({'index': 1, 'dcr': 0x1803, 'tach': 0x181E}, {'index': 2, 'dcr': 0x1804, 'tach': 0x1820})
TACH_INVALID = 0xFFF0  # counter saturates when no pulses arrive


class Port:
    def __init__(self, path='/dev/port'):
        self.fd = os.open(path, os.O_RDWR)

    def outb(self, port, value):
        os.pwrite(self.fd, bytes([value]), port)

    def inb(self, port):
        return os.pread(self.fd, 1, port)[0]

    def close(self):
        os.close(self.fd)


def sio_id(p):
    p.outb(IDX, 0x20)
    hi = p.inb(DAT)
    p.outb(IDX, 0x21)
    return hi << 8 | p.inb(DAT)


def i2ec_read(p, addr):
    p.outb(IDX, 0x2E); p.outb(DAT, 0x11); p.outb(IDX, 0x2F); p.outb(DAT, (addr >> 8) & 0xFF)
    p.outb(IDX, 0x2E); p.outb(DAT, 0x10); p.outb(IDX, 0x2F); p.outb(DAT, addr & 0xFF)
    p.outb(IDX, 0x2E); p.outb(DAT, 0x12); p.outb(IDX, 0x2F)
    return p.inb(DAT)


def read_word(p, addr):
    """16-bit LSB/MSB pair; re-read the LSB so a counter update between reads is not torn."""
    for _ in range(3):
        lo, hi, again = i2ec_read(p, addr), i2ec_read(p, addr + 1), i2ec_read(p, addr)
        if lo == again:
            break
    return hi << 8 | again


def scan(port=None):
    p = port or Port()
    try:
        chip = sio_id(p)
        if chip not in CHIPS:
            return {'chip': f'0x{chip:04X}', 'supported': False, 'ctr': None, 'fans': []}
        return {'chip': f'0x{chip:04X}', 'chip_name': CHIPS[chip], 'supported': True, 'ctr': i2ec_read(p, REG_CTR),
                'fans': [{'index': f['index'], 'dcr': i2ec_read(p, f['dcr']), 'tach': read_word(p, f['tach'])} for f in FANS]}
    finally:
        if port is None:
            p.close()


class Reader:
    def __init__(self, command=COMMAND, ttl=5.0):
        self.command, self.ttl = command, ttl
        self.lock = threading.Lock()
        self.at, self.data, self.error, self.known = None, None, 'not read yet', []

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            if self.at is not None and now - self.at < self.ttl:
                return self.data, self.error
            self.at = now
            try:
                r = subprocess.run(self.command, capture_output=True, timeout=10)
                if r.returncode != 0:
                    raise RuntimeError(f'exit {r.returncode}')
                data = json.loads(r.stdout)
                if not isinstance(data.get('fans'), list):
                    raise ValueError('fans missing')
                self.data, self.error = data, None
                self.known = [f['index'] for f in data['fans']]
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, RuntimeError) as e:
                self.data, self.error = None, str(e)
            return self.data, self.error


reader = Reader()


def fans(env=None, fan_reader=None):
    env = os.environ if env is None else env
    if env.get('MONITOR_FAN_I2EC', '').strip().lower() not in ('1', 'true', 'yes'):
        return []
    try:
        tach_hz = float(env.get('MONITOR_FAN_TACH_HZ', '') or 0)
    except ValueError:
        tach_hz = 0
    rd = fan_reader or reader
    data, error = rd.snapshot()
    sampled_at = datetime.now(timezone.utc).isoformat()
    out = []
    items = data['fans'] if data is not None else [{'index': i} for i in rd.known]
    for f in items:
        idx = f['index']
        entry = {'id': f'fan{idx}', 'label': f'风扇 {idx}（EC PWM 通道 {idx}）', 'duty': None, 'dcr': None, 'ctr': None,
                 'tach_count': None, 'rpm': None, 'rpm_status': 'uncalibrated', 'quality': 'unavailable', 'note': '',
                 'sampled_at': sampled_at, 'chip': data.get('chip_name') if data else None,
                 'source': {'driver': 'monitor-fan-i2ec', 'path': '/dev/port',
                            'label': f'I2EC 0x1801 CTR, 0x{FANS[idx-1]["dcr"]:04X} DCR{idx}, 0x{FANS[idx-1]["tach"]:04X} F{idx}TLRR/MRR'}}
        if data is None:
            entry['note'] = f'EC 风扇读取失败（{error}），未更新'
        elif not data.get('supported', True):
            entry['note'] = f'EC 芯片 {data.get("chip")} 未验证寄存器映射，未读取'
        else:
            ctr, dcr, count = data.get('ctr'), f.get('dcr'), f.get('tach')
            if not isinstance(ctr, int) or not isinstance(dcr, int) or ctr <= 0:
                entry['note'] = 'PWM 周期寄存器无效，未更新'
            else:
                entry.update(quality='reported', dcr=dcr, ctr=ctr, duty=round(min(100.0, dcr / (ctr + 1) * 100), 1))
                if isinstance(count, int) and 0 < count < TACH_INVALID:
                    entry['tach_count'] = count
                    if tach_hz > 0:
                        entry.update(rpm=round(60 * tach_hz / (2 * count)), rpm_status='calibrated')
                    else:
                        entry['note'] = '转速计数为原始周期计数；RPM 未校准（设置 MONITOR_FAN_TACH_HZ 后显示）'
                else:
                    entry['note'] = '无转速脉冲（风扇停转或未接测速线）'
        out.append(entry)
    return out


def main():
    print(json.dumps({**scan(), 'sampled_at': datetime.now(timezone.utc).isoformat()}))


if __name__ == '__main__':
    main()
