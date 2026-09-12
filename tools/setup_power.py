#!/usr/bin/env python3
"""Grant only delayed shutdown/reboot/cancellation and persist magic-packet Wake-on-LAN.

Run once with sudo on each monitored host. This never shuts down a host or
reconfigures its network addresses/routes.
"""
import argparse
import os
from pathlib import Path
import pwd
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    parser.add_argument('--interface', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this setup with sudo.')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user) or pwd.getpwnam(args.user).pw_uid < 1000:
        parser.error('Use a regular local account.')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.interface) or not Path('/sys/class/net', args.interface).exists():
        parser.error('Unknown network interface.')
    ethtool = '/usr/sbin/ethtool'
    info = subprocess.check_output([ethtool, args.interface], text=True)
    supported = re.search(r'Supports Wake-on:\s*(\S+)', info)
    if not supported or 'g' not in supported.group(1):
        parser.error('This interface does not report magic-packet support.')
    rule = f'{args.user} ALL=(root) NOPASSWD: /usr/sbin/shutdown -h +1, /usr/sbin/shutdown -r +1, /usr/sbin/shutdown -c\n'
    pending = Path('/etc/sudoers.d/.monitor-power.tmp')
    pending.write_text(rule)
    pending.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo', '-cf', str(pending)], check=True)
    pending.replace('/etc/sudoers.d/monitor-power')
    unit = f'''[Unit]
Description=Enable magic-packet wake for the resource dashboard
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart={ethtool} -s {args.interface} wol g
ExecStop={ethtool} -s {args.interface} wol g
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
'''
    Path('/etc/systemd/system/monitor-wol.service').write_text(unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'monitor-wol.service'], check=True)
    # Also apply after an idempotent setup when the unit was already active.
    subprocess.run([ethtool, '-s', args.interface, 'wol', 'g'], check=True)
    info = subprocess.check_output([ethtool, args.interface], text=True)
    print('\n'.join(line.strip() for line in info.splitlines() if 'Wake-on:' in line))
    print('Configured delayed shutdown/reboot, cancellation and persistent Wake-on-LAN.')


if __name__ == '__main__':
    main()
