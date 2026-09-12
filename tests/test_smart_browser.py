"""SATA charts, SMART warnings, stale reads, and cross-host isolation."""
import threading
import unittest
from http.server import ThreadingHTTPServer
from test_server import Handler
from test_dashboard import async_playwright, metrics
from test_smart import fixture
import smart


@unittest.skipIf(async_playwright is None, 'Install playwright for browser checks')
class SmartBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_sata_health_stale_recovery_and_host_switch(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(); page = await browser.new_page()
                errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                fail = False
                disk = smart.parse(fixture(), '/dev/sda')
                snapshot = {'enabled': True, 'stale': False, 'disks': [disk],
                            'updated_at': '2026-09-11T14:00:00+00:00', 'interval_seconds': 60}

                async def route(r):
                    url = r.request.url
                    if url.endswith('/hosts.json'):
                        await r.fulfill(json={'hosts': [{'name': 'Alpha', 'addr': 'alpha.test'}, {'name': 'Beta', 'addr': 'beta.test'}]})
                    elif url.endswith('/power.json'):
                        await r.fulfill(json={'enabled': False})
                    elif url.endswith('/api/4/all'):
                        await r.fulfill(json=metrics('Beta' if 'beta.test' in url else 'Alpha', 12))
                    elif url.endswith('/sensors.json'):
                        if 'beta.test' in url:
                            await r.fulfill(json={'sensors': []})
                        elif fail:
                            await r.fulfill(status=503, body='Unavailable')
                        else:
                            await r.fulfill(json={'sensors': smart.temperature_sensors(snapshot), 'storage': snapshot})
                    else:
                        await r.continue_()

                await page.route('**/*', route)
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                await page.wait_for_function("document.querySelector('#smartState').textContent === '需关注'")
                self.assertIn('SMART', await page.locator('#smartPanel').inner_text())
                self.assertIn('40 °C', await page.locator('#smartValues').inner_text())
                self.assertIn('型号未匹配数据库', await page.locator('#smartNote').inner_text())
                self.assertEqual(await page.locator('.disk-temperature-chart [data-disk-id="sda"]').count(), 1)
                fail = True
                await page.evaluate('poll()')
                self.assertEqual(await page.locator('#smartState').inner_text(), '未更新')
                self.assertIn('40 °C', await page.locator('#smartValues').inner_text())
                self.assertIn('上次成功读取值', await page.locator('#smartTime').inner_text())
                fail = False
                await page.evaluate('poll()')
                self.assertEqual(await page.locator('#smartState').inner_text(), '需关注')
                disk['temperature_status'] = 'unverified'
                disk['statistics'] = {'bytes_read': 8 * 1024**3, 'bytes_written': 16 * 1024**3}
                await page.evaluate('poll()')
                self.assertEqual(await page.locator('.disk-temperature-chart').count(), 0)
                self.assertIn('固件温度 · 未核实', await page.locator('#smartValues').inner_text())
                self.assertIn('累计读取 · ATA 日志', await page.locator('#smartValues').inner_text())
                self.assertIn('05 · 厂商原始值', await page.locator('#smartValues').inner_text())
                self.assertIn('非零值不能直接等同坏扇区', await page.locator('#smartNote').inner_text())
                await page.set_viewport_size({'width': 390, 'height': 844})
                self.assertFalse(await page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                await page.locator('#host').select_option('beta.test:61208:61209')
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                self.assertTrue(await page.locator('#smartPanel').is_hidden())
                self.assertEqual(await page.locator('.disk-temperature-chart').count(), 0)
                self.assertFalse(errors)
                await browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()
