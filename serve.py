#!/usr/bin/env python3
"""Static server for the dashboard plus /sensors.json built from sysfs hwmon.

Glances' sensor labels ("Composite", "Composite 1") are ordered by hwmon index and
carry no device identity, so temperatures are read here and named by hardware.
Usage: serve.py [port]
"""
import json
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HWMON = Path("/sys/class/hwmon")

NVME_SENSOR_NAMES = {"Composite": "复合", "Sensor 1": "控制器", "Sensor 2": "闪存"}


def read(p, default=None):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return default


def milli(p):
    v = read(p)
    if v is None or v == "":
        return None
    v = int(v) / 1000
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
    for hw in sorted(HWMON.glob("hwmon*"), key=lambda p: int(p.name[5:])):
        name = read(hw / "name", "")
        dev = (hw / "device").resolve().name if (hw / "device").exists() else ""
        if name == "nvme":
            model = re.sub(r"\s+", " ", read(f"/sys/class/nvme/{dev}/model", dev) or dev)
            device, group, did = f"{dev} · {model}", "nvme", dev
        elif name == "k10temp":
            device, group, did = f"CPU · {cpu_model()}", "core", "cpu"
        elif name == "amdgpu":
            device, group, did = f"GPU · {gpu_model(hw)}", "core", "gpu"
        elif name == "acpitz":
            device, group, did = "主板 · ACPI 热区", "core", "board"
        elif name.startswith("iwlwifi"):
            device, group, did = "Wi-Fi · 无线网卡", "core", "wifi"
        else:
            device, group, did = f"{name}", "other", name
        for t in sorted(hw.glob("temp*_input")):
            base = str(t)[:-6]
            val = milli(t)
            if val is None:
                continue
            label = read(base + "_label", t.name[:-6]) or t.name[:-6]
            sid = f"{did}.{re.sub(r'[^a-z0-9]+', '_', label.lower())}"
            warn, crit, assumed = milli(base + "_max"), milli(base + "_crit"), False
            if did in ("cpu", "gpu") and crit is None:  # k10temp/amdgpu expose no limits; Ryzen 7040 Tjmax is 100 °C
                warn, crit, assumed = 85.0, 95.0, True
            nice = {"Tctl": "Tctl（封装）", "edge": "核心", "temp1": "温度"}.get(label, label)
            out.append({
                "id": sid, "device_id": did, "device": device, "group": group,
                "label": NVME_SENSOR_NAMES.get(label, label) if name == "nvme" else nice,
                "value": val, "warn": warn, "crit": crit, "assumed": assumed,
            })
        if name == "amdgpu":
            pw = read(hw / "power1_input")
            if pw:
                out.append({"id": "gpu.power", "device_id": "gpu", "device": f"GPU · {gpu_model(hw)}", "group": "power",
                            "label": "功耗", "value": int(pw) / 1e6, "unit": "W"})
    order = {"cpu": 0, "gpu": 1, "board": 2, "wifi": 3}
    out.sort(key=lambda x: (order.get(x["device_id"], 10), x["device_id"]))
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if any(part.startswith(".") for part in path.split("/")):  # never serve .git or other dotfiles
            self.send_error(404)
            return
        if path == "/sensors.json":
            body = json.dumps({"sensors": sensors()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 61209
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
