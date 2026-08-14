# SYSTEMD-BOOT-M2 — from basic.target to a usable systemd system

Date: 2026-08-14
Status: **MILESTONE (with one open gap)** — systemd 257.5 now drives a real
login session: `getty@ttyS0` prompts, `login` drops to an interactive root
shell, and `systemctl start/status/stop` manages custom services. Lifecycle
(`Type=simple` + `Type=forking`) and socket activation are verified end-to-end.
`systemd-journald` starts and stays active, but journal-file **writing** still
fails (ENOENT), so `journalctl` cannot yet read entries.

## Objective

M1 ended at `basic.target` with systemd merely *printing* its banner and
satisfying targets. M2 turns that into a system you can actually drive:

1. **getty@ttyS0** — a serial login prompt and an interactive shell.
2. **Custom service units** — `systemctl start` a `Type=simple` and a
   `Type=forking` service, and observe the full lifecycle.
3. **Socket activation** — systemd's signature AF_UNIX fd-passing mechanism.
4. **journald** — running state and log output.
5. QEMU-verify each step and fix the kernel gaps it exposes, in incremental
   commits.

## Deliverables (`tools/riscv/systemd/`)

| File | Purpose |
|---|---|
| `units/{multi-user,target,getty@.service,getty.target}` | login-prompts chain (default.target → multi-user.target → getty.target → getty@ttyS0) |
| `units/{simpletest,forktest}.service` | lifecycle probes (`Type=simple`, `Type=forking`) |
| `units/{socktest.socket,socktest.service}` | socket-activation pair (`ListenStream=/run/socktest.sock`) |
| `units/{systemd-journald.service,systemd-journald.socket,systemd-journald-dev-log.socket}` | journald + native + `/dev/log` sockets |
| `src/{simpletest,forktest,socktest,sockclient}.c` | static riscv64 test programs |
| `boot_systemd_m2.py` | interactive driver: boot → login → run a probe script |
| `gate_m2.sh` | one-command gate (`--rebuild-kernel` optional) |
| `build_systemd_boot.sh` | extended to install the M2 units/programs and a login-ready `/etc/passwd` |

Plus `tools/riscv/nixos/build_busybox.sh` now builds the `getty`, `login`,
`logger` and `hostname` applets, with `CONFIG_USE_BB_PWD_GRP`/`USE_BB_CRYPT`
so login can read `/etc/passwd` in a static-glibc binary.

## What now works (verified in QEMU)

The driver logs in and runs 15 probes. Result of the final gate:

```
getty-login-prompt   OK   "asterinas-riscv login: root"
login-shell          OK   "login[2]: root login on 'ttyS0'" → ___LOGIN_SHELL_READY___
shell-echo           OK
simpletest-start     OK   systemctl start simpletest
simpletest-marker    OK   "simpletest started pid=7"       (ExecStart ran)
simpletest-active    OK   systemctl is-active simpletest
forktest-start       OK   systemctl start forktest         (Type=forking)
forktest-pid         OK   "15"                             (child wrote pidfile)
forktest-active      OK   systemctl is-active forktest
socktest-socket-start OK  systemctl start socktest.socket
socktest-connect     OK   "hello-from-socket-activated-service"
journald-start       OK   systemctl start systemd-journald
journald-active      OK   systemctl is-active systemd-journald
journald-log         FAIL journalctl cannot read entries (see gap #1)
simpletest-stop      OK   systemctl stop simpletest → inactive
```

**Login path.** busybox `getty 115200 ttyS0` opens `/dev/ttyS0`, sets termios,
prompts, and `exec()`s `/bin/login`; login (with `USE_BB_PWD_GRP`) reads
`root::0:0:…` and spawns `/bin/sh` (ash) which sources `/etc/profile`
(`PS1='root@asterinas:~# '`). The whole controlling-terminal/session/job-control
stack (TIOCSCTTY, TCGETS/TCSETS, setsid, setpgid) was already present in the
kernel and needed no changes.

**Service lifecycle.** `systemctl start/status/stop` works end-to-end, proving
systemd's private bus socket (`/run/systemd/private`, SO_PEERCRED + SCM_CREDENTIALS)
and its `fork()`/`exec()` child machinery are functional.

**Socket activation.** `socktest.socket` (`ListenStream`, `Accept=no`) hands the
already-listening fd to `socktest.service` as fd 3 with `LISTEN_FDS=1`; the
client gets `hello-from-socket-activated-service`.

## Kernel gaps fixed this milestone (2 incremental commits)

### 1. `keyctl` — `KEYCTL_LINK`/`KEYCTL_SETPERM`/… no-ops (commit 39dedb0aa)

`getty@ttyS0` would not spawn at all: systemd's exec step
`setup_keyring()` (src/core/exec-invoke.c) walks the session keyring for every
service and **aborts the service at the `KEYRING` exec step** when
`keyctl(KEYCTL_LINK)` / `KEYCTL_SETPERM` return `EOPNOTSUPP`:

```
getty@ttyS0.service: Failed to restrict invocation ID permission: Operation not supported
getty@ttyS0.service: Failed at step KEYRING spawning /bin/getty: Operation not supported
```

The minimal keyring already handed out serials for
`GET_KEYRING_ID`/`JOIN_SESSION_KEYRING`/`REVOKE`; the remaining common ops are
now no-op successes (keys are never retained) and `KEYCTL_SEARCH` returns
`ENOKEY`.

### 2. `SO_TIMESTAMP` — accepted as a no-op (commit bf51bab24)

`systemd-journald` sets `SO_TIMESTAMP` on its native and syslog datagram sockets
and treats failure as fatal (`journald-native.c:496`). The kernel returned
`ENOPROTOOPT`, so journald exit-1 crash-looped. `SO_TIMESTAMP` (=29) is now a
settable bool option that is accepted but has no observable effect (no
`SCM_TIMESTAMP` cmsg is attached); journald falls back to its own clock. With
this, journald starts and stays active:

```
systemd-journald running as PID 29 for the system.
```

## Remaining gaps

### 1. journal file *write* fails — `journalctl` cannot read (the one M2 gap)

journald now runs but every entry append fails:

```
Failed to write entry to /run/log/journal/<machine-id>/system.journal (20 items, 554 bytes): No such file or directory
Unexpected error while writing to journal file: No such file or directory
```

`journalctl -b` reports `Failed to get boot ID: No such file or directory`.
The directory `/run/log/journal/<machine-id>/` is created, the journal file is
opened and hash tables are reserved, so this is in the mmap/`pwrite`/`fstat`
append path (journald's `journal_file_append_entry` → mmap-cache window), not a
missing syscall — `fallocate` and `ftruncate` are implemented. Root-causing this
is the next step; it is the only remaining blocker to a fully functional
journal.

### 2. `/dev/kmsg` cannot be opened rw

`Failed to open /dev/kmsg for rw access, ignoring: No such file or directory`.
journald keeps running but cannot forward kernel messages into the journal.

### 3. `SO_PASSSEC` not implemented

`PassSecurity=yes` → `setsockopt(SO_PASSSEC)` returns `ENOPROTOOPT`. Worked
around in M2 by omitting `PassSecurity=yes` from the journald socket units;
security labels are not needed without an LSM.

### 4. `CLONE_NEWUSER` unsupported (user namespaces)

systemd's exec path calls `unshare(CLONE_NEWUSER)` (non-fatal warning). User
namespaces are a prerequisite for full NixOS sandboxing.

### 5. Pre-existing M1 gaps (still open, now with journald context)

| Gap | Impact |
|---|---|
| `AF_NETLINK` | no udev device monitor; `Failed to start device monitor: Protocol not available` |
| cgroup-v2 per-unit accounting | `cgroup is not supported` on every service start; `cg_get_root_path` degrades |
| `name_to_handle_at`, `bpf` | mount detection + device policy |
| `copy_file_range` | journal copy fast-path |
| `riscv_hwprobe`, `rseq`, `settimeofday` | glibc probes / time sync (harmless) |
| `waitid` WEXITED, `TFD_TIMER_CANCEL_ON_SET`, inotify `mountinfo` | minor |

## NixOS stage-2 implications

- **Login + service manager are now usable** — stage-2 can rely on systemd to
  start getty and run units.
- **Socket activation is proven** — the mechanism NixOS units (D-Bus, daemons)
  depend on works.
- **journald must work before stage-2 diagnostics are useful** — gap #1
  (journal write) is the highest-value next fix.
- **AF_NETLINK** remains the largest subsystem gap (udev), as flagged in M1.

## Reproduce

```bash
# rebuild busybox with getty/login (once):
tools/riscv/nixos/build_busybox.sh

# full gate (rebuilds kernel only with --rebuild-kernel):
tools/riscv/systemd/gate_m2.sh                # or: --rebuild-kernel, --smp 4

# serial transcript: target/nixos/systemd/systemd-m2-serial.log.smp1
```

The driver matches a distinctive marker on each probe's *output* (it first
consumes the serial echo of the command, so `&& echo MARKER` markers are matched
on the command's real output, not on what was typed).
