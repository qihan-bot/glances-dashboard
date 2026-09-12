"""ITE I2EC fan readings: duty from CTR/DCR, tach handling, calibration gate, failures."""
import json
import sys
import unittest

import fan


class FakePort:
    """Simulates SIO ID registers and I2EC reads from a dict of EC RAM addresses."""

    def __init__(self, chip=0x5571, ram=None, tear=False):
        self.chip, self.ram, self.tear = chip, ram or {}, tear
        self.index, self.d2adr, self.addr_lo, self.addr_hi, self.reads = None, None, 0, 0, 0

    def outb(self, port, value):
        if port == fan.IDX:
            self.index = value
        elif port == fan.DAT:
            if self.index == 0x2E:
                self.d2adr = value
            elif self.index == 0x2F:
                if self.d2adr == 0x10:
                    self.addr_lo = value
                elif self.d2adr == 0x11:
                    self.addr_hi = value
                elif self.d2adr == 0x12:
                    raise AssertionError('helper must never write EC RAM')

    def inb(self, port):
        if self.index == 0x20:
            return self.chip >> 8
        if self.index == 0x21:
            return self.chip & 0xFF
        assert self.index == 0x2F and self.d2adr == 0x12
        addr = self.addr_hi << 8 | self.addr_lo
        self.reads += 1
        if self.tear and addr == 0x181E and self.reads == 1:
            return 0x00  # first LSB read torn; re-read must be used
        return self.ram.get(addr, 0)


class FakeReader:
    def __init__(self, data=None, error=None, known=()):
        self.data, self.error, self.known = data, error, list(known)

    def snapshot(self):
        return self.data, self.error


def helper_data(**kw):
    base = {'chip': '0x5571', 'chip_name': 'ITE IT5571', 'supported': True, 'ctr': 63,
            'fans': [{'index': 1, 'dcr': 22, 'tach': 1150}, {'index': 2, 'dcr': 57, 'tach': 1068}]}
    base.update(kw)
    return base


class FanTests(unittest.TestCase):
    def test_scan_reads_registers_without_writing_ram(self):
        ram = {0x1801: 63, 0x1803: 22, 0x1804: 57, 0x181E: 0x7E, 0x181F: 0x04, 0x1820: 0x2C, 0x1821: 0x04}
        data = fan.scan(FakePort(ram=ram))
        self.assertEqual((data['chip'], data['supported'], data['ctr']), ('0x5571', True, 63))
        self.assertEqual(data['fans'], [{'index': 1, 'dcr': 22, 'tach': 0x047E}, {'index': 2, 'dcr': 57, 'tach': 0x042C}])
        torn = fan.scan(FakePort(ram=ram, tear=True))
        self.assertEqual(torn['fans'][0]['tach'], 0x047E)

    def test_unknown_chip_is_not_read(self):
        port = FakePort(chip=0x8613, ram={0x1801: 63})
        data = fan.scan(port)
        self.assertEqual((data['supported'], data['fans'], port.reads), (False, [], 0))

    def test_duty_and_uncalibrated_tach(self):
        out = fan.fans({'MONITOR_FAN_I2EC': '1'}, FakeReader(helper_data()))
        self.assertEqual([f['id'] for f in out], ['fan1', 'fan2'])
        f1 = out[0]
        self.assertEqual((f1['duty'], f1['tach_count'], f1['rpm'], f1['rpm_status'], f1['quality']), (34.4, 1150, None, 'uncalibrated', 'reported'))
        self.assertIn('未校准', f1['note'])
        self.assertEqual(f1['source']['driver'], 'monitor-fan-i2ec')
        saturated = fan.fans({'MONITOR_FAN_I2EC': '1'}, FakeReader(helper_data(fans=[{'index': 1, 'dcr': 68, 'tach': 0xFFFF}])))[0]
        self.assertEqual((saturated['duty'], saturated['tach_count']), (100.0, None))
        self.assertIn('无转速脉冲', saturated['note'])

    def test_calibrated_rpm(self):
        out = fan.fans({'MONITOR_FAN_I2EC': '1', 'MONITOR_FAN_TACH_HZ': '93750'}, FakeReader(helper_data()))
        self.assertEqual((out[0]['rpm'], out[0]['rpm_status'], out[0]['note']), (2446, 'calibrated', ''))

    def test_failures_and_gate(self):
        self.assertEqual(fan.fans({}, FakeReader(helper_data())), [])
        failed = fan.fans({'MONITOR_FAN_I2EC': '1'}, FakeReader(None, 'exit 1', known=[1, 2]))
        self.assertEqual([(f['id'], f['quality']) for f in failed], [('fan1', 'unavailable'), ('fan2', 'unavailable')])
        self.assertIn('exit 1', failed[0]['note'])
        unsupported = fan.fans({'MONITOR_FAN_I2EC': '1'}, FakeReader({'chip': '0x8613', 'supported': False, 'ctr': None, 'fans': []}))
        self.assertEqual(unsupported, [])

    def test_reader_parses_helper_json(self):
        good = fan.Reader([sys.executable, '-c', f'print({json.dumps(helper_data())!r})'], ttl=60)
        data, error = good.snapshot()
        self.assertEqual((error, data['ctr'], good.known), (None, 63, [1, 2]))
        bad = fan.Reader(['false'], ttl=0)
        self.assertIsNone(bad.snapshot()[0])


if __name__ == '__main__':
    unittest.main()
