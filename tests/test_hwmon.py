"""Hardware identity and host-specific exclusions against sysfs fixtures."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import serve


class HwmonTests(unittest.TestCase):
    def test_n100_channels_and_host_scoped_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures = [
                dict(name='acpitz', temp1_input='27800'),
                dict(name='it8613', temp1_input='55000', temp1_max='-62000',
                     temp2_input='54000', temp2_max='-99000', temp3_input='-43000'),
                dict(name='coretemp', temp1_input='58000', temp1_label='Package id 0',
                     temp1_max='105000', temp1_crit='105000', temp2_input='56000', temp2_label='Core 0'),
            ]
            for i, fields in enumerate(fixtures):
                hw = root / f'hwmon{i}'
                hw.mkdir()
                for name, value in fields.items():
                    (hw / name).write_text(value)
            with patch.object(serve, 'HWMON', root), patch.dict(os.environ, {
                'MONITOR_SENSOR_EXCLUDE': 'acpitz:temp1,it8613:temp3',
                'MONITOR_SENSOR_IGNORE_LIMITS': 'it8613',
            }):
                data = {s['id']: s for s in serve.sensors()}
                self.assertNotIn('board.temp1', data)
                self.assertNotIn('it8613.temp3', data)
                self.assertEqual(data['it8613.temp1']['value'], 55)
                self.assertEqual(data['it8613.temp2']['group'], 'board')
                self.assertIn('位置未核实', data['it8613.temp1']['label'])
                self.assertIsNone(data['it8613.temp1']['warn'])
                self.assertEqual(data['cpu.package_id_0']['crit'], 105)
                self.assertEqual(data['cpu.package_id_0']['group'], 'core')
                self.assertEqual(data['cpu.core_0']['group'], 'cpu_core')
            with patch.object(serve, 'HWMON', root), patch.dict(os.environ, {
                'MONITOR_SENSOR_EXCLUDE': '', 'MONITOR_SENSOR_IGNORE_LIMITS': '',
            }):
                data = {s['id']: s for s in serve.sensors()}
                acpi = next(s for s in data.values() if s['source']['driver'] == 'acpitz')
                self.assertEqual(acpi['value'], 27.8)
                self.assertEqual(acpi['quality'], 'unverified')
                self.assertEqual(acpi['group'], 'diagnostic')
                self.assertIn('it8613.temp3', data)

    def test_channel_errors_are_isolated_and_thresholds_are_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp) / 'hwmon0'; hw.mkdir()
            fields = {'name': 'k10temp', 'temp1_input': '90500', 'temp1_label': 'Tctl',
                      'temp2_input': 'invalid', 'temp3_input': '45000', 'temp3_fault': '1',
                      'temp4_input': '50000', 'temp4_enable': '0'}
            for name, value in fields.items(): (hw / name).write_text(value)
            with patch.object(serve, 'HWMON', Path(tmp)):
                rows = {s['id']: s for s in serve.sensors()}
            good = rows['cpu.tctl']
            self.assertEqual(good['value'], 90.5)
            self.assertIsNone(good['warn']); self.assertIsNone(good['crit'])
            self.assertFalse(good['assumed'])
            self.assertIn('sampled_at', good)
            for name in ['temp2', 'temp3', 'temp4']:
                self.assertIsNone(rows['cpu.' + name]['value'])
                self.assertEqual(rows['cpu.' + name]['quality'], 'unavailable')

    def test_nvme_non_positive_channel_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dev = Path(tmp) / 'nvme1'; dev.mkdir()
            hw = Path(tmp) / 'hwmon0'; hw.mkdir(); (hw / 'device').symlink_to(dev)
            fields = {'name': 'nvme', 'temp1_input': '37850', 'temp1_label': 'Composite',
                      'temp3_input': '-150', 'temp3_label': 'Sensor 2'}
            for name, value in fields.items(): (hw / name).write_text(value)
            with patch.object(serve, 'HWMON', Path(tmp)):
                rows = {s['id']: s for s in serve.sensors()}
            self.assertEqual(rows['nvme1.composite']['value'], 37.85)
            self.assertIsNone(rows['nvme1.sensor_2']['value'])
            self.assertEqual(rows['nvme1.sensor_2']['quality'], 'unavailable')

    def test_amdgpu_ppt_on_apu_is_labelled_package_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp) / 'hwmon0'; hw.mkdir()
            for name, value in {'name': 'amdgpu', 'temp1_input': '45000', 'temp1_label': 'edge',
                                'power1_input': '28177000', 'power1_label': 'PPT'}.items():
                (hw / name).write_text(value)
            for cpu, label in [('AMD Ryzen 5 7640HS w/ Radeon 760M Graphics', 'APU 封装功耗'), ('AMD Ryzen 9 7950X 16-Core Processor', '功耗')]:
                with self.subTest(cpu=cpu), patch.object(serve, 'HWMON', Path(tmp)), patch.object(serve, 'cpu_model', lambda: cpu):
                    power = next(s for s in serve.sensors() if s['id'] == 'gpu.power')
                    self.assertEqual(power['label'], label)
                    self.assertAlmostEqual(power['value'], 28.177)

    def test_multiple_acpi_zones_do_not_share_sensor_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(2):
                hw = Path(tmp) / f'hwmon{i}'; hw.mkdir()
                (hw / 'name').write_text('acpitz'); (hw / 'temp1_input').write_text('20000')
            with patch.object(serve, 'HWMON', Path(tmp)):
                rows = serve.sensors()
            self.assertEqual(len({s['id'] for s in rows}), 2)
            self.assertTrue(all(s['quality'] == 'unverified' for s in rows))

    def test_duplicate_driver_names_get_distinct_ids_and_single_ones_keep_theirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(2):
                hw = Path(tmp) / f'hwmon{i}'; hw.mkdir()
                (hw / 'name').write_text('mlx5'); (hw / 'temp1_input').write_text('49000'); (hw / 'temp1_label').write_text('asic')
            hw = Path(tmp) / 'hwmon2'; hw.mkdir()
            (hw / 'name').write_text('mt7925_phy0'); (hw / 'temp1_input').write_text('42000')
            with patch.object(serve, 'HWMON', Path(tmp)):
                rows = serve.sensors()
            ids = [s['id'] for s in rows]
            self.assertEqual(len(set(ids)), 3)
            self.assertIn('mt7925_phy0.temp1', ids)
            mlx = [s for s in rows if s['source']['driver'] == 'mlx5']
            self.assertTrue(all(s['id'].startswith('mlx5.') and s['id'] != 'mlx5.asic' for s in mlx))
            self.assertNotEqual(mlx[0]['device'], mlx[1]['device'])
