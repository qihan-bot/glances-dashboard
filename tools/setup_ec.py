#!/usr/bin/env python3
"""Grant one exact read-only EC RAM read to the dashboard account and load ec_sys at boot.

Requires an ACPI embedded controller (PNP0C09). ec_sys is loaded without write
support; the sudo rule allows only reading /sys/kernel/debug/ec/ec0/io. Nothing
here writes to the EC, changes fan control, or alters other sudo rules.
"""
import argparse
import os
from pathlib import Path
import pwd
import re
import subprocess

EC_IO = Path('/sys/kernel/debug/ec/ec0/io')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user) or pwd.getpwnam(args.user).pw_uid < 1000:
        parser.error('Use a regular local account')
    if not Path('/sys/bus/acpi/devices/PNP0C09:00').exists():
        parser.error('No ACPI embedded controller (PNP0C09) on this host')
    modules = Path('/etc/modules-load.d/monitor-ec.conf')
    if modules.exists() and modules.read_text() != 'ec_sys\n':
        parser.error('Existing monitor-ec.conf differs; review it before changing')
    modules.write_text('ec_sys\n')
    if not Path('/sys/module/ec_sys').exists():
        subprocess.run(['modprobe', 'ec_sys'], check=True)
    if Path('/sys/module/ec_sys/parameters/write_support').read_text().strip() != 'N':
        parser.error('ec_sys is loaded with write support; unload it before continuing')
    if not EC_IO.exists():
        parser.error(f'{EC_IO} missing after loading ec_sys')
    target = Path('/etc/sudoers.d/monitor-ec')
    rule = f'{args.user} ALL=(root) NOPASSWD: /usr/bin/cat {EC_IO}\n'
    if target.exists() and target.read_text() != rule:
        parser.error('Existing EC rule differs; review it before changing')
    pending = target.with_name('.monitor-ec.tmp')
    pending.write_text(rule)
    pending.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo', '-cf', str(pending)], check=True)
    pending.replace(target)
    print('Configured one read-only EC RAM read; ec_sys loads at boot without write support')


if __name__ == '__main__':
    main()
