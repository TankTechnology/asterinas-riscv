# Megrez Fast Desktop Boot Design

## Purpose

Provide one simple, bounded path that boots Asterinas on Megrez to a graphical
desktop with a visible Firefox window and a usable serial debug console. A
successful boot remains running until an explicit software reboot. A failed
boot returns to a fresh U-Boot prompt within five minutes without requesting a
physical reset.

This path is a prerequisite for Firefox performance work. It is not itself a
browser qualification or performance experiment.

## Current Problem

The existing physical Firefox runner combines artifact transfer, boot,
graphical admission, browser workload execution, evidence upload, and recovery.
Its recovery timer is 1,140 seconds. A failure before the debug console becomes
available therefore consumes about nineteen minutes before U-Boot recovery.

The current physical boot also selects `console=tty0`, so Stage1 progress and
failure markers are not visible on the serial console. The most recent boot
proved that the kernel and Stage1 archive load correctly and that the kernel
unpacks the initramfs, but it timed out before
`ASTERINAS_DEBUG_CONSOLE_READY`. The exact failed Stage1 or systemd boundary was
not observable.

The board's first partition is nearly full. Normal startup must not depend on
copying another generation of artifacts into `/boot`, and it must not silently
fall back to serial transfer.

## Goals

- Expose separate `prepare` and `start` commands.
- Make `start` the only normal daily-use entry point.
- Reach the desktop, Firefox window, and serial debug console within 300
  seconds.
- Keep a successful system running until an explicit software reboot.
- Recover a failed boot to U-Boot within 300 seconds plus a 60-second firmware
  recovery allowance.
- Show the last completed startup phase on the serial console and in retained
  host evidence.
- Verify immutable kernel, Stage1, and DTB identities before every boot.
- Never install or rewrite the Debian root partition during normal startup.
- Use RockOS and Asterinas software reboot paths; do not require physical
  reset for ordinary recovery.

## Non-Goals

- Installing, repairing, resizing, or hashing the complete Debian root image
  during `start`.
- Running Firefox daily-use, web-compatibility, input-latency, or performance
  workloads.
- Uploading browser evidence or starting host fixture servers.
- Deleting historical boot artifacts from the first partition.
- Automatically extending or rearming a watchdog after boot.

## User Interface

The implementation provides one host module with two explicit actions:

```text
python3 -m tools.riscv.megrez_desktop_boot prepare ...
python3 -m tools.riscv.megrez_desktop_boot start ...
```

Make targets wrap these commands for the repository's standard Megrez device
and current frozen plan:

```text
make prepare_riscv_megrez_desktop_boot ...
make run_riscv_megrez_desktop ...
```

`prepare` is required only when the kernel, Stage1, DTB, or boot manifest
changes. `start` refuses an unstaged generation with an actionable error that
names `prepare`; it does not transfer a replacement or boot RockOS implicitly.

Every invocation uses a fresh output directory and retains canonical
`result.json` and `serial.log` files. `start` prints the last observed phase
while it runs and exits zero only after the terminal ready marker has been
validated.

## Persistent Artifact Layout

RockOS partition 3 has sufficient space and is already readable by U-Boot.
`prepare` stores one immutable generation beneath:

```text
/home/debian/asterinas/boot/<generation-sha256-prefix>/
```

The generation contains the kernel, Stage1, prepared Megrez DTB, and a
canonical manifest. Artifact basenames include their SHA-256 prefixes.
`prepare` uses a temporary sibling path, verifies size and SHA-256, calls
`sync`, and renames the completed generation into place. It never overwrites a
different existing generation and never removes an older generation.

`start` loads all three artifacts directly from `mmc 1:3`. It verifies U-Boot's
reported byte count and CRC32 against the manifest before executing `booti`.
This avoids first-partition capacity pressure, network transfer, and YMODEM on
the normal path.

The Debian root remains partition 2. A separate installation or inventory
workflow establishes its full image identity. `start` relies on Stage1's exact
filesystem-label probe and records the expected root image identity from the
frozen plan, but does not hash 2 GiB on every boot.

## Boot Contract

The normal boot arguments include:

- serial-visible informational logging;
- the Stage1 `/init` entry point;
- a 300-second Asterinas recovery watchdog;
- the systemd root-init selector;
- an isolated serial debug console;
- the existing volatile desktop home policy; and
- the existing physical desktop service masks and offline browser policy.

The serial console is authoritative for the startup state machine. The ordered
phases are:

1. `kernel-entered`
2. `stage1-started`
3. `root-found`
4. `root-mounted`
5. `systemd-exec`
6. `debug-console-ready`
7. `display-ready`
8. `firefox-ready`
9. `watchdog-disarmed`
10. `desktop-ready`

Stage1 prints enter, success, and failure markers for every handoff operation.
The host rejects reordered, duplicate, malformed, or skipped required phases.

## One-Way Recovery Watchdog

The existing `asterinas.reboot_after=<seconds>` timer remains opt-in and uses a
frozen boot-time deadline. A new proc sysctl exposes its state and one-way
disarm operation:

```text
/proc/sys/kernel/asterinas_reboot_watchdog
```

Reading returns `1` while armed and `0` after disarm. A writer must have
`CAP_SYS_ADMIN`, begin at offset zero, and provide exactly the value `0` with
normal sysctl whitespace handling. Writing `1`, another integer, trailing
data, or an oversized payload fails. Disarming is idempotent. The interface
cannot arm, rearm, or extend a deadline.

The timer callback checks the armed state with acquire ordering immediately
before rearming or restarting. Disarm publishes false with release ordering.
Once disarmed, a stale timer interrupt returns without restarting the system.
The kernel emits exactly one serial marker when disarm first succeeds.

## Desktop Readiness and Disarm

Stage1 installs a transient systemd readiness service under
`/newroot/run/systemd/system`; the root image does not need to be rewritten to
change this startup control plane. The service runs from the Stage1 tools bind
mount and checks all of the following within the shared 300-second boot budget:

- the isolated debug console service is active;
- `/dev/fb0` and the X11 display socket exist;
- Xorg uses the fbdev path;
- Openbox is running;
- Firefox is running under the expected desktop user; and
- `xdotool` finds a visible Firefox window on display `:0`.

Only after all checks pass does the service write `0` to the watchdog sysctl.
It then reads the sysctl back, requires `0`, and emits the terminal
`ASTERINAS_DESKTOP_BOOT_READY` marker. A check failure emits a bounded reason
and exits nonzero without disarming the watchdog.

The host independently validates the terminal marker and performs short,
read-only debug-console queries for the X11 socket, Firefox PID, visible
window, watchdog state, and boot ID. The host does not write the disarm sysctl.
This preserves a guest-local safety boundary while giving the host independent
admission evidence.

## Failure and Recovery Semantics

`prepare` fails before mutation if its manifest, source artifacts, partition
identity, or destination is unsafe. Interrupted publication leaves only a
temporary path; a generation is visible only after complete verification and
rename.

`start` fails before `booti` if any persistent artifact is absent or has the
wrong byte count or CRC32. After `booti`, every failure keeps the watchdog
armed. The host observes the software reboot and requires a fresh OpenSBI,
U-Boot, and prompt sequence within 360 seconds of kernel entry.

If the debug console is available, diagnostics may issue an explicit guest
software reboot to shorten recovery. Commands are sent once and are never
retried blindly. The watchdog remains the fallback when Stage1 or systemd
fails before the debug console.

A missing U-Boot recovery marker is reported as a bounded failure with the
last startup phase. The tool does not request a physical reset.

## Evidence

`result.json` is the final commit marker and contains:

- pass/fail and stable reason;
- generation and plan SHA-256 identities;
- kernel, Stage1, DTB, and expected root image identities;
- exact persistent MMC paths;
- ordered phases and their host-monotonic timestamps;
- total boot-to-ready duration;
- boot ID and Firefox PID on success;
- watchdog armed/disarmed state;
- recovery status and recovery duration on failure; and
- SHA-256 of `serial.log`.

The result contains no performance claim. A later Firefox performance runner
may consume a passing boot result but remains a separate command and evidence
boundary.

## Verification Strategy

Unit tests cover manifest validation, safe MMC paths, state-machine ordering,
timeouts, result publication, and the rule that `start` never installs,
downloads, or transfers artifacts.

Kernel tests cover disabled, armed, first disarm, repeated disarm, stale timer,
deadline race, and invalid write states. Procfs tests cover permissions, exact
input parsing, readback, offset handling, and the prohibition on arming or
extending the watchdog.

Stage1 and shell tests cover transient service generation, every readiness
predicate, failure without disarm, successful disarm/readback, and the exact
terminal marker.

QEMU tests execute two cases with the same immutable generation:

1. readiness succeeds, the watchdog is disarmed, and the guest remains alive
   beyond the original deadline;
2. readiness is deliberately withheld, the watchdog fires, and a fresh U-Boot
   epoch appears within the recovery bound.

Physical validation first uses serial-visible Stage1 logging to identify and
fix the current root handoff failure. It then runs one injected failure recovery
and at least two consecutive successful `start` cycles. Each physical attempt
has a five-minute startup bound and a sixty-second firmware recovery allowance.

## Rollout

The new path is additive. Existing installation, boot-menu, physical-graphics,
and Firefox qualification tools remain available while the fast path is being
validated. After physical validation, documentation names `start` as the
normal desktop entry point and the performance workflow requires a passing
fast-boot result before starting a workload.

The existing uncommitted namespace and U-Boot prompt fixes remain separate
changes and are not folded into this design commit.
