"""Kernel saturation/error counters and failed systemd units for /sensors.json.

Glances covers utilization; this adds the saturation and error side:
PSI (/proc/pressure), swap-in/out, major faults and OOM kills (/proc/vmstat),
TCP retransmissions (/proc/net/snmp), per-interface errors/drops (/proc/net/dev),
failed systemd units (system manager plus this service's user manager) and the
Ubuntu reboot-required flag. Counters are cumulative; the page computes rates
from consecutive samples using the host's monotonic clock in `mono`.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

PROC = Path('/proc')
REBOOT_FLAG = Path('/run/reboot-required')
VMSTAT_KEYS = ('pswpin', 'pswpout', 'pgmajfault', 'oom_kill')


def psi():
    out = {}
    for res in ('cpu', 'memory', 'io'):
        try:
            text = (PROC / 'pressure' / res).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            kind, *fields = line.split()
            vals = dict(f.split('=', 1) for f in fields)
            out.setdefault(res, {})[kind] = {k: float(vals[k]) for k in ('avg10', 'avg60', 'avg300')}
    return out or None


def vmstat():
    try:
        rows = dict(line.split() for line in (PROC / 'vmstat').read_text().splitlines())
    except (OSError, ValueError):
        return None
    return {k: int(rows[k]) for k in VMSTAT_KEYS if k in rows}


def tcp():
    try:
        lines = [l.split() for l in (PROC / 'net/snmp').read_text().splitlines() if l.startswith('Tcp:')]
        row = dict(zip(lines[0][1:], map(int, lines[1][1:])))
        return {'out_segs': row['OutSegs'], 'retrans_segs': row['RetransSegs']}
    except (OSError, IndexError, KeyError, ValueError):
        return None


def netdev():
    out = {}
    try:
        lines = (PROC / 'net/dev').read_text().splitlines()[2:]
    except OSError:
        return None
    for line in lines:
        name, _, rest = line.partition(':')
        f = rest.split()
        if len(f) >= 12:
            out[name.strip()] = {'rx_errs': int(f[2]), 'rx_drop': int(f[3]), 'tx_errs': int(f[10]), 'tx_drop': int(f[11])}
    return out


class Units:
    """Failed units, refreshed at most every 30 s (systemctl costs ~20 ms each)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.next_read = 0
        self.last = None

    def snapshot(self):
        with self.lock:
            if time.monotonic() >= self.next_read:
                failed, ok = [], False
                for scope, extra in (('system', []), ('user', ['--user'])):
                    try:
                        r = subprocess.run(['systemctl', *extra, 'list-units', '--failed', '--output=json', '--no-pager'],
                                           capture_output=True, text=True, timeout=4, check=False)
                        if r.returncode == 0:
                            failed += [{'unit': u['unit'], 'scope': scope, 'description': u.get('description', '')}
                                       for u in json.loads(r.stdout or '[]')]
                            ok = True
                    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                        pass
                self.last = failed if ok else None
                self.next_read = time.monotonic() + 30
            return self.last


units = Units()


def snapshot():
    return {
        'mono': time.monotonic(),
        'page_size': os.sysconf('SC_PAGE_SIZE'),
        'psi': psi(),
        'vm': vmstat(),
        'tcp': tcp(),
        'net': netdev(),
        'failed_units': units.snapshot(),
        'reboot_required': REBOOT_FLAG.exists(),
    }
