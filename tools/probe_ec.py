#!/usr/bin/env python3
"""Read-only probe of the ACPI Embedded Controller (EC) for temperature mapping.

Run once with sudo on a host whose hwmon tree has no board sensor but whose
ACPI namespace declares an EC (PNP0C09). It loads ``ec_sys`` without write
support, copies the ACPI tables, and samples the 256-byte EC RAM alongside the
already-known hwmon readings (k10temp Tctl, amdgpu edge, SMU SoC, NVMe) so
that EC bytes can be matched against named DSDT fields and against real
temperature movement. Nothing is written to hardware, sudoers, or services.

Usage: sudo python3 probe_ec.py [--seconds 90] [--interval 3] [--out DIR]
Analyse the output without root: python3 ec_map.py DIR
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TABLES = Path('/sys/firmware/acpi/tables')
EC_IO = Path('/sys/kernel/debug/ec/ec0/io')


def read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return None


def hwmon_reference():
    """Known-good temperatures (milli-degrees) sampled next to each EC read."""
    out = {}
    for hw in Path('/sys/class/hwmon').glob('hwmon*'):
        name = read(hw / 'name')
        for t in hw.glob('temp*_input'):
            label = read(str(t)[:-6] + '_label') or t.name[:-6]
            out[f'{name}:{label}'] = read(t)
    try:
        d = Path('/sys/class/drm/card0/device/gpu_metrics').read_bytes()
        if len(d) >= 28 and d[2] == 2:  # gpu_metrics v2.x: gfx, soc, core[8], l3[2] in centi-degrees
            vals = struct.unpack_from('<12H', d, 4)
            out['smu:gfx'] = vals[0] * 10
            out['smu:soc'] = vals[1] * 10
            out['smu:core_max'] = max(vals[2:10]) * 10
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seconds', type=int, default=90)
    ap.add_argument('--interval', type=float, default=3.0)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    if os.geteuid() != 0:
        ap.error('Run with sudo (reads root-only ACPI tables and debugfs)')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = Path(args.out or f'/tmp/monitor-ec-probe-{stamp}')
    out.mkdir(parents=True, exist_ok=True)
    log = []

    def note(msg):
        print(msg)
        log.append(msg)

    # 1. ACPI tables (DSDT + SSDTs) for offline field-name extraction.
    for t in sorted(TABLES.iterdir()):
        if t.name == 'DSDT' or t.name.startswith('SSDT'):
            shutil.copyfile(t, out / f'{t.name}.aml')
    note(f'copied ACPI tables: {sorted(p.name for p in out.glob("*.aml"))}')

    # 2. EC via ec_sys (read-only; write_support stays off).
    if not EC_IO.exists():
        r = subprocess.run(['modprobe', 'ec_sys'], capture_output=True, text=True)
        note(f'modprobe ec_sys rc={r.returncode} {r.stderr.strip()}')
    ec_ok = EC_IO.exists()
    note(f'EC debugfs io present: {ec_ok}')
    ws = read('/sys/module/ec_sys/parameters/write_support')
    note(f'ec_sys write_support={ws}')
    if ws not in (None, 'N', '0'):
        note('ABORT: ec_sys loaded with write support; refusing to continue')
        sys.exit(2)
    for extra in ('gpe', 'use_global_lock'):
        note(f'ec0/{extra}={read(EC_IO.parent / extra)}')

    # 3. Context that also needs root: DMI memory/board, EC kernel messages.
    for name, cmd in (('dmidecode_t2_t17.txt', ['dmidecode', '-t', '2', '-t', '17']),
                      ('dmesg_ec.txt', ['sh', '-c', 'dmesg | grep -iE "ACPI: EC|ec_sys|EC: |thermal|acpitz|PNP0C09"'])):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            (out / name).write_text(r.stdout + (('\n[stderr]\n' + r.stderr) if r.stderr else ''))
        except (OSError, subprocess.SubprocessError) as e:
            (out / name).write_text(f'failed: {e}\n')

    # 4. Sample EC RAM with reference temperatures.
    samples = out / 'ec_samples.jsonl'
    n = 0
    if ec_ok:
        deadline = time.monotonic() + args.seconds
        with samples.open('w') as fh:
            while True:
                try:
                    with EC_IO.open('rb') as f:
                        ram = f.read(256)
                    err = None
                except OSError as e:
                    ram, err = b'', str(e)
                rec = {'t': datetime.now(timezone.utc).isoformat(), 'ec': ram.hex(), 'error': err,
                       'ref': hwmon_reference()}
                fh.write(json.dumps(rec) + '\n')
                n += 1
                if time.monotonic() >= deadline:
                    break
                time.sleep(args.interval)
    note(f'EC samples written: {n}')

    (out / 'probe_log.txt').write_text('\n'.join(log) + '\n')
    # Hand the directory to the invoking user so analysis needs no root.
    uid, gid = int(os.environ.get('SUDO_UID', 0)), int(os.environ.get('SUDO_GID', 0))
    if uid:
        for p in [out, *out.iterdir()]:
            os.chown(p, uid, gid)
            p.chmod(0o755 if p.is_dir() else 0o644)
    print(f'\nDone. Analyse without root:\n  python3 {Path(__file__).resolve().parent / "ec_map.py"} {out}')


if __name__ == '__main__':
    main()
