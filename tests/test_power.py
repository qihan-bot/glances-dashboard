"""Exercise live HTTP boundaries without shutting down any machine."""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch, Mock
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import power
from serve import Handler


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.cfg = {'access': 'lan', 'addr': '127.0.0.1', 'shutdown': True,
                    'hosts': [{'addr':'127.0.0.1', 'dport':self.server.server_port,
                               'wake_enabled':True, 'mac':'02:00:00:00:00:29',
                               'broadcast':'192.0.2.255'}]}
        self.config_patch = patch('power.config', return_value=self.cfg)
        self.config_patch.start()
        power.LAST_ACTION.clear()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.config_patch.stop()

    def request(self, body, headers=None, method='POST', path='/api/power'):
        h = {'Origin':self.origin, 'Content-Type':'application/json', 'X-Monitor-Action':'1'}
        if headers is not None: h = headers
        conn = HTTPConnection(*self.server.server_address)
        conn.request(method, path, json.dumps(body), h)
        response = conn.getresponse()
        code, data = response.status, json.loads(response.read())
        conn.close()
        return code, data

    @patch('power.subprocess.run')
    @patch('power.send_wake')
    def test_rejects_cross_origin_rebinding_forms_and_unknown_target(self, wake, run):
        for headers in [{}, {'Origin':'http://evil.test','Content-Type':'application/json','X-Monitor-Action':'1'},
                        {'Origin':self.origin,'Content-Type':'application/json'},
                        {'Origin':self.origin,'Content-Type':'application/json','X-Monitor-Action':'1','Host':'evil.test'}]:
            self.assertEqual(self.request({'action':'shutdown','target':'127.0.0.1'}, headers)[0],403)
        self.assertEqual(self.request({'action':'shutdown','target':'192.0.2.1'})[0],400)
        self.assertEqual(self.request({'action':'shell','target':'127.0.0.1'})[0],400)
        self.assertEqual(self.request([])[0],400)
        run.assert_not_called(); wake.assert_not_called()

    @patch('power.subprocess.run', return_value=Mock(returncode=0))
    def test_schedule_cancel_and_non_mutating_check(self, run):
        for action in ['check','shutdown','cancel']:
            self.assertEqual(self.request({'action':action,'target':'127.0.0.1'})[0],200)
        self.assertEqual(run.call_args_list[0].args[0], ['/usr/bin/sudo','-n','-l','/usr/sbin/shutdown','-h','+1'])
        self.assertEqual(run.call_args_list[1].args[0], ['/usr/bin/sudo','-n','-l','/usr/sbin/shutdown','-c'])
        self.assertEqual(run.call_args_list[2].args[0], ['/usr/bin/sudo','-n','/usr/sbin/shutdown','-h','+1'])
        self.assertEqual(run.call_args_list[3].args[0], ['/usr/bin/sudo','-n','/usr/sbin/shutdown','-c'])
        self.assertEqual(self.request({'action':'shutdown','target':'127.0.0.1'})[0],429)

    @patch('power.subprocess.run', return_value=Mock(returncode=1))
    def test_permission_failure_is_not_success(self, run):
        self.cfg['reboot'] = True
        for action in ('shutdown', 'reboot'):
            code, data = self.request({'action':action,'target':'127.0.0.1'})
            self.assertEqual(code,503); self.assertNotIn('ok',data)

    @patch('power.subprocess.run', return_value=Mock(returncode=0))
    def test_reboot_is_opt_in_local_delayed_and_cancellable(self, run):
        body = {'action': 'reboot', 'target': '127.0.0.1'}
        self.assertFalse(power.status(self.cfg)['reboot'])
        self.assertEqual(self.request(body)[0], 409)
        self.cfg['reboot'] = True
        self.assertTrue(power.status(self.cfg)['reboot'])
        self.cfg['hosts'].append({'addr': '192.0.2.1'})
        self.assertEqual(self.request(dict(body, target='192.0.2.1'))[0], 400)
        self.assertEqual(self.request(body, {})[0], 403)
        self.cfg['access'] = 'disabled'
        self.assertEqual(self.request(body)[0], 403)
        self.cfg['access'] = 'lan'
        run.assert_not_called()
        self.assertEqual(self.request({'action': 'check', 'target': '127.0.0.1'})[0], 200)
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ['/usr/bin/sudo', '-n', '-l', '/usr/sbin/shutdown', *args]
            for args in (['-h', '+1'], ['-r', '+1'], ['-c'])])
        run.reset_mock()
        code, data = self.request(dict(body, command='reboot now'))
        self.assertEqual(code, 200)
        self.assertIn('1 分钟后重启', data['message'])
        self.assertEqual(run.call_args.args[0], ['/usr/bin/sudo', '-n', '/usr/sbin/shutdown', '-r', '+1'])
        self.assertEqual(self.request(body)[0], 429)
        self.assertEqual(self.request({'action':'cancel','target':'127.0.0.1'})[0], 200)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/sudo', '-n', '/usr/sbin/shutdown', '-c'])

    @patch('power.send_wake')
    def test_wake_uses_only_configured_target(self, wake):
        self.assertEqual(self.request({'action':'wake','target':'127.0.0.1','mac':'bad'})[0],200)
        wake.assert_called_once_with(self.cfg['hosts'][0])

    @patch('power.socket.socket')
    def test_exact_magic_packet(self, sock):
        power.send_wake(self.cfg['hosts'][0])
        packet = b'\xff'*6 + bytes.fromhex('04d9f5f6537f')*16
        sender = sock.return_value.__enter__.return_value
        self.assertEqual(sender.sendto.call_count,3)
        sender.sendto.assert_called_with(packet,('192.0.2.255',9))

    @patch('power.subprocess.run')
    def test_disabled_and_password_fail_closed(self, run):
        self.cfg['access']='disabled'
        self.assertEqual(self.request({'action':'shutdown','target':'127.0.0.1'})[0],403)
        self.cfg['access']='password'
        self.assertEqual(self.request({'action':'shutdown','target':'127.0.0.1','password':'wrong'})[0],403)
        run.assert_not_called()


if __name__ == '__main__': unittest.main()
