#!/usr/bin/env python3
"""Install the read-only ITE I2EC fan helper and grant it to the dashboard account.

Verifies the Super I/O chip ID first and configures nothing on an unknown chip.
Installs fan.py as a root-owned helper and grants exactly that helper via sudo.
The helper only reads EC RAM; no EC register or fan setting is written.
"""
import argparse
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fan  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user) or pwd.getpwnam(args.user).pw_uid < 1000:
        parser.error('Use a regular local account')
    if not Path('/dev/port').exists():
        parser.error('/dev/port is unavailable on this kernel')
    data = fan.scan()
    if not data['supported']:
        parser.error(f'Super I/O chip {data["chip"]} has no verified I2EC fan register map; nothing configured')
    target = Path(fan.HELPER)
    pending = target.with_name('.monitor-fan-i2ec.tmp')
    shutil.copyfile(Path(fan.__file__), pending)
    os.chown(pending, 0, 0)
    pending.chmod(0o755)
    pending.replace(target)
    rules = Path('/etc/sudoers.d/monitor-fan')
    rule = f'{args.user} ALL=(root) NOPASSWD: {fan.HELPER}\n'
    if rules.exists() and rules.read_text() != rule:
        parser.error('Existing fan rule differs; review it before changing')
    tmp = rules.with_name('.monitor-fan.tmp')
    tmp.write_text(rule)
    tmp.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo', '-cf', str(tmp)], check=True)
    tmp.replace(rules)
    print(f"{data['chip_name']}: CTR={data['ctr']} " + ' '.join(f"fan{f['index']} DCR={f['dcr']} tach={f['tach']}" for f in data['fans']))
    print('Configured one read-only I2EC fan helper; no EC writes were performed')


if __name__ == '__main__':
    main()
