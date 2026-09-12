"""Restricted Wake-on-LAN sender for a separate broadcast domain.

There is no shell execution or shutdown endpoint. Bind only to the configured
LAN address, accept only configured client addresses, and wake only fixed targets.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from power import send_wake

CONFIG = Path.home() / '.config/monitor/wol-relay.json'
LOCK = threading.Lock()
LAST_WAKE = {}


def config():
    return json.loads(CONFIG.read_text())


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self.reply(200, {'ok': True})
        else:
            self.reply(404, {'error': 'Unknown endpoint'})

    def do_POST(self):
        cfg = config()
        if (self.path != '/wake' or self.client_address[0] not in cfg['clients']
                or self.headers.get('X-Monitor-Relay') != '1'
                or self.headers.get('Content-Type') != 'application/json'
                or self.headers.get('Origin')):
            self.reply(403, {'error': 'Request not permitted'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                raise ValueError()
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or body.get('action') != 'wake':
                raise ValueError()
            host = next((h for h in cfg['hosts'] if h['addr'] == body.get('target')), None)
            if not host:
                raise ValueError()
        except (ValueError, OSError):
            self.reply(400, {'error': 'Unknown target or invalid request'})
            return
        with LOCK:
            now = time.monotonic()
            if now - LAST_WAKE.get(host['addr'], -100) < 3:
                self.reply(429, {'error': 'Wake was recently requested'})
                return
            try:
                # Reconstruct only local packet fields; relays cannot chain.
                send_wake({'mac': host['mac'], 'broadcast': host['broadcast']})
            except (OSError, ValueError, KeyError):
                self.reply(503, {'error': 'Wake packet could not be sent'})
                return
            LAST_WAKE[host['addr']] = now
        self.reply(200, {'ok': True, 'target': host['addr']})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    cfg = config()
    ThreadingHTTPServer((cfg['bind'], cfg.get('port', 61210)), Handler).serve_forever()
