# glances-dashboard

A single-file, dependency-free web dashboard for multiple Linux hosts, built on top of the
[Glances](https://github.com/nicolargo/glances) REST API. Open it from any browser on
your LAN to see CPU, memory, load, temperatures, network, disk I/O, storage and the full
process list, with live charts and in-browser history (up to one hour).

![tiles](docs/screenshot-tiles.png)

## What you get

- **Stat tiles**: CPU, memory + swap, load, CPU/GPU/board temperature, network, disk I/O,
  storage, processes (with the current top CPU and top memory process).
- **Live charts**: CPU stacked by user/system/iowait, per-core columns, memory/swap,
  network throughput, disk I/O, load averages, and temperatures grouped by device
  (CPU/GPU and identified board chips, plus a combined drive chart with device channel labels).
- **Hardware temperature table**: every hwmon sensor, named by device model, with
  warning/critical thresholds and a distance-to-critical meter.
- **Process table**: sortable, filterable, optional aggregation by program.
- **Host switcher**: shared presets plus browser-local hosts, custom ports, and a remembered selection. Switching clears old samples and reconnects immediately.
- Crosshair tooltips on every chart, a data-table view per chart, light/dark theme.

Why a sidecar (`serve.py`) instead of plain static hosting: Glances labels temperature
sensors by hwmon index ("Composite", "Composite 1"), which does not identify the device
and can change across reboots. `serve.py` reads `/sys/class/hwmon` directly and names
sensors by hardware (CPU model, GPU, NVMe model) and exposes them at `/sensors.json`.

## Requirements

- Linux with `/sys/class/hwmon` (temperatures are optional; everything else works without).
- Python 3.10+ (standard library only).
- Glances 4.x with the web extra, running with its API reachable from the dashboard host.
  No root required for any of this.

## Install

```sh
# 1. Glances API (user-level, no root)
uv tool install 'glances[web]'      # or: pipx install 'glances[web]'

# 2. Dashboard files
git clone https://github.com/qihan-bot/glances-dashboard ~/monitor

# 3. User-level systemd services (survive reboots if lingering is enabled)
mkdir -p ~/.config/systemd/user
cp ~/monitor/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now glances-web.service monitor-web.service
loginctl enable-linger "$USER"      # may need sudo once
```

Then open `http://<host>:61209/`. The page fetches `http://<same host>:61208/api/4/all`
from Glances and `/sensors.json` from `serve.py`.

Ports: Glances on **61208**, dashboard on **61209**. Change them in the two unit files and
in the host configuration below. The local dashboard port follows the page URL; its
Glances port defaults to 61208 (the `SELF.gport` value in `index.html`).

## Multiple machines

Install Glances and this dashboard on each machine. In **管理**, enter one host per
line: `Name, hostname[, Glances port][, dashboard port]`. For example:

```text
Desktop, desktop.local
NAS, nas.local, 61208, 61209
```

Names may contain spaces. Addresses accept hostnames, IPv4, or bracketed IPv6;
use separate port fields, without a URL scheme or path. Custom hosts and the last
selection are stored in this browser for this dashboard origin. Remove a custom
host by deleting its line. Invalid entries show an error without replacing the list.

For presets shared by all visitors, copy `hosts.example.json` to `hosts.json` and
edit its `hosts` array. Deploy this file separately to each dashboard. It is ignored
by Git so local addresses are not published to the repository, but is readable by
LAN dashboard visitors. Presets are managed on disk, not in the browser.

The browser must reach both ports on the selected host, and both APIs must allow
the dashboard origin via CORS. The bundled sensor service allows cross-origin
reads; verify your Glances CORS settings if metrics fail. HTTP LAN dashboards
should be opened over HTTP; HTTPS pages cannot fetch plain HTTP APIs.

Switching resets charts, process rows, network/disk selection, and in-page history.
Requests from a previous selection are cancelled and ignored. Failed requests time
out after eight seconds and retry automatically. An unavailable sensor sidecar
retains its last readings marked as stale; it never substitutes Glances labels,
zeros, or repeated historical values as new temperature observations. Charts and
sparklines leave gaps, and sample tables display unavailable observations explicitly.

## Power controls (optional)

To move the main dashboard entry to another deployed host, set
`MONITOR_DASHBOARD_URL=http://NEW_HOST:61209/` in a systemd user-service override
for `monitor-web.service` on the old host, reload the user manager, and restart
that service. Only `/` redirects, with a non-cached HTTP 302. Keep both Glances
and the sidecar running on monitored hosts for metrics, temperatures, and power
controls. `/index.html` remains a local fallback, including for waking the main
dashboard host when it is off. Leave the variable unset on the new main host.

Each dashboard can schedule its own host to shut down or reboot in one minute,
cancel either pending operation, or send a magic packet to a configured host. The confirmation
names the selected host. Changing hosts closes the confirmation. A successful
wake response means the packet was sent, not that the host has booted.

1. Copy `power.example.json` to `~/.config/monitor/power.json` on each host,
   **outside the served directory**, and restrict the file to its owner (0600).
   Set `addr` to this host, and list every allowed dashboard under `hosts`.
   Preserve a separate configuration on each host: whether a destination can be
   woken depends on the sending host's network. The default without a config is
   disabled. Set `reboot: true` after installing the reboot sudo rule to enable
   the reboot button; older hosts without this capability keep it disabled.
   `hosts.json` still manages the visible host presets independently.
2. Run `sudo python3 tools/setup_power.py --user USER --interface INTERFACE` once
   on each host. It grants exactly `shutdown -h +1` and `shutdown -c` via sudo,
   and `shutdown -r +1`, and enables a `monitor-wol.service` unit that sets magic-packet wake on the
   selected interface at startup and shutdown. It does not shut down the host,
   restart networking, or change network addresses. Verify BIOS/UEFI wake and
   standby power separately, especially for USB network adapters.
3. Deploy `power.py`, `serve.py` and `index.html`; restart `monitor-web.service`.
   Controls are disabled for unconfigured hosts and shutdown is unavailable when
   the selected host's control API cannot be reached.

`access: "lan"` follows the existing trusted-LAN access model: anyone who can
reach these services can control the configured hosts. Power requests require
JSON, a custom request header, and an exact configured Origin and Host. CORS is
limited to configured dashboard origins. These checks prevent browser-based
cross-site requests and DNS rebinding; they are not user authentication. Do not
expose LAN mode to untrusted networks. Optional `access: "password"` requires a
separate control password on each operation; configure `password_salt` (hex) and
`password_hash` (PBKDF2-HMAC-SHA256, 300000 iterations, hex) in the private config.
Use HTTPS for a password-protected deployment outside a trusted LAN. Passwords
are not retained in browser storage or exposed by `/power.json`.

Wake-on-LAN normally needs an online sender on the target's broadcast domain.
A routed IP connection does not prove broadcast delivery. Set `wake_enabled` to
false and explain the missing relay in `wake_note` when that route is not ready.
When the dashboard host itself is shut down, open another online dashboard to
send its wake packet. If every dashboard is off, a separate always-on relay is
needed. Neither the API nor tests claim to validate firmware wake without an
explicit power-cycle test.

A restricted relay is included as `wol_relay.py`. Install it with `power.py` in
`~/.local/share/monitor-wol/` on an always-on host in the destination subnet.
Install `systemd/monitor-wol-relay.service` as a user service and enable lingering
for that user. Configure `~/.config/monitor/wol-relay.json` outside the served
root with `bind` (LAN IP), `port` (61210), `clients` (observed source IP addresses,
including a verified NAT address if applicable), and `hosts` (fixed `addr`, `mac`
and `broadcast` values). The relay accepts only `wake` for those targets. It has
no shell or shutdown endpoint and accepts no browser cross-origin requests.

On a dashboard, add `wake_relay: {"url": "http://RELAY_IP:61210/wake"}` to the
corresponding private host entry. A failed or negative relay response is an error,
not a successful wake. LAN requests bypass environment HTTP proxies.

When the browser can reach a sender that the current server cannot, set
`wake_dashboard` to that sender's address in the target entry. The sender must
also be in `hosts`. The browser checks the sender's identity and target capability
before enabling Wake, then posts directly to that sender using the existing
restricted CORS policy. Keep `wake_enabled` false on a server that cannot send
the packet itself; this does not prevent the browser from using a ready sender.
A dashboard sender must remain online; if an entire subnet is off, that subnet
needs its own independent always-on sender.

For safe live validation, POST `{"action":"check","target":"HOST_IP"}` to
`/api/power`, with `Origin` set to a configured dashboard origin,
`Content-Type: application/json` and `X-Monitor-Action: 1`. Include `password` only
when password access is configured. This checks the configured shutdown/reboot and cancellation sudo permissions
using `sudo -n -l`; it never executes shutdown, reboot, or cancellation. The automated
suite mocks shutdown and verifies the HTTP boundaries, confirmation flow, host
switching, failures, and exact magic-packet bytes.

## SATA SMART (optional)

Install `smartmontools` on the SATA host, then run
`sudo python3 tools/setup_smart.py --user USER --device /dev/sda`.
This grants only `/usr/sbin/smartctl -x -j /dev/sda` through sudo; the
dashboard cannot initiate self-tests, change SMART settings, or write sectors.
Deploy `smart.py` with `serve.py`, and set `MONITOR_SMART_DEVICE=/dev/sda` in
a `monitor-web.service` user-service override. Reload the user manager and
restart that service. Deploy the updated `index.html` to dashboard entry hosts.

`/sensors.json` includes a `storage` object and SATA temperature sensors.
The existing web process runs one bounded SMART read on demand per 60 seconds;
no extra scheduler is required. Snapshots are cached in memory under a lock.
Failed reads retain the previous health snapshot with `stale: true` and its
original UTC timestamp, and contribute no new disk temperature sample.
The browser also marks prior health values stale when the endpoint is unreachable,
and clears them when selecting a different host. Unconfigured hosts are unchanged.

The health card shows overall SMART status, temperature, power-on hours/cycles,
and raw attributes 5, 197, 198 and 199 when reported. Missing fields stay unknown.
A passing overall SMART test with nonzero selected raw counters is shown as a warning.
Attribute labels are smartctl's decoding, not a manufacturer-specific guarantee;
unmatched drive models are explicitly marked. Do not infer remaining lifespan,
write endurance, or Celsius thresholds from normalized/vendor-specific attributes.

Extended collection includes standard ATA Device Statistics log 04h, page 01h.
Only valid, non-normalized counters are accepted. Cumulative byte counts use the
reported logical sector size; unknown sizes remain unknown. Vendor attributes
241/242 are never interpreted as sector counts. Unmatched models use neutral
raw-attribute labels; nonzero values alone do not establish a bad-sector count.
Re-run the setup tool when upgrading from `-a`: it permits replacement of the
exact legacy rule for the same account/device, and rejects unrelated rules.

Set `MONITOR_SMART_TEMPERATURE_UNVERIFIED=1` on a host after investigating an
unreliable temperature report. The health card retains the original value with
an explicit unverified label, but no new temperature curve samples are produced.
This is configured for the EAGET SSD 128GB / U0115A0 on N100-8G-Backup: raw SMART
checksums and ATA/SAT reads agree, but temperature stayed at 40 degrees across a
power cycle and an 8 GiB direct read on 2026-09-11. The latter increased the
standard read counter by 8 GiB plus 36 KiB of other reads; attribute 242 increased
by 256 (consistent with 32 MiB units). SCT temperature is unsupported and the
standard temperature statistics page is empty. Physical temperature and the exact
controller remain unverified; the latest upstream drive database has no model match.

## Additional hwmon channels

Intel `coretemp` package readings drive the CPU tile/chart; individual cores stay
in the sensor table. IT8613E temperatures have a separate chart and retain channel
numbers because physical sensor locations require board documentation or testing.
ACPI thermal zones are diagnostic-only, labelled unverified, and excluded from
temperature samples and charts. A readable firmware temperature alone does not
identify a physical board measurement. Constant readings alone do not prove a
sensor is fake; the UI states the uncertainty instead of inventing a correction.
On 2026-09-12 the MECHREVO F7BSC host (.210) returned 20 degrees from `acpitz`
through six samples while CPU control temperature varied from 90.25 to 91.75 degrees.
Its currently exposed hwmon devices provide no independently identified board sensor. Its board thermistors live in the
embedded controller instead; see *Embedded controller temperatures* below.

## Embedded controller temperatures (optional)

Some boards expose no hwmon board sensor but keep thermistor readings in the ACPI
embedded controller (EC, `PNP0C09`). `ec.py` reports the EC RAM bytes listed in
`MONITOR_EC_TEMPS` (`offset:group:label`, comma separated; group `board` joins the
board chart, `diagnostic` appears only in the table), read through one exact sudo
command. Raw values outside 1–127 and failed reads are reported as unavailable, so
the page keeps the last reading marked 未更新; no thresholds are inferred and the
EC is never written. The byte map is host-specific and must be established first:

1. `sudo python3 tools/probe_ec.py` copies the ACPI tables, loads `ec_sys` without
   write support, and samples EC RAM next to the known hwmon readings for 90 s.
   `--seconds 300 --interval 1` around a CPU load step separates CPU mirrors from
   slow board thermistors.
2. `python3 tools/ec_map.py DIR` (no root) lists named `EmbeddedControl` fields from
   the ACPI tables, decodes constant `_TMP` returns, and ranks bytes by movement and
   correlation with the references. A byte is a board temperature only when its
   behaviour supports it; firmware that never reads a byte gives it no DSDT name.
3. `sudo python3 tools/setup_ec.py --user USER` writes `/etc/modules-load.d/monitor-ec.conf`,
   loads `ec_sys` read-only, and grants exactly `/usr/bin/cat /sys/kernel/debug/ec/ec0/io`.
4. Set `MONITOR_EC_TEMPS` in the `monitor-web.service` user override, reload the user
   manager, restart the service, and verify `/sensors.json`.

MECHREVO F7BSC (.210, AMI BIOS 1.07, checked 2026-09-12): the DSDT declares the EC
(`\_SB.PCI0.SBRG.EC0`) but names only one byte (`P3TL`, 0x61). `\_TZ.TZ01._TMP`
stores 20.0 degrees and replaces it with `EC0.DIEH` only when `EC0.OKEC` exists,
which this DSDT never defines, so the ACPI zone is a constant. Over a 300 s sample
with a 100 s twelve-thread load step, byte 0x09 followed Tctl (44–92 degrees,
r = 0.96, about 5 s lag); bytes 0x04 and 0x05 rose from 39/38 to 57/54 degrees,
peaked at the end of the load, and decayed over about two minutes (r = 0.95 with
each other, 0.78/0.58 with Tctl). They are not the DDR5 modules: the SPD5118 hubs at
i2c-0 0x50/0x51 read 35.75/32.5 degrees at the same moment. No other byte moved;
bytes 0x11–0x38 are constant tables that look like fan-curve settings, and no fan
tachometer exists in the ACPI EC space. The physical locations of 0x04/0x05 remain
unverified. The intended host map is
`MONITOR_EC_TEMPS=0x04:board:主板热敏 1,0x05:board:主板热敏 2,0x09:diagnostic:CPU（EC 读数）`.
A full 256-byte read takes about 115 ms; `ec.py` caches it for two seconds.

## DDR5 module temperatures (optional)

DDR5 modules carry an SPD5118 hub with a temperature sensor, but kernels before
6.11 have no `spd5118` driver. `spd.py` doubles as a root-only helper: installed by
`sudo python3 tools/setup_spd.py --user USER` as `/usr/local/sbin/monitor-spd-temps`
(root-owned, granted through one exact sudo rule), it scans SMBus adapters for hubs
at 0x50–0x57, checks the device type (MR0/MR1 = 0x51 0x18), and prints the current
temperature (MR49/MR50) with the hardware high/critical limits (MR28, MR32) as JSON.
Only SMBus reads are issued; the page register and EEPROM are never written, so the
module part number is not read and slot assignment stays unverified. The setup tool
configures nothing when no hub is found. Set `MONITOR_SPD_TEMPS=1` in the
`monitor-web.service` user override to enable it; a disabled sensor, an
out-of-range value, or a failed helper run reports unavailable for the modules seen
last, and the memory chart keeps the last reading marked 未更新. Readings are cached
for five seconds. On .210 (2026-09-12) the two Micron CT16G56C46S5 SODIMMs answered
at i2c-0 0x50/0x51 with 35.75/32.5 degrees and module limits high 55, critical 85.
The 家庭网关 (.135) and N100-8G-Backup (.243) hosts use soldered LPDDR5 with no SPD hub,
so the setup tool found nothing there and they stay unconfigured.

Every hwmon temperature includes its driver, sysfs path, raw channel label, UTC
sample time, and quality. Failed/malformed reads, disabled channels, and asserted
`tempN_fault` flags produce unavailable observations without failing other channels.
Thresholds come only from the hardware interface; absent/invalid values remain
unknown. No generic CPU/GPU limits are inferred. NVMe Sensor 1/2 retain their
driver names because controller/NAND placement is device-specific. ACPI zone IDs
include the device identity to prevent multiple zones overwriting one another.
Restart `monitor-web.service` after Python changes and verify the live API; an
updated file does not update the already-running process.

For a host with independently verified invalid inputs, set a comma-separated
`MONITOR_SENSOR_EXCLUDE` list of `chip:tempN` keys in that host's service override.
`MONITOR_SENSOR_IGNORE_LIMITS` lists chips with unusable firmware alarm limits.
These settings affect dashboard presentation only and never write hardware registers.
They are unset by default; do not exclude all constant or 27.8 degree readings globally.

The N100-8G-Backup host was checked on 2026-09-11: ACPI `_TMP` has a 27.8 degree
fallback, IT8613E channel 3 reports roughly -50 degrees, and its temperature limits
are invalid. Its override excludes `acpitz:temp1,it8613:temp3` and ignores `it8613`
limits. Channel 1 changed with CPU cooling; channel 2 remained near 54 degrees
during the initial sample. Neither channel's physical location or calibration is
independently confirmed. No fan-control/PWM settings were changed.

This host uses the [upstream IT87 driver](https://github.com/frankcrawford/it87)
at commit `a904dd88b295a1bd4eeb47af523d8fad1e566a9f`, built with `CC=gcc-15`
for kernel `7.0.0-31-generic`. Sources are retained in `~/monitor-it87` on that host.
The module is installed in that kernel's `updates` directory without overwriting
the distribution module, and `monitor-it87.conf` loads it at boot. This is a local,
unsigned, out-of-tree module; Secure Boot was disabled at installation. It is not
managed by DKMS: after a kernel upgrade, rebuild and verify support before copying
the module into the new kernel's `updates` directory and running `depmod`.

## Security

Both services bind to `0.0.0.0` with no authentication. Run this only on a trusted LAN or
behind a VPN such as Tailscale. To add a password to Glances, run `glances -w --password`
once and adjust the unit file.

The static server rejects dot-prefixed path components (including URL-encoded
ones) for GET and HEAD, and disables directory listings.

## Design notes

NEXUS is a cyberpunk console with a graphite background, cyan primary signals,
magenta comparison series and lime accents. Alert colors remain separate and
include text labels. A day palette is available; the selected palette persists
under `nexus_theme`. Fonts, the geometric Q logo and charts require no CDN.
`favicon.svg` is the logo source; the PNG and touch icon are rasterized from it.

The desktop rail links to overview, telemetry, thermal sensors and processes.
The node strip and selector share the same host-switch path and cancellation
logic. The selected node badge indicates selection, not a claim that all other
nodes are online. On mobile, the strip scrolls horizontally and charts use one
column. Power confirmations and their target checks retain the existing behavior.

Charts use faint dashed grids, gradient area fills, restrained line glow, a
latest-value legend and segmented core-load columns. The time axis fits collected
history with a minimum 30-second span, up to the selected maximum time window;
no synthetic historical observations are added. Crosshair tooltips and expandable
sample tables remain available. Every chart shows its real sample/core count.
The dashboard defaults to a 5-second sampling interval and a 30-minute time window.

Drive temperatures share one chart. Each drive has a model/name tag, with
single-selection, multi-selection and select-all controls. At least one drive
remains selected. Colors identify drives; line dashes distinguish their composite,
individual sensor channels. Selections preserve collected history and reset when
switching hosts. The separate hardware table retains all available sensor values.

Theme switching cycles only between dark and light. Invalid saved preferences
(including the legacy `undefined` value) recover to dark, and switching continues
to work if browser storage is blocked.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

Server checks use only the standard library. Browser regression checks require
`playwright` (`python3 -m pip install playwright` and `python3 -m playwright install chromium`);
they are skipped when the package is unavailable. Browser tests use synthetic metrics
and cover switching, stale responses, offline recovery, persistence, and custom ports.

## License

MIT
