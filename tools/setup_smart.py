#!/usr/bin/env python3
"""Grant one exact read-only SMART command to the dashboard account."""
import argparse
import os
from pathlib import Path
import pwd
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    parser.add_argument('--device', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user) or pwd.getpwnam(args.user).pw_uid < 1000:
        parser.error('Use a regular local account')
    if not re.fullmatch(r'/dev/sd[a-z]+', args.device) or not Path(args.device).is_block_device():
        parser.error('Use an existing SATA block device')
    if not Path('/usr/sbin/smartctl').is_file():
        parser.error('Install smartmontools first')
    target = Path('/etc/sudoers.d/monitor-smart')
    rule = f'{args.user} ALL=(root) NOPASSWD: /usr/sbin/smartctl -x -j {args.device}\n'
    legacy = rule.replace(' -x -j ', ' -a -j ')
    if target.exists() and target.read_text() not in (rule, legacy):
        parser.error('Existing SMART rule differs; review it before changing')
    pending = target.with_name('.monitor-smart.tmp')
    pending.write_text(rule)
    pending.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo', '-cf', str(pending)], check=True)
    pending.replace(target)
    print('Configured one read-only SMART query; no self-test or disk changes performed')


if __name__ == '__main__':
    main()
