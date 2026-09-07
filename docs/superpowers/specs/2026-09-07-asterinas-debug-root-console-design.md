# Asterinas opt-in debug root console design

Date: 2026-09-07

## Goal

Allow an operator with physical serial access to enter a root Bash running on
the browser-web Debian userspace under the Asterinas kernel, while systemd and
the HDMI desktop continue to run. The normal boot path must remain unchanged
and must not gain a password, passwordless sudo, or a persistent root-login
configuration.

The physical acceptance signal is an interactive serial prompt preceded by
`ASTERINAS_DEBUG_CONSOLE_READY uid=0`, followed by successful `id`, PID 1,
mount, and service-status probes. A bounded test boot must recover without a
physical reset.

## Current behavior and root cause

The browser-web image contains an `asterinas` account, but its shadow field is
`!`. This has been the deliberate desktop-image policy since the account was
introduced, so there is no historical password to recover. The factory
`debian` / `debian` credential belongs to RockOS and is unrelated to the
Asterinas Debian root.

Stage1 already has an interactive root-Bash mode, but it accepts only the
separate `ASTER_DEBIANROOT` persistent-shell image. The installed browser root
is labelled `ASTER_BROWSERWEB` and is deliberately accepted only by the
systemd mode. Relaxing that label boundary would conflate two previously
separate rootfs contracts and would also stop the desktop from starting.

## Considered approaches

### Fixed root or desktop-user password

This is operationally familiar but puts a reusable credential in every image.
It also conflicts with the browser-image identity check that requires the
`asterinas` account to remain locked. This approach is rejected.

### Permanent passwordless sudo for `asterinas`

This preserves a non-root prompt but gives every process in the graphical
session a permanent route to root, even when the serial debug mode is not in
use. This approach is rejected.

### Opt-in ephemeral root console

This is the selected approach. An exact Stage1 argument enables a root console
for one boot. Stage1 materializes runtime-only systemd configuration beneath
the `/run` tmpfs before handing off to `/sbin/init`. No browser rootfs file is
changed, and rebooting removes the entire configuration.

## Boot interface

Stage1 accepts one new exact argument:

```text
--debug-console=root
```

It is valid only together with `--root-init=systemd`. Unknown values,
duplicates, control characters, and use with interactive mode fail closed.
The resulting U-Boot command line for a bounded physical test has this shape:

```text
console=ttyS0 loglevel=info init=/init \
asterinas.reboot_after=120 -- \
--root-init=systemd --debug-console=root
```

Normal boots omit the new argument and retain the existing login behavior.
The kernel recovery timer remains independent: it is present in test boots and
absent from an intentional operator handoff.

## Stage1 and systemd handoff

After mounting the selected browser root, binding `/dev`, and mounting `/run`,
Stage1 performs a separate, testable debug-console preparation step. It creates
only the following runtime objects under `/newroot/run`:

- an enable marker;
- a root-console Bash rc file with a distinctive readiness marker and prompt;
- `asterinas-debug-console.service` beneath `/run/systemd/system`;
- a runtime drop-in that prevents `console-getty.service` from competing for
  the same terminal;
- a `getty.target.wants` link for the debug service.

The debug unit runs `/bin/bash` as root on `/dev/console`, with
`StandardInput=tty-force`. HDMI continues to use `/dev/tty1`, so the serial
root console does not replace the graphical session. If the shell exits, the
unit restarts after a short delay so serial access can be recovered without
rebooting.

Every directory, regular file, and link is created beneath the already-mounted
`/run` tmpfs. Failure to create or validate any object aborts the handoff rather
than falling back to a partially configured systemd boot. Stage1 reports
ordered preparation markers to the serial log.

## Security boundary

This mechanism grants full root access to anyone who controls both the boot
arguments and physical serial input. That is intentional for development.
It does not authenticate the operator and is not suitable for an unattended
or production boot configuration.

The boundary remains explicit and reversible:

- no fixed password or password hash is introduced;
- `/etc/passwd`, `/etc/shadow`, and sudo policy are unchanged;
- the normal systemd path creates no debug unit;
- runtime objects disappear on reboot;
- U-Boot environment is not persisted with `saveenv`;
- physical validation retains the automatic recovery timer.

## Verification

Host and Stage1 self-tests cover exact argument parsing, invalid combinations,
handoff ordering, generated file contents, link targets, error propagation,
and absence of debug preparation in existing modes.

The QEMU gate performs two boots:

1. a normal browser-web boot that must reach the existing graphical markers
   and must not emit the debug-console marker;
2. a debug boot that must reach graphical target and the root-console marker,
   then run framed commands proving UID 0, PID 1 is systemd, the browser root is
   mounted, and the desktop service remains present.

The physical Megrez run uses SMP=4 and a 120-second recovery timer. It loads
CRC-checked kernel, Stage1, and DTB artifacts, confirms the exact
`ASTER_BROWSERWEB` root, runs the same framed root probes, and records the full
serial transcript. The final success condition also requires a fresh recovery
epoch and successful RockOS SSH reachability; no physical reset is part of the
test procedure.

## Scope

This work provides a reliable local root debugging channel. It does not enable
remote root login, change browser/network acceptance, add a user password, or
alter the production desktop security policy. SSH key-based access can be
designed separately after the Asterinas network path is sufficiently stable.
