"""Opt-in LAN power controls; private configuration lives outside the web root."""
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
import urllib.error
import urllib.request

CONFIG = Path.home() / '.config/monitor/power.json'
LOCK = threading.Lock()
LAST_ACTION = {}


def config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def origins(cfg):
    return {f'http://{h["addr"]}:{h.get("dport", 61209)}' for h in cfg.get('hosts', [])}


def allowed_request(headers, cfg):
    origin = headers.get('Origin', '')
    return (origin in origins(cfg)
            and 'http://' + headers.get('Host', '') in origins(cfg)
            and headers.get('X-Monitor-Action') == '1'
            and headers.get('Content-Type', '').split(';')[0] == 'application/json')


def status(cfg):
    return {
        'addr': cfg.get('addr'),
        'enabled': cfg.get('access') in ('lan', 'password'),
        'password_required': cfg.get('access') == 'password',
        'shutdown': bool(cfg.get('shutdown')),
        'reboot': bool(cfg.get('reboot')),
        'hosts': [{k: h.get(k) for k in ('addr', 'name', 'dport', 'wake_note', 'wake_enabled', 'wake_dashboard')}
                  for h in cfg.get('hosts', [])],
    }


def authenticated(cfg, password):
    if cfg.get('access') == 'lan':
        return True
    if cfg.get('access') != 'password' or not isinstance(password, str):
        return False
    try:
        salt = bytes.fromhex(cfg['password_salt'])
        expected = cfg['password_hash']
    except (KeyError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 300000).hex()
    return hmac.compare_digest(actual, expected)


def magic_packet(mac):
    if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
        raise ValueError('唤醒网卡地址未配置')
    return b'\xff' * 6 + bytes.fromhex(mac.replace(':', '')) * 16


def send_wake(host):
    relay = host.get('wake_relay')
    if relay:
        body = {'action': 'wake', 'target': host['addr']}
        headers = {'Content-Type': 'application/json', 'X-Monitor-Relay': '1'}
        if relay.get('origin'):
            headers.update({'Origin': relay['origin'], 'X-Monitor-Action': '1'})
        request = urllib.request.Request(relay['url'], data=json.dumps(body).encode(), headers=headers)
        # LAN control traffic must not use an environment-configured HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            result = json.loads(response.read(4096))
            if response.status != 200 or result.get('ok') is not True or result.get('target') != host['addr']:
                raise ValueError('Relay did not confirm the requested target')
        return
    packet = magic_packet(host.get('mac'))
    broadcast = str(ipaddress.IPv4Address(host['broadcast']))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            sock.sendto(packet, (broadcast, 9))


def execute(cfg, body):
    if not authenticated(cfg, body.get('password')):
        return 403, {'error': '控制密码不正确或电源控制尚未启用'}
    action, target = body.get('action'), body.get('target')
    host = next((h for h in cfg.get('hosts', []) if h.get('addr') == target), None)
    if not host or action not in ('shutdown', 'reboot', 'cancel', 'wake', 'check'):
        return 400, {'error': '未知操作或未配置的主机'}
    if action != 'wake' and target != cfg.get('addr'):
        return 400, {'error': '电源操作请求必须发送到所选主机'}
    if action == 'wake' and not host.get('wake_enabled'):
        return 409, {'error': host.get('wake_note') or '尚未配置唤醒'}
    if action in ('shutdown', 'reboot') and not cfg.get(action):
        return 409, {'error': '本机尚未启用此电源操作'}
    if action in ('cancel', 'check') and not (cfg.get('shutdown') or cfg.get('reboot')):
        return 409, {'error': '本机尚未启用电源操作'}
    with LOCK:
        # Check is deliberately non-mutating, including on a live machine.
        if action == 'check':
            commands = [['/usr/bin/sudo', '-n', '-l', '/usr/sbin/shutdown', *args]
                        for enabled, args in ((cfg.get('shutdown'), ['-h', '+1']),
                                              (cfg.get('reboot'), ['-r', '+1']),
                                              (True, ['-c'])) if enabled]
        elif action == 'wake':
            commands = []
        else:
            commands = [['/usr/bin/sudo', '-n', '/usr/sbin/shutdown',
                         *{'shutdown': ['-h', '+1'], 'reboot': ['-r', '+1'], 'cancel': ['-c']}[action]]]
        key = (target, action)
        now = time.monotonic()
        if action != 'check' and now - LAST_ACTION.get(key, -100) < 5:
            return 429, {'error': '操作刚刚提交，请稍后再试'}
        try:
            for cmd in commands:
                result = subprocess.run(cmd, capture_output=True, timeout=8, check=False)
                if result.returncode:
                    return 503, {'error': '系统未接受请求；请检查电源操作权限或待执行的计划'}
            if action == 'wake':
                send_wake(host)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            return 503, {'error': '控制服务执行失败，请检查配置及网络'}
        if action != 'check':
            LAST_ACTION[key] = now
    messages = {'shutdown': '已安排 1 分钟后关机，可在关机前取消。',
                'reboot': '已安排 1 分钟后重启，可在重启前取消。',
                'cancel': '已取消待执行的关机或重启计划。',
                'wake': '已发送唤醒包；请等待主机恢复在线。发送成功不代表已经开机。',
                'check': '已启用的电源操作及取消权限检查通过，未执行关机或重启。'}
    return 200, {'ok': True, 'action': action, 'target': target, 'message': messages[action]}
