# Debian systemd Mount-Monitor M3 Design

Date: 2026-08-25

## Goal

Make Debian's packaged systemd and libmount observe ordinary mount operations
correctly on Asterinas. This is the next desktop-foundation milestone after the
real Megrez systemd M2 two-boot pass: desktop sessions depend on reliable
runtime mounts before they depend on Xorg, a display manager, or a browser.

M3 must preserve the signed Debian root, Stage1 systemd handoff, normal reboot,
and persistent second-boot evidence already established by M2.

## Confirmed failure

Both QEMU and Megrez M2 transcripts contain this sequence:

```text
Failed to acquire watch file descriptor: Invalid argument
Failed to drain libmount events: Invalid argument
run-lock.mount: Mount process finished, but there is no mount.
tmp.mount: Mount process finished, but there is no mount.
```

systemd 257 creates a libmount monitor and watches its top-level descriptor.
util-linux 2.41 enables the userspace monitor with an inotify watch containing
`IN_CREATE | IN_ISDIR`. Linux accepts `IN_ISDIR` in an inotify watch mask and
also returns it on events concerning directories.

Asterinas already emits `FsEvents::ISDIR` for directory creation and deletion,
but `InotifyEvents` omits the same bit. The syscall boundary therefore treats
the legitimate mask as unknown and returns `EINVAL`. This is the first proven
root cause.

## Approaches considered

### 1. Repair the Linux ABI in layers (chosen)

First accept and report `IN_ISDIR`, then rerun one targeted systemd gate. Add
event-driven `/proc/self/mountinfo` polling only if that gate proves the kernel
monitor is still incomplete. This keeps each kernel change tied to observable
Linux behavior and avoids speculative mount-namespace machinery.

### 2. Suppress or replace the Debian mount units

Stage1 could pre-mount every runtime filesystem, or the image could mask
`tmp.mount` and `run-lock.mount`. This is rejected because it hides a kernel
compatibility defect, diverges from normal Debian behavior, and would recur in
desktop and container workloads.

### 3. Install a desktop profile immediately

Adding Xorg, a window manager, and applications now could produce an earlier
visual demo. This is rejected for M3 because systemd mount tracking, sessions,
udev, and runtime directories would remain unreliable, making desktop failures
hard to attribute.

## Scope

M3 includes:

- Linux-compatible `IN_ISDIR` input-mask acceptance;
- correct `IN_ISDIR` output on directory events;
- a regression test through the public inotify syscall behavior;
- one QEMU generic-Sv39, SMP=4, 2 GiB, networkless systemd gate;
- transcript assertions for libmount setup/drain and mount-unit protocol errors;
- a conditional second slice for `/proc/self/mountinfo` poll semantics, entered
  only if the first gate remains red for a kernel-monitor reason;
- concise evidence documenting what changed and what remains unsupported.

M3 does not add desktop packages, networking, a display manager, graphics,
USB/xHCI, or a board image update. It does not modify the signed Debian root
unless a focused guest diagnostic is strictly necessary.

## Inotify contract

`inotify_add_watch(directory, IN_CREATE | IN_ISDIR)` must succeed. Creating a
subdirectory must produce an event with all of these properties:

- the watch descriptor matches;
- `IN_CREATE` is present;
- `IN_ISDIR` is present;
- the event name matches the created directory;
- no unknown input bit is silently accepted.

The implementation adds the Linux UAPI bit to `InotifyEvents`; existing VFS
notification code remains the single source of directory-event classification.

## Conditional mountinfo contract

libmount also registers `/proc/self/mountinfo` in its epoll set. The first M3
QEMU run determines whether the userspace monitor is sufficient for the Debian
mount units. If not, the next RED must demonstrate this public behavior:

1. open `/proc/self/mountinfo` and register it with epoll;
2. drain the initial state;
3. create or remove a mount in the same mount namespace;
4. observe one readiness edge;
5. reread mountinfo and observe the topology change;
6. after draining, do not remain spuriously readable.

Any implementation must attach notification state to the relevant mount
namespace rather than a global flag, notify only after committed topology
changes, and use a per-open procfs handle. It will not be implemented merely
because the current generic inode handle reports ordinary files as readable.

## QEMU gate

Reuse the frozen M2 signed root, Stage1 archive, generic-Sv39 CPU arguments,
four-hart DTB, private writable root copy, 2 GiB RAM, no network, no display,
bounded transcript, and full process-group teardown. Rebuild only the kernel
and initramfs inputs affected by M3.

The gate must retain the ordered boot-1, userspace reboot, boot-2, and PASS
markers. In addition, the complete transcript must not contain:

```text
Failed to acquire watch file descriptor
Failed to drain libmount events
Mount process finished, but there is no mount
```

The guest must show `/tmp` and `/run/lock` as mount points after systemd has
processed the units. If fixing `IN_ISDIR` changes the failure but does not meet
this contract, that evidence becomes the RED for the conditional mountinfo
slice.

## Desktop direction after M3

The next desktop milestone will define a separate signed profile rather than
mutating M2. Its first acceptance surface is a real login session with correct
`/run`, `/tmp`, D-Bus, logind, devtmpfs/udev-facing devices, and a serial
fallback. Xorg, input, framebuffer/DRM, a lightweight window manager, file
manager, terminal, and browser are added only after that base session is green.

This orders work by user-visible value while preserving a diagnosable kernel
foundation.

## Verification and stopping rules

1. Capture a focused regression RED on current Asterinas.
2. Make only the `IN_ISDIR` ABI change and run the focused test.
3. Run kernel compile/lint checks proportional to the two changed files.
4. Run one new QEMU M3 gate; do not repeat an unchanged passing gate.
5. Implement mountinfo notifications only with a new public-behavior RED.
6. Update Megrez only after QEMU is green and only when a new board artifact is
   justified.

M3 is complete when the new QEMU gate passes without the three libmount/mount
protocol failures and the M2 persistence/reboot contract still passes. It does
not claim a usable graphical desktop.
