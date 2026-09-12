"""SMART health flags, missing values, and failed refresh behavior."""
import json
import subprocess
import unittest
from unittest.mock import patch

import smart


def fixture(passed=True):
    return {'device': {'name': '/dev/sda'}, 'model_name': 'Test SATA SSD',
            'smart_status': {'passed': passed}, 'temperature': {'current': 40},
            'power_on_time': {'hours': 4296}, 'power_cycle_count': 77,
            'ata_smart_attributes': {'table': [
                {'id': 5, 'raw': {'value': 1}}, {'id': 197, 'raw': {'value': 1}},
                {'id': 198, 'raw': {'value': 0}}, {'id': 199, 'raw': {'value': 0}}]}}


class SmartTests(unittest.TestCase):
    def test_standard_statistics_use_valid_flags_and_logical_sector_units(self):
        data = fixture()
        data['logical_block_size'] = 4096
        data['ata_smart_attributes']['table'].append({'id': 242, 'raw': {'value': 99999}})
        data['ata_device_statistics'] = {'pages': [{'number': 1, 'table': [
            {'offset': 40, 'value': 20, 'flags': {'valid': True, 'normalized': False}},
            {'offset': 24, 'value': 100, 'flags': {'valid': False, 'normalized': False}},
            {'offset': 48, 'value': 50, 'flags': {'valid': True, 'normalized': True}},
        ]}]}
        stats = smart.parse(data, '/dev/sda')['statistics']
        self.assertEqual(stats['bytes_read'], 20 * 4096)
        self.assertNotIn('bytes_written', stats)
        self.assertNotIn('read_commands', stats)
        del data['logical_block_size']
        self.assertNotIn('bytes_read', smart.parse(data, '/dev/sda')['statistics'])

    @patch.dict('os.environ', MONITOR_SMART_DEVICE='/dev/sda', MONITOR_SMART_TEMPERATURE_UNVERIFIED='1')
    def test_unverified_temperature_retains_raw_value_without_chart_sample(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(fixture()), '')
        with patch('smart.subprocess.run', return_value=result):
            snapshot = smart.Collector().snapshot()
        self.assertEqual(snapshot['disks'][0]['temperature'], 40)
        self.assertEqual(snapshot['disks'][0]['temperature_status'], 'unverified')
        self.assertEqual(smart.temperature_sensors(snapshot), [])

    def test_pass_does_not_hide_counters_or_invent_lifetime(self):
        d = smart.parse(fixture(), '/dev/sda')
        self.assertIs(d['passed'], True)
        self.assertEqual(d['state'], 'warning')
        self.assertEqual(d['counters']['197'], 1)
        self.assertEqual(d['power_on_hours'], 4296)
        self.assertFalse(d['database_matched'])
        self.assertNotIn('life_percent', d)
        sensor = smart.temperature_sensors({'disks': [d]})[0]
        self.assertEqual((sensor['group'], sensor['value']), ('sata', 40))
        self.assertIsNone(sensor['crit'])

    def test_health_exit_bits_are_data_but_read_errors_are_failures(self):
        self.assertEqual(smart.parse(fixture(False), '/dev/sda', 8)['state'], 'failed')
        d = fixture(); d['ata_smart_attributes']['table'] = []
        self.assertEqual(smart.parse(d, '/dev/sda', 64)['state'], 'warning')
        for code in (1, 2, 4):
            with self.assertRaises(ValueError):
                smart.parse(d, '/dev/sda', code)
        with self.assertRaises(ValueError):
            smart.parse(d, '/dev/sdb')

    def test_absent_fields_stay_unknown(self):
        d = smart.parse({'device': {'name': '/dev/sda'}}, '/dev/sda')
        self.assertEqual(d['state'], 'unknown')
        self.assertIsNone(d['temperature'])
        self.assertIsNone(d['counters']['5'])
        self.assertEqual(smart.temperature_sensors({'disks': [d]}), [])

    @patch.dict('os.environ', MONITOR_SMART_DEVICE='/dev/sda')
    def test_cache_failure_retains_old_values_without_new_temperature_and_recovers(self):
        collector = smart.Collector()
        good = subprocess.CompletedProcess([], 0, json.dumps(fixture()), '')
        with patch('smart.subprocess.run', return_value=good) as run:
            first = collector.snapshot()
            self.assertEqual(collector.snapshot(), first)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/sudo', '-n', '/usr/sbin/smartctl', '-x', '-j', '/dev/sda'])
        collector.next_read = 0
        with patch('smart.subprocess.run', side_effect=subprocess.TimeoutExpired('smartctl', 4)):
            failed = collector.snapshot()
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['disks'], first['disks'])
        self.assertEqual(failed['updated_at'], first['updated_at'])
        self.assertEqual(smart.temperature_sensors(failed), [])
        collector.next_read = 0
        with patch('smart.subprocess.run', return_value=good):
            self.assertFalse(collector.snapshot()['stale'])

    def test_disabled_or_invalid_configuration_never_executes(self):
        with patch('smart.subprocess.run') as run:
            with patch.dict('os.environ', MONITOR_SMART_DEVICE=''):
                self.assertFalse(smart.Collector().snapshot()['enabled'])
            with patch.dict('os.environ', MONITOR_SMART_DEVICE='/dev/sda;reboot'):
                self.assertTrue(smart.Collector().snapshot()['stale'])
            run.assert_not_called()
