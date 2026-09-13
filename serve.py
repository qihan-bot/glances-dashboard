#!/usr/bin/env python3
"""Static server for the dashboard plus /sensors.json built from sysfs hwmon.

Glances' sensor labels ("Composite", "Composite 1") are ordered by hwmon index and
carry no device identity, so temperatures are read here and named by hardware.
Usage: serve.py [port]
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
import ec
import fan
import nvgpu
import power
import smart
import spd

ROOT = Path(__file__).resolve().parent
HWMON = Path("/sys/class/hwmon")

NVME_SENSOR_NAMES = {"Composite": "复合温度"}


def read(p, default=None):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return default


def milli(p):
    v = read(p)
    if v is None or v == "":
        return None
    try:
        v = int(v) / 1000
    except ValueError:
        return None
    return v if -50 < v < 500 else None  # nvme reports 65261 for "unset"


def cpu_model():
    m = re.search(r"model name\s*:\s*(.+)", read("/proc/cpuinfo", "") or "")
    return m.group(1).strip() if m else "CPU"


def gpu_model(hw):
    dev = (hw / "device").resolve()
    for card in Path("/sys/class/drm").glob("card*"):
        if (card / "device").resolve() == dev:
            name = read(card / "device/product_name")
            if name:
                return name
    return "amdgpu"


def sensors():
    out = []
    sampled_at = datetime.now(timezone.utc).isoformat()
    excluded = set(os.environ.get("MONITOR_SENSOR_EXCLUDE", "").split(","))
    ignore_limits = set(os.environ.get("MONITOR_SENSOR_IGNORE_LIMITS", "").split(","))
    chips = sorted(HWMON.glob("hwmon*"), key=lambda p: int(p.name[5:]))
    dup = {n for n in (read(hw / "name", "") for hw in chips) if sum(read(h / "name", "") == n for h in chips) > 1}
    for hw in chips:
        name = read(hw / "name", "")
        dev = (hw / "device").resolve().name if (hw / "device").exists() else ""
        if name == "nvme":
            model = re.sub(r"\s+", " ", read(f"/sys/class/nvme/{dev}/model", dev) or dev)
            device, group, did = f"{dev} · {model}", "nvme", dev
        elif name in ("k10temp", "coretemp"):
            device, group, did = f"CPU · {cpu_model()}", "core", "cpu"
        elif name == "amdgpu":
            device, group, did = f"GPU · {gpu_model(hw)}", "core", "gpu"
        elif name == "acpitz":
            device, group, did = "ACPI · 固件热区（未核实）", "diagnostic", f"acpitz.{dev or hw.name}"
        elif name == "it8613":
            device, group, did = "主板 · IT8613E", "board", name
        elif name.startswith("iwlwifi"):
            device, group, did = "Wi-Fi · 无线网卡", "core", "wifi"
        else:  # several chips with one driver name (e.g. mlx5 ports) must not share ids
            device, group, did = (f"{name} · {dev or hw.name}", "other", f"{name}.{dev or hw.name}") if name in dup else (name, "other", name)
        for t in sorted(hw.glob("temp*_input")):
            if f"{name}:{t.name[:-6]}" in excluded:
                continue
            base = str(t)[:-6]
            val = milli(t)
            quality, note = 'reported', ''
            if val is None:
                quality, note = 'unavailable', '读取失败或格式无效，未更新'
            elif read(base + '_enable') == '0':
                val, quality, note = None, 'unavailable', '通道已禁用，未更新'
            elif read(base + '_fault') == '1':
                val, quality, note = None, 'unavailable', '硬件报告传感器故障，未更新'
            elif name == 'acpitz':
                quality, note = 'unverified', '固件热区不代表主板测点；未核实，不计入温度曲线'
            label = read(base + "_label", t.name[:-6]) or t.name[:-6]
            sid = f"{did}.{re.sub(r'[^a-z0-9]+', '_', label.lower())}"
            warn, crit, assumed = milli(base + "_max"), milli(base + "_crit"), False
            if name in ignore_limits:
                warn, crit = None, None
            if warn is not None and (warn <= 0 or (crit is not None and warn > crit)):
                warn = None
            if crit is not None and crit <= 0:
                crit = None
            nice = {"Tctl": "Tctl（控制温度）", "edge": "核心", "temp1": "温度"}.get(label, label)
            sensor_group = group
            if name == "coretemp" and not label.startswith("Package id"):
                sensor_group = "cpu_core"
            if name == "it8613":
                nice = f"通道 {t.name[4:-6]}（位置未核实）"
            out.append({
                "id": sid, "device_id": did, "device": device, "group": sensor_group,
                "label": NVME_SENSOR_NAMES.get(label, label) if name == "nvme" else nice,
                "value": val, "warn": warn, "crit": crit, "assumed": assumed,
                "quality": quality, "note": note, "sampled_at": sampled_at,
                "source": {"driver": name, "path": str(t.resolve()), "label": label},
            })
        if name == "amdgpu":
            pw = read(hw / "power1_input")
            if pw:
                out.append({"id": "gpu.power", "device_id": "gpu", "device": f"GPU · {gpu_model(hw)}", "group": "power",
                            "label": "功耗", "value": int(pw) / 1e6, "unit": "W"})
    order = {"cpu": 0, "gpu": 1, "board": 2, "ec": 2, "wifi": 3}
    out.sort(key=lambda x: (order.get(x["device_id"], 10), x["device_id"]))
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def send_head(self):
        # Validate decoded paths for both GET and HEAD before static resolution.
        path = unquote(urlsplit(self.path).path)
        if any(part.startswith(".") for part in path.split("/")):
            self.send_error(404)
            return None
        dashboard_url = os.environ.get('MONITOR_DASHBOARD_URL')
        if path == '/' and dashboard_url:
            self.send_response(302)
            self.send_header('Location', dashboard_url)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def power_response(self, status, data, cfg):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Vary', 'Origin')
        origin = self.headers.get('Origin')
        if origin in power.origins(cfg):
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Monitor-Action')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        cfg = power.config()
        if urlsplit(self.path).path == '/api/power' and self.headers.get('Origin') in power.origins(cfg):
            self.power_response(200, {}, cfg)
        else:
            self.power_response(403, {'error': '不允许的请求来源'}, cfg)

    def do_POST(self):
        cfg = power.config()
        if urlsplit(self.path).path != '/api/power':
            self.power_response(404, {'error': '未知接口'}, cfg)
            return
        if not power.allowed_request(self.headers, cfg):
            self.power_response(403, {'error': '请从已配置的内网看板操作'}, cfg)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 2048:
                raise ValueError()
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, OSError):
            self.power_response(400, {'error': '无效请求'}, cfg)
            return
        code, data = power.execute(cfg, body)
        self.power_response(code, data, cfg)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/power.json':
            cfg = power.config()
            self.power_response(200, power.status(cfg), cfg)
            return
        if path == "/sensors.json":
            storage = smart.collector.snapshot()
            body = json.dumps({"sensors": sensors() + nvgpu.sensors() + ec.sensors() + spd.sensors() + smart.temperature_sensors(storage),
                               "fans": fan.fans(), "storage": storage}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A host switch can cancel an in-flight sensor request.
            return
        super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 61209
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
