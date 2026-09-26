"""Kernel saturation/error counters and failed units against /proc fixtures."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import health

PRESSURE = {
    'cpu': 'some avg10=1.76 avg60=1.04 avg300=0.26 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n',
    'memory': 'some avg10=0.00 avg60=0.00 avg300=0.00 total=2\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=2\n',
    'io': 'some avg10=0.19 avg60=0.17 avg300=0.08 total=4\nfull avg10=0.07 avg60=0.11 avg300=0.06 total=4\n',
}
SNMP = ('Ip: Forwarding DefaultTTL\nIp: 1 64\n'
        'Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors\n'
        'Tcp: 1 200 120000 -1 168535 61202 12388 43774 170 18344163 18877274 11157 3 61656 0\n')
NETDEV = ('Inter-|   Receive                                                |  Transmit\n'
          ' face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n'
          '    lo: 2632438743 8640080    0    0    0     0          0         0 2632438743 8640080    0    0    0     0       0          0\n'
          'enp2s0: 16333383624 17337730    2 13695    0     0          0     16513 6428522254 11357637    1    5    0     0       0          0\n')


def proc_fixture(root, pressure=True):
    if pressure:
        (root / 'pressure').mkdir()
        for name, text in PRESSURE.items():
            (root / 'pressure' / name).write_text(text)
    (root / 'net').mkdir()
    (root / 'net/snmp').write_text(SNMP)
    (root / 'net/dev').write_text(NETDEV)
    (root / 'vmstat').write_text('nr_free_pages 1\npswpin 141109\npswpout 644977\npgmajfault 423001\noom_kill 2\n')


class HealthTests(unittest.TestCase):
    def test_counters_parse_from_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); proc_fixture(root)
            with patch.object(health, 'PROC', root):
                self.assertEqual(health.psi()['cpu']['some'], {'avg10': 1.76, 'avg60': 1.04, 'avg300': 0.26})
                self.assertEqual(health.psi()['io']['full']['avg60'], 0.11)
                self.assertEqual(health.vmstat(), {'pswpin': 141109, 'pswpout': 644977, 'pgmajfault': 423001, 'oom_kill': 2})
                self.assertEqual(health.tcp(), {'out_segs': 18877274, 'retrans_segs': 11157})
                self.assertEqual(health.netdev()['enp2s0'], {'rx_errs': 2, 'rx_drop': 13695, 'tx_errs': 1, 'tx_drop': 5})

    def test_missing_sources_report_unknown_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(health, 'PROC', root):
                self.assertIsNone(health.psi())
                self.assertIsNone(health.vmstat())
                self.assertIsNone(health.tcp())
                self.assertIsNone(health.netdev())

    def test_failed_units_cover_both_managers_and_are_cached(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            units = [{'unit': 'backup.service', 'load': 'loaded', 'active': 'failed', 'sub': 'failed', 'description': 'Backup'}] if '--user' in cmd else []
            return subprocess.CompletedProcess(cmd, 0, json.dumps(units), '')
        u = health.Units()
        with patch.object(health.subprocess, 'run', run):
            self.assertEqual(u.snapshot(), [{'unit': 'backup.service', 'scope': 'user', 'description': 'Backup'}])
            u.snapshot()
        self.assertEqual(len(calls), 2)

    def test_systemctl_unavailable_is_unknown(self):
        def run(cmd, **kw):
            raise FileNotFoundError('systemctl')
        with patch.object(health.subprocess, 'run', run):
            self.assertIsNone(health.Units().snapshot())


if __name__ == '__main__':
    unittest.main()
