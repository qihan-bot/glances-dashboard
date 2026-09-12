"""EC RAM byte mapping, validity handling, and read failures."""
import subprocess
import sys
import unittest

import ec


class FakeReader:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error

    def snapshot(self):
        return self.data, self.error


class EcTests(unittest.TestCase):
    def test_parse_map(self):
        self.assertEqual(ec.parse_map(' 0x04:board:主板热敏 1, 9:diagnostic:CPU（EC 读数）'),
                         [(4, 'board', '主板热敏 1'), (9, 'diagnostic', 'CPU（EC 读数）')])
        for bad in ('0x04:board', '0x100:board:x', '0x04:core:x', '0x04:board:'):
            with self.assertRaises(ValueError):
                ec.parse_map(bad)

    def test_sensors_from_bytes(self):
        ram = bytearray(256)
        ram[4], ram[5], ram[9] = 39, 38, 47
        env = {'MONITOR_EC_TEMPS': '0x04:board:主板热敏 1,0x05:board:主板热敏 2,0x09:diagnostic:CPU（EC 读数）'}
        data = {s['id']: s for s in ec.sensors(env, FakeReader(bytes(ram)))}
        self.assertEqual(list(data), ['ec.0x04', 'ec.0x05', 'ec.0x09'])
        self.assertEqual(data['ec.0x04']['value'], 39)
        self.assertEqual(data['ec.0x04']['group'], 'board')
        self.assertEqual(data['ec.0x04']['label'], '主板热敏 1（EC 0x04，位置未核实）')
        self.assertEqual(data['ec.0x09']['label'], 'CPU（EC 读数）（EC 0x09）')
        self.assertEqual(data['ec.0x09']['group'], 'diagnostic')
        self.assertIsNone(data['ec.0x04']['warn'])
        self.assertIsNone(data['ec.0x04']['crit'])
        self.assertEqual(data['ec.0x04']['quality'], 'reported')
        self.assertEqual(data['ec.0x04']['source'], {'driver': 'ec_sys', 'path': ec.EC_IO, 'label': 'byte 0x04'})

    def test_invalid_bytes_and_read_failure_are_unavailable(self):
        ram = bytearray(256)
        ram[4], ram[5] = 0, 0xFF
        env = {'MONITOR_EC_TEMPS': '0x04:board:a,0x05:board:b'}
        for s in ec.sensors(env, FakeReader(bytes(ram))):
            self.assertIsNone(s['value'])
            self.assertEqual(s['quality'], 'unavailable')
            self.assertIn('未更新', s['note'])
        failed = ec.sensors(env, FakeReader(None, 'exit 1, 0 bytes'))
        self.assertEqual([s['quality'] for s in failed], ['unavailable', 'unavailable'])
        self.assertIn('exit 1', failed[0]['note'])

    def test_no_or_bad_config_reports_nothing(self):
        self.assertEqual(ec.sensors({}, FakeReader(bytes(256))), [])
        self.assertEqual(ec.sensors({'MONITOR_EC_TEMPS': 'garbage'}, FakeReader(bytes(256))), [])

    def test_reader_runs_command_and_caches(self):
        good = ec.Reader([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes(range(256)))'], ttl=60)
        data, error = good.snapshot()
        self.assertEqual((len(data), error, data[4]), (256, None, 4))
        good.command = ['false']
        self.assertEqual(good.snapshot()[1], None, 'cached within ttl')
        bad = ec.Reader(['false'], ttl=0)
        self.assertEqual(bad.snapshot()[0], None)
        self.assertIn('exit 1', bad.snapshot()[1])
        short = ec.Reader([sys.executable, '-c', 'print(1)'], ttl=0)
        self.assertIn('2 bytes', short.snapshot()[1])


if __name__ == '__main__':
    unittest.main()
