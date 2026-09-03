# Megrez Desktop-Only Boot Design

## Goal

Boot the existing Debian desktop on Asterinas/Megrez to a usable local GUI
without making Ethernet or a browser part of the startup path.  The milestone
is a visible Openbox desktop with PCManFM, LXPanel, and an interactive xterm,
using the physical keyboard and mouse already exposed by Asterinas.

This work does not remove the existing network and browser profiles.  It adds
an explicitly selected desktop-only path so those profiles can be resumed
later without changing their contracts.

## Scope

The desktop-only path must:

- boot the persistent Debian root on `/dev/mmcblk1p2` through Asterinas;
- start Xorg on `/dev/fb0` and use the existing evdev keyboard and pointer;
- start Openbox, PCManFM desktop management, LXPanel, and xterm;
- never start NetSurf as part of the session;
- omit `asterinas.net`, static-neighbor, proxy, and network-fixture boot
  arguments;
- publish ordered serial milestones ending in a desktop-ready marker;
- retain the bounded software reboot used to recover the board;
- preserve the existing `desktop-m4`, `desktop-m5-network`, and browser
  behavior when desktop-only mode is not selected.

Network correctness, NetSurf/Firefox behavior, web compatibility, package
installation, and desktop visual redesign are outside this milestone.

## Selected Approach

Use a runtime desktop-only mode on the existing M4 session and M5 rootfs.
This is preferable to falling back to M3 because M3 proves only Matchbox and
xterm, and preferable to building a new rootfs profile because the board
already contains every required desktop package.

The mode is selected by a systemd environment variable supplied on the
kernel command line.  The existing session script starts the desktop shell
and terminal in both modes, but starts NetSurf only in the legacy application
mode.  The evidence script similarly uses a browser-free readiness contract
when desktop-only mode is selected.

## Components

### Desktop session

`tools/riscv/debian/rootfs/desktop_m4_session.sh` will accept a boolean
`ASTERINAS_DESKTOP_BROWSER_ENABLED` setting.  Its default remains enabled to
preserve existing M4/M5 behavior.  With the value `0`, the session will skip
all URL, proxy, and NetSurf setup while still launching Openbox, PCManFM,
LXPanel, and xterm.

### Desktop evidence

`tools/riscv/debian/rootfs/desktop_m4_evidence.sh` will select one of two
contracts.  The existing contract continues to require and identify NetSurf.
Desktop-only mode will require the window manager, file manager, panel, and
xterm but will neither inspect NetSurf nor move a browser window.  It will
emit a distinct clients marker and a distinct final ready marker so a stale
browser run cannot satisfy the new gate.

The failure path remains bounded and continues to print systemd, session,
Xorg, and X window-tree diagnostics to the serial console.  Browser logs are
included only when browser mode is enabled.

### Physical gate

`tools/riscv/megrez_gmac_gate.py` will gain a `desktop` target alongside the
existing `network` and `browser` targets.  Despite the historical module
name, this is the established physical-board lifecycle implementation and is
the narrowest safe place to add the target.

The desktop target will:

- require the desktop-only ordered milestones;
- set the M4 serial-evidence destination and disable the browser;
- retain `console`, `init`, writable partition 2, and bounded reboot
  arguments;
- omit GMAC address configuration, static neighbors, proxy variables, and
  fixture variables;
- avoid starting the host network fixture because no selected milestone uses
  it.

The serial session remains bounded, closes the device on every outcome, and
publishes an auditable transcript and result file.

### Existing board root

After local tests pass, boot the board's RockOS maintenance system, mount the
Asterinas partition, and install only the reviewed session/evidence scripts.
Back up the previous files on the same partition, synchronize, unmount, and
then return to U-Boot.  This avoids rebuilding or transferring the one-GiB
root image and gives a direct rollback path.

## Verification

Verification proceeds from cheap to hardware-specific:

1. Unit tests prove strict boolean parsing, no NetSurf launch or probe in
   desktop-only mode, unchanged default behavior, target-specific bootargs,
   ordered marker classification, and absence of host fixture startup.
2. Shell syntax and focused Python tests run locally in the project
   container.
3. The existing RISC-V QEMU desktop path runs with SMP=4 and captures serial
   evidence plus a framebuffer screenshot.  It must show the desktop shell
   and terminal without a NetSurf process/window.
4. A single bounded Megrez run starts from the current U-Boot prompt.  PASS
   requires the desktop-only ready marker; the HDMI view and keyboard/xterm
   interaction are supporting evidence.  The automatic reboot protects
   recovery if userspace stalls.

The run must be reported as failed or inconclusive if only the framebuffer
changes but the ordered serial evidence is absent.

## Success Criteria

The milestone is complete when one SMP=4 QEMU run and one bounded physical
Megrez run both prove:

- Xorg uses `/dev/fb0`;
- keyboard and pointer evdev devices are registered;
- Openbox, PCManFM desktop, LXPanel, and xterm are alive;
- an `Asterinas Terminal` window is visible;
- NetSurf is neither running nor required;
- no Asterinas network boot argument was supplied;
- the board remains recoverable without a physical reset.
