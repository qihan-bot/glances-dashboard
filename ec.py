"""Optional board temperatures read from ACPI Embedded Controller RAM via ec_sys.

The EC address map is host-specific and is not described by hwmon or (on this
firmware) by the DSDT, so only bytes listed in ``MONITOR_EC_TEMPS`` are reported
and each must first be established with tools/probe_ec.py and tools/ec_map.py.
Entry format: ``0x04:board:主板热敏 1,0x09:diagnostic:CPU（EC 读数）``.
``board`` entries join the board chart; ``diagnostic`` entries appear only in
the table. The read is one exact sudo command granted by tools/setup_ec.py and
never writes to the EC.
"""
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

EC_IO = '/sys/kernel/debug/ec/ec0/io'
COMMAND = ['sudo', '-n', '/usr/bin/cat', EC_IO]
DEVICE = '主板 · 嵌入式控制器 EC0（ACPI PNP0C09）'
GROUPS = ('board', 'diagnostic')


def parse_map(text):
    """Return [(offset, group, label)] from the MONITOR_EC_TEMPS syntax."""
    out = []
    for item in filter(None, (s.strip() for s in (text or '').split(','))):
        parts = item.split(':', 2)
        if len(parts) != 3:
            raise ValueError(f'EC map entry must be offset:group:label: {item!r}')
        offset, group, label = int(parts[0].strip(), 0), parts[1].strip(), parts[2].strip()
        if not 0 <= offset < 256 or group not in GROUPS or not label:
            raise ValueError(f'invalid EC map entry: {item!r}')
        out.append((offset, group, label))
    return out


class Reader:
    """Cached 256-byte EC RAM snapshot; a failed read yields (None, error)."""

    def __init__(self, command=COMMAND, ttl=2.0):
        self.command, self.ttl = command, ttl
        self.lock = threading.Lock()
        self.at, self.data, self.error = None, None, 'not read yet'

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            if self.at is not None and now - self.at < self.ttl:
                return self.data, self.error
            self.at = now
            try:
                r = subprocess.run(self.command, capture_output=True, timeout=5)
                if r.returncode != 0 or len(r.stdout) != 256:
                    raise RuntimeError(f'exit {r.returncode}, {len(r.stdout)} bytes')
                self.data, self.error = r.stdout, None
            except (OSError, subprocess.SubprocessError, RuntimeError) as e:
                self.data, self.error = None, str(e)
            return self.data, self.error


reader = Reader()


def sensors(env=None, ec_reader=None):
    env = os.environ if env is None else env
    spec = env.get('MONITOR_EC_TEMPS', '')
    if not spec.strip():
        return []
    try:
        mapping = parse_map(spec)
    except ValueError as e:
        print(f'MONITOR_EC_TEMPS ignored: {e}', file=sys.stderr)
        return []
    data, error = (ec_reader or reader).snapshot()
    sampled_at = datetime.now(timezone.utc).isoformat()
    out = []
    for offset, group, label in mapping:
        value, quality, note = None, 'unavailable', ''
        if data is None:
            note = f'EC 读取失败（{error}），未更新'
        elif not 1 <= data[offset] <= 127:
            note = f'EC 字节 0x{offset:02X} 原始值 {data[offset]} 超出有效范围，未更新'
        else:
            value, quality = data[offset], 'reported'
        suffix = f'（EC 0x{offset:02X}，位置未核实）' if group == 'board' else f'（EC 0x{offset:02X}）'
        out.append({
            'id': f'ec.0x{offset:02x}', 'device_id': 'ec', 'device': DEVICE, 'group': group,
            'label': label + suffix, 'value': value, 'warn': None, 'crit': None, 'assumed': False,
            'quality': quality, 'note': note, 'sampled_at': sampled_at,
            'source': {'driver': 'ec_sys', 'path': EC_IO, 'label': f'byte 0x{offset:02X}'},
        })
    return out
