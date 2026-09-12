"""SPD5118 decoding, hardware limits, and helper failure handling."""
import json
import sys
import unittest

import spd


class FakeReader:
    def __init__(self, modules=None, error=None, known=()):
        self.modules, self.error, self.known = modules, error, list(known)

    def snapshot(self):
        return self.modules, self.error


def module(**kw):
    base = {'bus': 'i2c-0', 'adapter': 'SMBus PIIX4 adapter port 0 at 0b00', 'addr': 0x50, 'ts_enabled': True,
            'temp': 35.75, 'high': 85.0, 'low': 0.0, 'crit': 95.0, 'lcrit': 0.0}
    base.update(kw)
    return base


class SpdTests(unittest.TestCase):
    def test_temperature_decode(self):
        self.assertEqual(spd.temperature(0x023C), 35.75)
        self.assertEqual(spd.temperature(0x0550), 85.0)
        self.assertEqual(spd.temperature(0x1FFC), -0.25)
        self.assertEqual(spd.temperature(0x0000), 0.0)

    def test_sensors_with_hardware_limits(self):
        env = {'MONITOR_SPD_TEMPS': '1'}
        data = spd.sensors(env, FakeReader([module(), module(addr=0x51, temp=32.5)]))
        self.assertEqual([s['id'] for s in data], ['spd.i2c-0.0x50', 'spd.i2c-0.0x51'])
        s = data[0]
        self.assertEqual((s['value'], s['warn'], s['crit'], s['group'], s['quality']), (35.75, 85.0, 95.0, 'memory', 'reported'))
        self.assertEqual(s['label'], '内存条 @i2c-0 0x50（槽位未核实）')
        self.assertFalse(s['assumed'])
        self.assertEqual(s['source']['driver'], 'monitor-spd-temps')
        odd = spd.sensors(env, FakeReader([module(high=200.0, crit=-5.0)]))[0]
        self.assertEqual((odd['warn'], odd['crit']), (None, None))
        inverted = spd.sensors(env, FakeReader([module(high=95.0, crit=85.0)]))[0]
        self.assertEqual((inverted['warn'], inverted['crit']), (None, 85.0))

    def test_disabled_sensor_and_failures_are_unavailable(self):
        env = {'MONITOR_SPD_TEMPS': '1'}
        off = spd.sensors(env, FakeReader([module(ts_enabled=False)]))[0]
        self.assertEqual((off['value'], off['quality']), (None, 'unavailable'))
        self.assertIn('未更新', off['note'])
        failed = spd.sensors(env, FakeReader(None, 'exit 1', known=[('i2c-0', 0x50), ('i2c-0', 0x51)]))
        self.assertEqual([(s['id'], s['quality']) for s in failed],
                         [('spd.i2c-0.0x50', 'unavailable'), ('spd.i2c-0.0x51', 'unavailable')])
        self.assertIn('exit 1', failed[0]['note'])
        self.assertEqual(spd.sensors(env, FakeReader(None, 'exit 1')), [])

    def test_gate_off_reports_nothing(self):
        self.assertEqual(spd.sensors({}, FakeReader([module()])), [])
        self.assertEqual(spd.sensors({'MONITOR_SPD_TEMPS': '0'}, FakeReader([module()])), [])

    def test_reader_parses_helper_json_and_remembers_modules(self):
        payload = json.dumps({'modules': [module()]})
        good = spd.Reader([sys.executable, '-c', f'print({payload!r})'], ttl=60)
        modules, error = good.snapshot()
        self.assertEqual((error, modules[0]['addr'], good.known), (None, 0x50, [('i2c-0', 0x50)]))
        bad = spd.Reader(['false'], ttl=0)
        self.assertEqual(bad.snapshot()[0], None)
        self.assertIn('exit 1', bad.snapshot()[1])
        junk = spd.Reader([sys.executable, '-c', 'print("{}")'], ttl=0)
        self.assertIsNone(junk.snapshot()[0])


if __name__ == '__main__':
    unittest.main()
