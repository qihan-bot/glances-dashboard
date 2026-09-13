#!/usr/bin/env python3
"""Optional NVIDIA GPU temperature and power from nvidia-smi.

NVIDIA GPUs expose no hwmon temperature on the tested platform (DGX Spark GB10,
driver 580, kernel 7.0-nvidia), and Glances' gpu plugin reports temperature but no
power, so the sidecar queries nvidia-smi directly. Reads only. One bounded query is
shared by all requests per INTERVAL seconds. Hosts without nvidia-smi report nothing.
A failed query marks the last known GPUs unavailable; it never repeats old values.

Thresholds come only from the classic "GPU Slowdown Temp" / "GPU Shutdown Temp"
lines of `nvidia-smi -q -d TEMPERATURE`. The GB10 reports those as N/A and only a
"T.Limit" headroom, which is not a threshold, so no limit is inferred there.
"""
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

INTERVAL = 2
FIELDS = ['index', 'name', 'temperature.gpu', 'power.draw']
QUERY = ['nvidia-smi', f'--query-gpu={",".join(FIELDS)}', '--format=csv,noheader,nounits']
LIMITS = ['nvidia-smi', '-q', '-d', 'TEMPERATURE']


def number(text, lo=-50, hi=500):
    try:
        v = float(text.strip())
    except (TypeError, ValueError):
        return None  # "[N/A]" and blanks
    return v if lo < v < hi else None


def parse_query(text):
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) != len(FIELDS) or not parts[0].isdigit():
            continue
        gpus.append({'index': int(parts[0]), 'name': parts[1] or 'NVIDIA GPU',
                     'temp': number(parts[2]), 'power': number(parts[3], 0, 10000)})
    return gpus


def parse_limits(text):
    """Per-GPU (warn, crit) from Slowdown/Shutdown Temp lines; T.Limit lines are ignored."""
    out, cur = [], None
    for line in text.splitlines():
        if re.match(r'GPU [0-9A-Fa-f]{8}:', line):
            cur = [None, None]
            out.append(cur)
        elif cur is not None:
            m = re.match(r'\s+GPU (Slowdown|Shutdown) Temp\s+:\s+(\S+)', line)
            if m:
                cur[0 if m.group(1) == 'Slowdown' else 1] = number(m.group(2))
    return [tuple(x) for x in out]


class Collector:
    def __init__(self, run=None, which=None):
        self.run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False))
        self.which = which or shutil.which
        self.lock = threading.Lock()
        self.next_read = 0
        self.gpus = []
        self.limits = None
        self.error = None
        self.sampled_at = None

    def snapshot(self):
        if not self.which('nvidia-smi'):
            return None, None, None
        with self.lock:
            if time.monotonic() >= self.next_read:
                try:
                    res = self.run(QUERY)
                    if res.returncode != 0:
                        raise ValueError(f'exit {res.returncode}')
                    gpus = parse_query(res.stdout)
                    if not gpus:
                        raise ValueError('no GPU rows')
                    if self.limits is None:
                        lim = self.run(LIMITS)
                        self.limits = parse_limits(lim.stdout) if lim.returncode == 0 else []
                    self.gpus, self.error = gpus, None
                    self.sampled_at = datetime.now(timezone.utc).isoformat()
                except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                    self.error = f'nvidia-smi 读取失败（{e}）'
                self.next_read = time.monotonic() + INTERVAL
            return self.gpus, self.limits or [], self.error


collector = Collector()


def sensors(coll=None):
    gpus, limits, error = (coll or collector).snapshot()
    if gpus is None:
        return []
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for g in gpus or [{'index': 0, 'name': 'NVIDIA GPU', 'temp': None, 'power': None}]:
        did = 'gpu' if g['index'] == 0 else f'gpu{g["index"]}'
        device = f'GPU · {g["name"]}'
        warn, crit = limits[g['index']] if g['index'] < len(limits) else (None, None)
        if warn is not None and crit is not None and warn > crit:
            warn = None
        src = {'driver': 'nvidia-smi', 'path': f'nvidia-smi --id={g["index"]}'}
        if error:
            value, quality, note = None, 'unavailable', f'{error}，未更新'
        elif g['temp'] is None:
            value, quality, note = None, 'unavailable', 'nvidia-smi 未报告温度，未更新'
        else:
            value, quality, note = g['temp'], 'reported', ''
        out.append({'id': f'{did}.edge', 'device_id': did, 'device': device, 'group': 'core', 'label': '核心',
                    'value': value, 'warn': warn, 'crit': crit, 'assumed': False, 'quality': quality,
                    'note': note, 'sampled_at': now if error else (coll or collector).sampled_at,
                    'source': {**src, 'label': 'temperature.gpu'}})
        if not error and g['power'] is not None:
            out.append({'id': f'{did}.power', 'device_id': did, 'device': device, 'group': 'power', 'label': '功耗',
                        'value': g['power'], 'unit': 'W', 'quality': 'reported', 'sampled_at': (coll or collector).sampled_at,
                        'source': {**src, 'label': 'power.draw'}})
    return out
