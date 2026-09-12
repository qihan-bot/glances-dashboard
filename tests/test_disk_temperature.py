"""Merged drive temperature chart behavior through browser controls."""
import threading
import unittest
from http.server import ThreadingHTTPServer
from test_server import Handler
from test_dashboard import async_playwright, metrics


def drive_sensors(ids):
    return [dict(id=f'{disk}.{name}', device_id=disk, device=f'{disk} · Test SSD {disk}',
                 group='nvme',label=label,value=30+i*10+j,warn=80,crit=90)
            for i,disk in enumerate(ids)
            for j,(name,label) in enumerate([('composite','复合'),('sensor_1','控制器'),('sensor_2','闪存')])]


@unittest.skipIf(async_playwright is None, 'Install playwright for browser checks')
class DiskTemperatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_chart_tags_single_multi_and_host_reset(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            async with async_playwright() as p:
                browser=await p.chromium.launch();page=await browser.new_page();errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                async def route(r):
                    url=r.request.url
                    if url.endswith('/hosts.json'):
                        await r.fulfill(json={'hosts':[dict(name='Alpha',addr='alpha.test'),dict(name='Beta',addr='beta.test')]})
                    elif url.endswith('/power.json'):await r.fulfill(json={'enabled':False})
                    elif url.endswith('/api/4/all'):await r.fulfill(json=metrics('Beta' if 'beta.test' in url else 'Alpha',12))
                    elif url.endswith('/sensors.json'):
                        await r.fulfill(json={'sensors':drive_sensors(['nvme2'] if 'beta.test' in url else ['nvme0','nvme1'])})
                    else:await r.continue_()
                await page.route('**/*',route)
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                chart=page.locator('.disk-temperature-chart')
                await chart.wait_for()
                self.assertEqual(await chart.count(),1)
                self.assertEqual(await chart.locator('.disk-tag.on').count(),2)
                self.assertEqual(await chart.locator('.legend > span').count(),6)
                nvme0=chart.locator('[data-disk-id="nvme0"]');nvme1=chart.locator('[data-disk-id="nvme1"]')
                await chart.locator('[data-mode="single"]').click()
                self.assertEqual(await chart.locator('.disk-tag.on').count(),1)
                await nvme1.click()
                self.assertEqual(await nvme1.get_attribute('aria-pressed'),'true')
                self.assertNotIn('nvme0',await chart.locator('.legend').inner_text())
                self.assertEqual(await chart.locator('.legend > span').count(),3)
                await chart.locator('[data-mode="multiple"]').click();await nvme0.click()
                self.assertEqual(await chart.locator('.legend > span').count(),6)
                await nvme1.click();await nvme0.click()
                self.assertEqual(await chart.locator('.disk-tag.on').count(),1)
                await chart.locator('.disk-all').click()
                self.assertEqual(await chart.locator('.disk-tag.on').count(),2)
                await chart.locator('summary').click()
                await page.wait_for_function("document.querySelector('.disk-temperature-chart table').rows.length > 1")
                self.assertEqual(await chart.locator('table th').count(),7)
                await page.set_viewport_size({'width':390,'height':844})
                self.assertFalse(await page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                await page.locator('#host').select_option('beta.test:61208:61209')
                await page.wait_for_function("last?.system.hostname === 'Beta'")
                self.assertEqual(await chart.count(),1)
                self.assertEqual(await chart.locator('.disk-tag').count(),1)
                self.assertEqual(await chart.locator('.disk-tag.on').get_attribute('data-disk-id'),'nvme2')
                self.assertNotIn('nvme0',await chart.inner_text())
                self.assertFalse(errors)
                await browser.close()
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
