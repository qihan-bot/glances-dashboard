"""Saturation/error tiles, derived rates, selector filtering and the no-PSI fallback."""
import threading
import unittest
from http.server import ThreadingHTTPServer
from test_server import Handler
from test_dashboard import async_playwright, metrics


def host_metrics(name):
    d = metrics(name, 12)
    d['network'] = [dict(interface_name='eth-test', bytes_all_gauge=9, speed=1000 * 1048576, bytes_recv_rate_per_sec=1000, bytes_sent_rate_per_sec=0),
                    dict(interface_name='cali0123', bytes_all_gauge=99, speed=10000 * 1048576)]
    d['diskio'] = [dict(disk_name='nvme0n1', read_time=20, read_count=10, write_time=0, write_count=0),
                   dict(disk_name='nvme0n1p1'), dict(disk_name='dm-0')]
    d['processlist'] = [dict(name='a', status='Z', cpu_percent=0, memory_info=dict(rss=1)), dict(name='b', status='D', cpu_percent=0, memory_info=dict(rss=1))]
    d['programlist'] = [dict(name='python', nprocs=3, cpu_percent=12, memory_info=dict(rss=3 << 30)), dict(name='solo', nprocs=1, cpu_percent=1, memory_info={})]
    return d


def system(n):
    some = lambda v: {'some': {'avg10': v, 'avg60': v / 2, 'avg300': 0}, 'full': {'avg10': 0, 'avg60': 0, 'avg300': 0}}
    return {'mono': 100 + 5 * n, 'page_size': 4096, 'psi': {'cpu': some(1.76), 'memory': some(0), 'io': some(30)},
            'vm': {'pswpin': 10 + 5 * n, 'pswpout': 7, 'pgmajfault': 0, 'oom_kill': 0},
            'tcp': {'out_segs': 1000 * n, 'retrans_segs': 10 * n},
            'net': {'eth-test': {'rx_errs': 0, 'rx_drop': 5 * n, 'tx_errs': 0, 'tx_drop': 0}},
            'failed_units': [{'unit': 'backup.service', 'scope': 'user', 'description': 'Backup'}], 'reboot_required': False}


@unittest.skipIf(async_playwright is None, 'Install playwright for browser checks')
class HealthBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_tiles_rates_filters_and_fallback(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(); page = await browser.new_page()
                errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                polls = 0
                async def route(r):
                    nonlocal polls
                    url = r.request.url
                    if url.endswith('/hosts.json'): await r.fulfill(json={'hosts': [dict(name='Alpha', addr='alpha.test'), dict(name='Beta', addr='beta.test')]})
                    elif url.endswith('/power.json'): await r.fulfill(json={'enabled': False})
                    elif url.endswith('/api/4/all'): await r.fulfill(json=host_metrics('Beta' if 'beta.test' in url else 'Alpha'))
                    elif url.endswith('/sensors.json'):
                        if 'beta.test' in url: await r.fulfill(json={'sensors': []})
                        else: polls += 1; await r.fulfill(json={'sensors': [], 'system': system(polls)})
                    else: await r.continue_()
                await page.route('**/*', route)
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                await page.wait_for_function("last?.system.hostname === 'Alpha'")
                await page.evaluate('poll()'); await page.wait_for_function('hist.length >= 2')
                tile = lambda code: page.locator(f'.tile[data-code={code}]').inner_text()
                self.assertEqual(await page.locator('#iface option').all_inner_texts(), ['全部（合计）', 'eth-test'])
                self.assertEqual(await page.locator('#disk option').all_inner_texts(), ['nvme0n1'])
                psi = await tile('PSI')
                self.assertIn('资源压力', psi); self.assertIn('30.0%', psi); self.assertIn('▲ 30.0%', psi); self.assertNotIn('● 1.8%', psi)
                self.assertEqual(await page.evaluate("pressureChart.opts.title"), '资源压力 · PSI')
                health = await tile('HEALTH')
                self.assertIn('1 项异常', health); self.assertIn('backup（用户）', health); self.assertIn('僵尸 1 · 不可中断 1', health)
                net = await tile('NET')
                self.assertIn('链路 1 Gb/s', net); self.assertIn('TCP 重传 1.00%', net); self.assertIn('丢弃 1.00/s', net)
                self.assertIn('换入 4.0 KB/s', await tile('MEM'))
                self.assertIn('延迟 读 2.00 ms · 写 –', await tile('IO'))
                self.assertEqual(await page.locator('#topcpu .n').first.inner_text(), 'python ×3')
                self.assertEqual(await page.locator('#topmem .n').nth(1).inner_text(), 'solo')
                await page.locator('#host').select_option('beta.test:61208:61209')
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                self.assertIn('负载', await tile('PSI'))
                self.assertEqual(await page.evaluate("pressureChart.opts.title"), '系统负载')
                health = await tile('HEALTH')
                self.assertIn('未采集', health); self.assertNotIn('backup', health)
                self.assertNotIn('TCP 重传', await tile('NET'))
                self.assertFalse(errors)
                await browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
