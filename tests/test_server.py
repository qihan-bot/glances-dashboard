"""Static path and sensor endpoint regression checks (standard library only)."""
import sys
import os
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve import Handler


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path):
        conn = HTTPConnection(*self.server.server_address)
        conn.request(method, path)
        response = conn.getresponse()
        status, headers, body = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return status, headers, body

    def test_hidden_paths(self):
        for method in ('GET', 'HEAD'):
            for path in ('/.git/config', '/%2egit/config', '/.%67it/config', '/docs/../.git/config', '/docs/%2e%2e/.git/config', '/.gitignore'):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.request(method, path)[0], 404)

    def test_public_assets(self):
        for path in ('/', '/favicon.svg', '/apple-touch-icon.png'):
            self.assertEqual(self.request('GET', path)[0], 200)
        self.assertEqual(self.request('GET', '/docs/')[0], 404)

    def test_sensors_cors(self):
        status, headers, body = self.request('GET', '/sensors.json')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Access-Control-Allow-Origin'], '*')
        self.assertIn(b'"sensors"', body)

    def test_dashboard_migration_redirect(self):
        with patch.dict(os.environ, MONITOR_DASHBOARD_URL='http://dashboard.test:61209/'):
            for method in ('GET', 'HEAD'):
                for path in ('/', '/?previous=bookmark'):
                    status, headers, body = self.request(method, path)
                    self.assertEqual(status, 302)
                    self.assertEqual(headers['Location'], 'http://dashboard.test:61209/')
                    self.assertEqual(headers['Cache-Control'], 'no-store')
                    self.assertEqual(body, b'')
            for path in ('/index.html', '/favicon.svg', '/sensors.json', '/power.json'):
                status, headers, _ = self.request('GET', path)
                self.assertEqual(status, 200)
                self.assertNotIn('Location', headers)
            self.assertEqual(self.request('GET', '/%2egit/config')[0], 404)


if __name__ == '__main__':
    unittest.main()
