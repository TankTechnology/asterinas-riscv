# Megrez Asterinas basic-console hard-hang incident

Date: 2026-10-03 (Asia/Shanghai)

Status: **unrecovered; physical reset required**

This record captures the failed physical boot so that it is not treated as a
successful startup or silently retried. It is an incident record, not a claim
that the exact kernel root cause has already been isolated.

## Trigger and selected artifacts

The run used the existing, previously verified Megrez files from `/boot`:

| Object | File | U-Boot CRC32 |
| --- | --- | --- |
| Kernel | `asterinas-e106f84051f0.booti` | `95d8aaf3` |
| Stage1 | `stage1-d62ab8325e03.cpio` | `4be69006` |
| DTB | `megrez-465cb129333c.dtb` | `40ed4c65` |

The volatile boot arguments included `console=ttyS0`,
`asterinas.klog_capture=info`, `init=/init`,
`asterinas.reboot_after=90`, and `-- --root-init=basic`. No U-Boot
environment, boot file, partition, or rootfs was written by this run. The
host-side `basic-console` profile only changed which serial marker the runner
accepted; it did not change the kernel or guest image.

Raw serial evidence is retained at
[`target/megrez-board-runs/20261003T-basic-console/serial.log`](../../../target/megrez-board-runs/20261003T-basic-console/serial.log).

## Observed boundary

The board successfully reached:

- four-hart Asterinas startup;
- framebuffer and MMC initialization;
- GMAC and both DWC3 USB host paths;
- USB hub/HID enumeration;
- the `asterinas-basic#` shell;
- `ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=90`;
- the host `[MILESTONE] userspace` marker.

The last output was a GMAC receive-progress snapshot at guest uptime about
3.9 seconds. After that point the UART produced no more bytes. The expected
OpenSBI/U-Boot recovery epoch after the 90-second timer never appeared, and no
RockOS login prompt appeared.

## Recovery checks

After the run, the host performed read-only checks:

1. `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` still
   resolved to `/dev/ttyUSB0`, but no process owned it.
2. Passive serial reads totaling roughly 40 seconds returned zero bytes.
3. `10.100.19.200` had an ARP state of `FAILED`; ICMP and bounded TCP probes
   to common management ports were unreachable.
4. No fresh `OpenSBI`, `U-Boot`, `panic`, `fatal`, or `error` marker was found
   after the last GMAC line.

These observations prove loss of the tested control paths. They do not prove
whether the board lost power, stopped transmitting UART, or entered a kernel
hard hang; the FTDI adapter remaining present only proves the host-side USB
adapter is connected.

## Current diagnosis

The strongest bounded hypothesis is a failure in the timer/SBI recovery path
or an interrupt-path hang after USB/GMAC initialization. The timer was armed,
but the timer did not produce a recovery epoch. USB/GMAC activity immediately
preceded the last output. This is a hypothesis, not a confirmed root cause;
the log contains no panic or register dump that distinguishes these cases.

The earlier basic run did recover once, so this behavior is currently
nondeterministic. The failure must be considered a **recovery-contract failure**
for unattended physical testing.

## Immediate safety decision

No further serial characters, U-Boot commands, software reboot requests, or
desktop launches are permitted while this incident remains unrecovered. An
operator must physically reset or power-cycle the board. After reset, the first
operation is a read-only RockOS identity/boot-id check, followed by a close and
reopen of the serial connection. A fresh authenticated control channel is a
precondition for any Asterinas boot.

## Prevention and next diagnostic gate

The next board attempt must be a minimal diagnostic boot, not a desktop run:

1. Keep `--require-recovery` and fail closed if no new firmware epoch appears.
2. Start with USB host and GMAC disabled, retaining only UART, timer, MMC, and
   the basic console; use a short recovery timer.
3. Add a bounded timer heartbeat and an explicit timer-expiry marker so that a
   silent timer/SBI failure is distinguishable from a UART-only failure.
4. Reintroduce GMAC and USB one at a time, with a fresh recovery proof after
   each boot. Never retry a failed physical boot automatically.
5. Do not qualify desktop, Firefox, DRM, or GPU work until the minimal boot
   has a repeatable recovery path.

The host runner has also been hardened in this checkout: it no longer sends an
empty line before waiting for U-Boot. An empty U-Boot line repeats the previous
command and could accidentally rerun a stale `booti` or load operation. The
regression suite now asserts that prompt acquisition sends no such command.

It now also requires a fresh `OpenSBI -> U-Boot -> =>` epoch before any load or
`booti` command, and a required-recovery run writes the stable workspace latch
`target/megrez-board-runs/.recovery-safety.json`. A later run is refused until a newer schema-v2 RockOS attestation receipt
proves a new `rockos_boot_id`, `measurement_nonce`, and
`rockos_recovered=true`. The first post-reset diagnostic run can use the
RAM-only `--device-profile minimal`, which disables GMAC and both DWC3 paths
before booting the basic console.

The existing boot guide already documents that `reboot_after` is a software
fallback rather than a hardware watchdog. This incident is the concrete
evidence that an external reset/watchdog path is required for unattended
testing of failures that disable timer interrupts or SBI reset.
