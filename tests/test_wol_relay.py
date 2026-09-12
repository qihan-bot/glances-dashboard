"""Verify restricted relay and dashboard forwarding via real local HTTP."""
import json
import threading
import unittest
import urllib.error
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import power
import wol_relay

class RelayTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'clients':['127.0.0.1'],'hosts':[{'addr':'192.0.2.29','mac':'02:00:00:00:00:29','broadcast':'192.0.2.255'}]}
        self.config_patch=patch('wol_relay.config',return_value=self.cfg);self.config_patch.start()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),wol_relay.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        wol_relay.LAST_WAKE.clear();power.LAST_ACTION.clear()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.config_patch.stop()

    def request(self,body,headers=None):
        conn=HTTPConnection(*self.server.server_address)
        conn.request('POST','/wake',json.dumps(body),headers or {'Content-Type':'application/json','X-Monitor-Relay':'1'})
        response=conn.getresponse();code=response.status;response.read();conn.close();return code

    @patch('wol_relay.send_wake')
    def test_forwarding_acknowledges_only_fixed_target(self, send):
        host=dict(self.cfg['hosts'][0],wake_relay={'url':f'http://127.0.0.1:{self.server.server_port}/wake'})
        power.send_wake(host)
        send.assert_called_once_with({'mac':'02:00:00:00:00:29','broadcast':'192.0.2.255'})
        host['addr']='192.0.2.30'
        with self.assertRaises(urllib.error.HTTPError):power.send_wake(host)
        self.assertEqual(send.call_count,1)

    @patch('wol_relay.send_wake')
    def test_rejects_shutdown_browser_requests_and_wrong_source(self, send):
        self.assertEqual(self.request({'action':'shutdown','target':'192.0.2.29'}),400)
        self.assertEqual(self.request({'action':'wake','target':'192.0.2.29'}, {'Content-Type':'application/json','X-Monitor-Relay':'1','Origin':'http://evil.test'}),403)
        self.cfg['clients']=[]
        self.assertEqual(self.request({'action':'wake','target':'192.0.2.29'}),403)
        send.assert_not_called()

    @patch('wol_relay.send_wake',side_effect=OSError('network unavailable'))
    def test_relay_failure_is_not_reported_as_success(self, send):
        host=dict(self.cfg['hosts'][0],wake_enabled=True,wake_relay={'url':f'http://127.0.0.1:{self.server.server_port}/wake'})
        code,data=power.execute({'access':'lan','addr':'192.0.2.10','hosts':[host]}, {'action':'wake','target':host['addr']})
        self.assertEqual(code,503);self.assertNotIn('ok',data)

if __name__=='__main__':unittest.main()
