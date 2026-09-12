#!/usr/bin/env python3
"""Install the read-only SPD5118 helper and grant it to the dashboard account.

Scans SMBus adapters first and configures nothing when no DDR5 SPD5118 hub is
present. Installs spd.py as a root-owned helper, loads i2c-dev at boot when it
is a module, and grants exactly that helper via sudo. No SMBus writes.
"""
import argparse
import glob
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import spd  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user) or pwd.getpwnam(args.user).pw_uid < 1000:
        parser.error('Use a regular local account')
    if not glob.glob('/dev/i2c-*'):
        subprocess.run(['modprobe', 'i2c-dev'], check=True)
    if 'i2c_dev ' in Path('/proc/modules').read_text():
        conf = Path('/etc/modules-load.d/monitor-spd.conf')
        if conf.exists() and conf.read_text() != 'i2c-dev\n':
            parser.error('Existing monitor-spd.conf differs; review it before changing')
        conf.write_text('i2c-dev\n')
    modules = spd.scan()
    if not modules:
        parser.error('No SPD5118 module found on SMBus adapters; nothing configured')
    target = Path(spd.HELPER)
    pending = target.with_name('.monitor-spd-temps.tmp')
    shutil.copyfile(Path(spd.__file__), pending)
    os.chown(pending, 0, 0)
    pending.chmod(0o755)
    pending.replace(target)
    rules = Path('/etc/sudoers.d/monitor-spd')
    rule = f'{args.user} ALL=(root) NOPASSWD: {spd.HELPER}\n'
    if rules.exists() and rules.read_text() != rule:
        parser.error('Existing SPD rule differs; review it before changing')
    tmp = rules.with_name('.monitor-spd.tmp')
    tmp.write_text(rule)
    tmp.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo', '-cf', str(tmp)], check=True)
    tmp.replace(rules)
    for m in modules:
        print(f"{m['bus']} 0x{m['addr']:02x}: {m['temp']:.2f} C (high {m['high']:.2f}, crit {m['crit']:.2f}, ts_enabled={m['ts_enabled']})")
    print('Configured one read-only SPD5118 helper; no SMBus writes were performed')


if __name__ == '__main__':
    main()
