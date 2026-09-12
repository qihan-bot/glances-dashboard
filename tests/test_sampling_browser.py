"""Never chart firmware placeholders or substitute a failed source."""
import threading
import unittest
from http.server import ThreadingHTTPServer
from test_server import Handler
from test_dashboard import async_playwright, metrics


@unittest.skipIf(async_playwright is None, 'Install playwright for browser checks')
class SamplingBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_channels_failure_gap_and_recovery(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(); page = await browser.new_page()
                errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                fail = False
                sensors = [dict(id='cpu.tctl', device_id='cpu', device='CPU', group='core', label='Tctl', value=91, quality='reported', warn=None, crit=None),
                           dict(id='acpitz.zone0.temp1', device_id='acpitz.zone0', device='ACPI · 固件热区（未核实）', group='diagnostic', label='温度', value=20, quality='unverified', warn=None, crit=None)]
                async def route(r):
                    url = r.request.url
                    if url.endswith('/hosts.json'): await r.fulfill(json={'hosts': []})
                    elif url.endswith('/power.json'): await r.fulfill(json={'enabled': False})
                    elif url.endswith('/api/4/all'):
                        d = metrics('Test', 12)
                        d['sensors'] = [dict(type='temperature_core', label='bad-fallback', value=999)]
                        await r.fulfill(json=d)
                    elif url.endswith('/sensors.json'):
                        if fail: await r.fulfill(status=503, body='Unavailable')
                        else: await r.fulfill(json={'sensors': sensors})
                    else: await r.continue_()
                await page.route('**/*', route)
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                await page.wait_for_function('hist.length > 0')
                self.assertEqual(await page.evaluate('hist.at(-1).temp'), {'cpu.tctl': 91})
                self.assertIn('20.0 °C', await page.locator('#tbody').inner_text())
                self.assertIn('未核实 · 不绘图', await page.locator('#tbody').inner_text())
                self.assertNotIn('ACPI', await page.evaluate("tempCharts.map(c=>c.legend.textContent).join()"))
                fail = True; await page.evaluate('poll()')
                self.assertEqual(await page.evaluate('hist.at(-1).temp'), {})
                self.assertIn('未更新', await page.locator('#sensorStatus').inner_text())
                self.assertIn('91.0 °C（上次）', await page.locator('#tbody').inner_text())
                await page.evaluate('tempCharts[0].renderTable()')
                self.assertIn('未更新', await page.evaluate('tempCharts[0].table.textContent'))
                fail = False; sensors[0]['value'] = 89; await page.evaluate('poll()')
                self.assertEqual(await page.evaluate("hist.at(-1).temp['cpu.tctl']"), 89)
                self.assertNotIn('（上次）', await page.locator('#tbody').inner_text())
                sensors[0].update(value=None, quality='unavailable')
                await page.evaluate('poll()')
                self.assertEqual(await page.evaluate('hist.at(-1).temp'), {})
                self.assertIn('89.0 °C（上次）', await page.locator('#tbody').inner_text())
                sensors[0].update(value=88, quality='reported')
                await page.evaluate('poll()')
                self.assertEqual(await page.evaluate("hist.at(-1).temp['cpu.tctl']"), 88)
                await page.set_viewport_size({'width': 390, 'height': 844})
                self.assertFalse(await page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                self.assertFalse(errors)
                await browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()
