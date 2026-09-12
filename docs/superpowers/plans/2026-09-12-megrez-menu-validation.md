# Megrez menu validation and incident notes

## Scope and artifact provenance

Worktree: `codex/megrez-boot-main`, based on main `7767494e6` plus the
approved design commit `2203bcb17`. No network-integration source was merged.
The kernel is the previously hardware-tested frozen integration artifact,
not a claim that this boot-only branch independently builds that binary.

Candidate 2 identities:

| Object | SHA-256 |
| --- | --- |
| Kernel | `485b9079c204bf6b34055f5e1061f3011381557d8cc4b4bf2d1e4831922058c1` |
| Shared Stage1 | `d62ab8325e03ec9959a82336ad7ddab2d433d9e6dfc1006d6237fa9f6c80c1e0` |
| Menu | `02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26` |

The manifest also records the exact prepared DTB and vendor configuration.
Local evidence is under `target/megrez-menu/`; it is deliberately not committed
as generated binaries or large serial logs. Candidate-1 tests never qualify
candidate 2. Failed/interrupted cycles never count as passed recovery cycles.

## Software evidence

- Basic QEMU, exact candidate-2 Stage1: 7.011 seconds, pass.
- Automatic Probe QEMU, same Stage1: 5.936 seconds, pass.
- Both use SMP4 `virt` with no disk or network device, exercising lightweight
  startup independently of the Debian partition.
- The final focused native suite passed 129 tests, including the short-command,
  QEMU-launch cleanup, DTB and BusyBox-closure preparation regressions.
- Rebuilding Stage1 after the last self-test/source-layout changes produced
  exactly the candidate-2 SHA-256 above.
- Shell-executed publication tests verify that a corrupt installed artifact
  stops publication and leaves the previous selector untouched. Promotion
  tests corrupt a referenced DTB, prove rejection, restore it, then verify an
  atomic selector replacement and a recoverable backup.

## Physical baseline results (candidate 2)

| Mode | Successful cycles | Menu to readiness (seconds) |
| --- | --- | --- |
| Basic | 3 | 8.676, 8.738, 8.714 |
| Probe | 3 | 8.514, 8.506, 8.512 |
| RockOS default | 3 | 30.606, 31.428, 32.599 |
| Missing-selector fallback | 3 | 21.607, 21.503, 21.986 |

Every listed cycle includes a successful software reboot and fresh firmware
recovery. RockOS/default numbers include the ten-second menu timeout; fallback
selects the original vendor default explicitly. The preceding firmware bootdelay
is not part of these menu-to-readiness numbers. Six read-only Debian filesystem
checks completed with `ROOTFS_CHECK_RC=0`. No artifacts were transferred between
these cycles.

Desktop qualification additionally requires a visible Firefox X window. Earlier
process-only checks are diagnostic evidence, not completed Desktop qualification.
Promotion and cold power-on remain gated on the implementation plan.

## Incidents and preventive changes

### MEGREZ-MENU-DESKTOP-001: isolated console is not a desktop boot

Candidate 1 reused `--debug-console=isolated-root` from the host-driven graphics
experiment. This deliberately selects `asterinas-debug-console.target` and
therefore does not automatically start the graphical target. The evidence was
an interactive PID-1 systemd/root shell, no X socket, and that exact default
target, not a Firefox crash.

Candidate 2 uses ordinary `--debug-console=root` and `--volatile-home`.
Stage1 prepares the temporary HOME before Debian handoff; no host command is
needed to start X/Firefox. The manifest rejects isolated-root for Desktop.
Native tests cover option conflicts, handoff order and preparation failure.

### MEGREZ-MENU-SERIAL-001: shell checks must not use U-Boot echo rules

A long readiness command produced a Bash syntax error; its received echo was
incomplete. The replacement uses short, individually acknowledged commands.
An additional failure showed `BoardSession.command` rejecting a valid command
because Bash redraws wrapped input with its own prompt. That helper is for
U-Boot echo/error rules. Shell checks now use `send`/`wait_for` and require an
explicit numeric result line, distinguishing command loss from not-yet-ready
services. A regression prohibits use of the U-Boot command helper here.

The underlying cause of the long-line loss is not claimed to be fixed in the
UART driver. Boot-menu selection itself sends only a single digit.

### MEGREZ-MENU-REBOOT-001: logind error is not the terminal reboot state

Ordinary `systemctl reboot` reported a logind unit error but eventually rebooted.
The initial checker was interrupted before that later firmware epoch; the run
is retained as failed, not retrospectively converted to success. A subsequent
`sync; systemctl --force reboot` returned through fresh OpenSBI/U-Boot without
operator reset. The single-force form bypasses logind; the workflow does not use
the double-force immediate reboot path. Shutdown latency remains measurable
and must not be hidden as an instant recovery promise.

### MEGREZ-MENU-DESKTOP-002: a process is not a visible browser window

The stricter candidate-2 Desktop test expired after 258.6 seconds total, with
X/Firefox processes but no visible Firefox window within its observation bound.
The same boot was retained. At guest uptime 282.92 seconds, `xwininfo -root -tree`
showed the 1280x972 Mozilla Firefox window; a subsequent independent query
reported `Map State: IsViewable` and `xdotool` returned window ID 6291498.
The journal also reported Marionette listening on port 2828, slow graphics
probing and delayed main-thread events. This proves eventual automatic startup,
not that those performance defects have been fixed.

The original failed cycle remains failed. Subsequent qualification allows 300
seconds after console readiness, still requires a visible window and fresh
firmware recovery, and reports actual timings. This host-only deadline change
does not alter the frozen menu/kernel/Stage1 or impose a guest reboot timer.

### MEGREZ-UBOOT-EMPTY-COMMAND: never acquire a prompt with an empty command

An empty U-Boot line repeats the prior command, which could be `booti`, a load,
or a memory dump. RockOS attestation and the menu controller now use Ctrl-C to
acquire the prompt, without executing command history. A mocked serial test
verifies that this path does not send an empty command.

## Claim boundaries

Desktop checks prove a running X server socket, framebuffer device and Firefox
process, not rendered webpage correctness or a working external network.
Three existing Debian services were reported failed (network evidence, browser
evidence and sysctl); cmdline masks did not prevent the first two from running
on this installed image. These are not silently reclassified as a clean systemd
boot. No rootfs image, vendor boot file or persistent U-Boot environment was
rewritten. The desktop itself can write its mounted Debian root filesystem.

Hardware reset/power-cycle results must be separate from software reboot logs.
No cold-start or USB-keyboard-menu success is inferred from serial testing.
