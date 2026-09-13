"""nvidia-smi parsing, threshold handling, and failure behaviour."""
import subprocess
import unittest

import nvgpu

QUERY_GB10 = '0, NVIDIA GB10, 44, 4.62\n'
QUERY_TWO = '0, NVIDIA RTX A6000, 61, 78.10\n1, NVIDIA RTX A6000, [N/A], [N/A]\n'
LIMITS_GB10 = """GPU 0000000F:01:00.0
    Temperature
        GPU Current Temp                               : 44 C
        GPU T.Limit Temp                               : 52 C
        GPU Shutdown T.Limit Temp                      : N/A
        GPU Slowdown T.Limit Temp                      : N/A
        GPU Max Operating T.Limit Temp                 : 0 C
"""
LIMITS_CLASSIC = """GPU 00000000:01:00.0
    Temperature
        GPU Current Temp                               : 61 C
        GPU Shutdown Temp                              : 98 C
        GPU Slowdown Temp                              : 95 C
GPU 00000000:02:00.0
    Temperature
        GPU Shutdown Temp                              : N/A
        GPU Slowdown Temp                              : 95 C
"""


class Result:
    def __init__(self, stdout='', returncode=0):
        self.stdout, self.returncode = stdout, returncode


def runner(query, limits, fail=False):
    def run(cmd):
        if fail:
            raise subprocess.TimeoutExpired(cmd, 3)
        return Result(limits) if cmd == nvgpu.LIMITS else Result(query)
    return run


class NvgpuTests(unittest.TestCase):
    def test_parse_query_handles_na(self):
        self.assertEqual(nvgpu.parse_query(QUERY_GB10), [{'index': 0, 'name': 'NVIDIA GB10', 'temp': 44.0, 'power': 4.62}])
        two = nvgpu.parse_query(QUERY_TWO)
        self.assertEqual((two[1]['index'], two[1]['temp'], two[1]['power']), (1, None, None))
        self.assertEqual(nvgpu.parse_query('garbage\n'), [])

    def test_parse_limits_ignores_tlimit_headroom(self):
        self.assertEqual(nvgpu.parse_limits(LIMITS_GB10), [(None, None)])
        self.assertEqual(nvgpu.parse_limits(LIMITS_CLASSIC), [(95.0, 98.0), (95.0, None)])

    def test_sensors_gb10(self):
        c = nvgpu.Collector(run=runner(QUERY_GB10, LIMITS_GB10), which=lambda n: '/usr/bin/nvidia-smi')
        data = nvgpu.sensors(c)
        self.assertEqual([s['id'] for s in data], ['gpu.edge', 'gpu.power'])
        t, p = data
        self.assertEqual((t['value'], t['warn'], t['crit'], t['group'], t['quality'], t['device']),
                         (44.0, None, None, 'core', 'reported', 'GPU · NVIDIA GB10'))
        self.assertEqual((p['value'], p['unit'], p['group']), (4.62, 'W', 'power'))
        self.assertEqual(t['source']['driver'], 'nvidia-smi')

    def test_sensors_multi_gpu_with_limits(self):
        c = nvgpu.Collector(run=runner(QUERY_TWO, LIMITS_CLASSIC), which=lambda n: '/usr/bin/nvidia-smi')
        data = nvgpu.sensors(c)
        self.assertEqual([s['id'] for s in data], ['gpu.edge', 'gpu.power', 'gpu1.edge'])
        self.assertEqual((data[0]['warn'], data[0]['crit']), (95.0, 98.0))
        self.assertEqual((data[2]['value'], data[2]['quality'], data[2]['device_id']), (None, 'unavailable', 'gpu1'))

    def test_absent_nvidia_smi_reports_nothing(self):
        c = nvgpu.Collector(run=runner(QUERY_GB10, LIMITS_GB10), which=lambda n: None)
        self.assertEqual(nvgpu.sensors(c), [])

    def test_failure_marks_known_gpus_unavailable_without_repeating_values(self):
        calls = {'fail': False}

        def run(cmd):
            if calls['fail']:
                return Result('', 1)
            return Result(LIMITS_GB10) if cmd == nvgpu.LIMITS else Result(QUERY_GB10)
        c = nvgpu.Collector(run=run, which=lambda n: '/usr/bin/nvidia-smi')
        self.assertEqual(nvgpu.sensors(c)[0]['value'], 44.0)
        calls['fail'] = True
        c.next_read = 0
        data = nvgpu.sensors(c)
        self.assertEqual([s['id'] for s in data], ['gpu.edge'])
        self.assertEqual((data[0]['value'], data[0]['quality']), (None, 'unavailable'))
        self.assertIn('未更新', data[0]['note'])
        first = nvgpu.Collector(run=runner('', '', fail=True), which=lambda n: '/usr/bin/nvidia-smi')
        data = nvgpu.sensors(first)
        self.assertEqual([(s['id'], s['quality']) for s in data], [('gpu.edge', 'unavailable')])

    def test_query_is_cached_between_requests(self):
        n = {'q': 0}

        def run(cmd):
            if cmd == nvgpu.QUERY:
                n['q'] += 1
            return Result(LIMITS_GB10) if cmd == nvgpu.LIMITS else Result(QUERY_GB10)
        c = nvgpu.Collector(run=run, which=lambda n: '/usr/bin/nvidia-smi')
        nvgpu.sensors(c); nvgpu.sensors(c)
        self.assertEqual(n['q'], 1)


if __name__ == '__main__':
    unittest.main()
