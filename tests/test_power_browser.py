"""Power UI tests use intercepted control requests, never real shutdowns."""
import threading
import unittest
from http.server import ThreadingHTTPServer
from test_server import Handler
from test_dashboard import async_playwright, metrics

@unittest.skipIf(async_playwright is None, 'Install playwright for browser checks')
class PowerBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_target_switch_offline_wake_and_failure(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever); thread.start()
        try:
            async with async_playwright() as p:
                browser=await p.chromium.launch(); page=await browser.new_page()
                calls=[]; errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                hosts=[{'name':'Alpha','addr':'alpha.test'},{'name':'Offline','addr':'offline.test'}]
                async def route(r):
                    url=r.request.url
                    if url.endswith('/hosts.json'): await r.fulfill(json={'hosts':hosts})
                    elif url.endswith('/power.json'):
                        addr='alpha.test' if 'alpha.test' in url else '127.0.0.1'
                        if 'offline.test' in url: await r.fulfill(status=503,json={}); return
                        await r.fulfill(json={'addr':addr,'enabled':True,'shutdown':True,'reboot':True,'password_required':False,
                                             'hosts':[dict(h,wake_enabled=True,wake_note='Wake requires power.',**({'wake_dashboard':'alpha.test'} if h['addr']=='offline.test' and addr=='127.0.0.1' else {})) for h in hosts]})
                    elif url.endswith('/api/power'):
                        calls.append((url,r.request.post_data_json))
                        if r.request.post_data_json['action']=='shutdown':
                            await r.fulfill(status=503,json={'error':'permission unavailable'})
                        elif r.request.post_data_json['action']=='reboot':
                            await r.fulfill(json={'ok':True,'message':'reboot scheduled in one minute'})
                        else: await r.fulfill(json={'ok':True,'message':'wake sent; not yet online'})
                    elif '/api/4/all' in url:
                        if 'offline.test' in url: await r.fulfill(status=503,json={})
                        else: await r.fulfill(json=metrics('Alpha',12))
                    elif url.endswith('/sensors.json'): await r.fulfill(json={'sensors':[]})
                    else: await r.continue_()
                await page.route('**/*',route)
                await page.goto(f'http://127.0.0.1:{server.server_port}/')
                await page.locator('#host').select_option('alpha.test:61208:61209')
                await page.wait_for_function("!document.getElementById('shutdownBtn').disabled")
                await page.locator('#shutdownBtn').click()
                self.assertIn('alpha.test',await page.locator('#powerTarget').inner_text())
                self.assertEqual(calls,[])
                await page.locator('#powerClose').click()
                self.assertEqual(calls,[])
                await page.locator('#shutdownBtn').click()
                await page.locator('#powerConfirm').click()
                await page.wait_for_function("document.getElementById('powerError').textContent === 'permission unavailable'")
                self.assertEqual(calls[0][1]['target'],'alpha.test')
                await page.locator('#powerClose').click()
                await page.locator('#rebootBtn').click()
                self.assertEqual(await page.locator('#powerTitle').inner_text(), '确认重启')
                self.assertIn('alpha.test', await page.locator('#powerTarget').inner_text())
                self.assertIn('1 分钟后重启', await page.locator('#powerExplanation').inner_text())
                self.assertEqual(len(calls), 1)
                await page.locator('#powerConfirm').click()
                await page.wait_for_function("document.getElementById('powerMessage').textContent.includes('reboot scheduled')")
                self.assertEqual(calls[1][1]['action'], 'reboot')
                self.assertEqual(calls[1][1]['target'], 'alpha.test')
                self.assertEqual(calls[1][0], 'http://alpha.test:61209/api/power')
                await page.locator('#rebootBtn').click()
                await page.evaluate("hostSel.value='offline.test:61208:61209';hostSel.dispatchEvent(new Event('change'))")
                self.assertFalse(await page.locator('#powerModal').is_visible())
                await page.wait_for_function("powerConfig?.target.addr === 'offline.test'")
                self.assertTrue(await page.locator('#shutdownBtn').is_disabled())
                self.assertTrue(await page.locator('#rebootBtn').is_disabled())
                self.assertFalse(await page.locator('#wakeBtn').is_disabled())
                await page.locator('#wakeBtn').click(); await page.locator('#powerConfirm').click()
                await page.wait_for_function("document.getElementById('powerMessage').textContent.includes('wake sent')")
                self.assertIn('alpha.test',calls[2][0]); self.assertEqual(calls[2][1]['target'],'offline.test')
                self.assertFalse(errors)
                await browser.close()
        finally:
            server.shutdown();server.server_close();thread.join()
