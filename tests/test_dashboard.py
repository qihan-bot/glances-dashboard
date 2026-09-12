"""Browser regressions. Requires Playwright and its Chromium installation."""
import threading
import unittest
from http.server import ThreadingHTTPServer

from test_server import Handler
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None


def metrics(name, cpu):
    return {
        'system': {'hostname': name, 'hr_name': 'Linux'},
        'quicklook': {'cpu_name': 'Test CPU', 'cpu_hz_current': 2e9},
        'core': {'phys': 2, 'log': 4}, 'uptime': '1:00', 'ip': {'address': name},
        'cpu': {'total': cpu, 'user': cpu, 'system': 0, 'iowait': 0},
        'mem': dict(percent=25, total=1024, used=256, available=768, cached=0),
        'memswap': dict(percent=0, total=0, used=0),
        'load': dict(min1=1, min5=1, min15=1),
        'network': [dict(interface_name='eth-test', bytes_all_gauge=1)],
        'diskio': [dict(disk_name='disk-test')], 'fs': [], 'sensors': [],
        'processcount': dict(thread=0, total=0, running=0, sleeping=0),
        'processlist': [], 'programlist': [], 'percpu': [dict(total=cpu)],
    }


@unittest.skipIf(async_playwright is None, 'Install playwright for browser checks')
class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_theme_cycle_recovers_invalid_storage_and_works_without_storage(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                await page.route('**/hosts.json', lambda r: r.fulfill(json={'hosts': []}))
                await page.route('**/power.json', lambda r: r.fulfill(json={'enabled': False}))
                await page.route('**/api/4/all', lambda r: r.fulfill(json=metrics('Alpha', 11)))
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                for saved in ('undefined', 'auto', 'invalid'):
                    await page.evaluate("v => localStorage.setItem('nexus_theme', v)", saved)
                    await page.reload()
                    self.assertEqual(await page.locator('#theme').inner_text(), '夜航模式')
                    self.assertEqual(await page.evaluate("localStorage.getItem('nexus_theme')"), 'dark')
                    for expected, label in [('light', '日光模式'), ('dark', '夜航模式')] * 3:
                        await page.locator('#theme').click()
                        self.assertEqual(await page.evaluate('document.documentElement.dataset.theme'), expected)
                        self.assertEqual(await page.locator('#theme').inner_text(), label)
                await page.evaluate("() => {Storage.prototype.setItem = Storage.prototype.getItem = () => {throw new Error('Storage blocked')};}")
                for expected, label in [('light', '日光模式'), ('dark', '夜航模式')] * 3:
                    await page.locator('#theme').click()
                    self.assertEqual(await page.evaluate('document.documentElement.dataset.theme'), expected)
                    self.assertEqual(await page.locator('#theme').inner_text(), label)
                await browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    async def test_switching(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                errors, calls = [], []
                page.on('pageerror', lambda e: errors.append(str(e)))
                recovering = False

                async def route(r):
                    nonlocal recovering
                    url = r.request.url
                    if url.endswith('/hosts.json'):
                        await r.fulfill(json={'hosts': [
                            dict(name='Local Friendly', addr='127.0.0.1', dport=server.server_port),
                            dict(name='Alpha', addr='alpha.test'),
                            dict(name='Beta', addr='beta.test', gport=62008, dport=62009),
                            dict(name='Offline', addr='offline.test'),
                        ]})
                    elif '/api/4/all' in url:
                        calls.append(url)
                        if 'offline.test' in url and not recovering:
                            await r.fulfill(status=503, body='Offline')
                        else:
                            name = 'Beta' if 'beta.test' in url else 'Alpha'
                            await r.fulfill(json=metrics(name, 73 if name == 'Beta' else 11))
                    elif url.endswith('/sensors.json'):
                        await r.fulfill(status=404, body='No sidecar')
                    else:
                        await r.continue_()

                await page.route('**/*', route)
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                await page.wait_for_function("last !== null")
                self.assertFalse(await page.locator('#hostModal').is_visible())
                self.assertEqual(await page.locator('#host option').count(), 4)
                self.assertIn('Local Friendly', await page.locator('#host option').first.inner_text())
                self.assertEqual(await page.locator('.node-card strong').first.inner_text(), 'Local Friendly')
                await page.locator('.node-card[data-host="beta.test:62008:62009"]').click()
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                self.assertEqual(await page.evaluate('hist.map(s => s.cpu)'), [73])
                self.assertTrue(any('beta.test:62008' in url for url in calls))
                await page.reload()
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                self.assertEqual(await page.locator('#host').input_value(), 'beta.test:62008:62009')
                self.assertEqual(await page.evaluate('[interval, range]'), [5000, 1800])
                self.assertEqual(await page.locator('#ivl .on').get_attribute('data-v'), '5000')
                self.assertEqual(await page.locator('#range .on').get_attribute('data-v'), '1800')
                await page.locator('#ivl [data-v="2000"]').click()

                # Delay decoded JSON, then switch to B while an A response is pending.
                await page.evaluate('''() => {
                  const realFetch = window.fetch;
                  window.fetch = async (...args) => {
                    const r = await realFetch(...args);
                    if (String(args[0]).includes('alpha.test') && String(args[0]).includes('/api/')) {
                      const read = r.json.bind(r);
                      r.json = async () => { const d = await read(); await new Promise(resolve => setTimeout(resolve, 800)); return d; };
                    }
                    return r;
                  };
                }''')
                await page.locator('#host').select_option('alpha.test:61208:61209')
                await page.wait_for_timeout(100)
                await page.locator('#host').select_option('beta.test:62008:62009')
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                await page.wait_for_timeout(1000)
                self.assertTrue(await page.evaluate('hist.every(s => s.cpu === 73)'))
                before = len(calls)
                await page.wait_for_timeout(2100)
                self.assertGreater(len(calls), before)

                await page.locator('#host').select_option('offline.test:61208:61209')
                await page.wait_for_function("document.querySelector('#stat').textContent.includes('连接失败')")
                self.assertEqual(await page.evaluate('hist.length'), 0)
                self.assertEqual(await page.locator('#tiles .value').first.inner_text(), '–')
                recovering = True
                await page.wait_for_function("document.querySelector('#stat').textContent.startsWith('更新于')")

                await page.locator('#hostmgr').click()
                await page.locator('#hostText').fill('Invalid, host.test, 70000')
                await page.locator('#hostSave').click()
                self.assertTrue(await page.locator('#hostError').inner_text())
                await page.locator('#hostText').fill('Named with spaces, beta.test, 63008, 63009')
                await page.locator('#hostSave').click()
                self.assertEqual(await page.locator('#host option').count(), 5)
                await page.locator('#host').select_option('beta.test:63008:63009')
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                self.assertTrue(any('beta.test:63008' in url for url in calls))
                self.assertEqual(await page.locator('.node-card.on').count(), 1)
                self.assertEqual(await page.locator('.node-card.on').get_attribute('aria-pressed'), 'true')
                self.assertIn('73.0%', await page.locator('.chart .legend').first.inner_text())
                self.assertEqual(await page.evaluate('document.documentElement.dataset.theme'), 'dark')
                await page.locator('#theme').click()
                self.assertEqual(await page.evaluate('document.documentElement.dataset.theme'), 'light')
                await page.reload()
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                self.assertEqual(await page.evaluate('document.documentElement.dataset.theme'), 'light')
                for width in (390, 768, 1440):
                    await page.set_viewport_size({'width': width, 'height': 900})
                    self.assertFalse(await page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                self.assertEqual(errors, [])
                await browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
