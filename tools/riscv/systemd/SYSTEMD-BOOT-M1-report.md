# SYSTEMD-BOOT-M1 — systemd boots as PID 1 on Asterinas RISC-V

Date: 2026-08-14
Status: **MILESTONE ACHIEVED** — systemd 257.5 prints its version banner,
detects riscv64, sets the hostname, and reaches `basic.target` (with
`sysinit.target` → `local-fs.target`/`swap.target` satisfied along the way).
"Startup finished in 5.3s (kernel) + 2.4s (userspace)". One kernel gap
(cgroup-v2 `memory.max` write) is fixed this milestone; the rest are catalogued
below with root cause and a fix path.

## Objective

The B session already cross-compiled systemd 257.5 for riscv64 glibc
(878/878 ninja targets — see the sibling tree
`tools/riscv/systemd/SYSTEMD-M2-report.md`). That build is **dynamically-linked
PIE**, so running it needs a working dynamic loader + glibc runtime on the
kernel. This milestone ports those binaries into this tree's rootfs, boots
systemd as PID 1 under QEMU, observes how far it gets, and fixes the kernel
gaps it exposes.

## Deliverables (`tools/riscv/systemd/`)

| File | Purpose |
|---|---|
| `build_systemd_boot.sh` | assemble the rootfs + initramfs (systemd + glibc runtime + busybox + units) |
| `init.c` | static `/init` launcher that `exec()`s systemd as PID 1 |
| `boot_systemd_smoke.py` | QEMU driver: U-Boot `booti` handoff → collect → report milestones |
| `gate.sh` | one-command gate: build → repack boot disk → boot → report |
| `SYSTEMD-BOOT-M1-report.md` | this report |

## Assembly — how the rootfs is put together

`build_systemd_boot.sh` produces `target/nixos/systemd/rootfs` (45 MB) and
packs it as **raw newc cpio** (no gzip — the kernel's zune-inflate decoder hangs
non-deterministically on >16 MB gzip inputs, cf. `nixos/m3/M3-report.md`).

Layout and the two things that were genuinely hard:

```
/init                                              static launcher (exec's systemd)
/lib/{ld-linux-riscv64-lp64d.so.1, libc.so.6,     glibc 2.41 runtime + systemd's
      libm.so.6, libdl.so.2, librt.so.1,            two internal .so files
      libpthread.so.0, libgcc_s.so.1,
      libsystemd-core-257.so, libsystemd-shared-257.so}
/usr/lib/systemd/{systemd, systemd-journald,        pid1 + all 69 helper ELFs
                  systemd-executor, ...}
/usr/bin/*                                          symlinks -> ../lib/systemd/*
/etc/os-release, machine-id, hostname, passwd, group
/etc/systemd/system/{default.target -> basic.target,
                     basic.target, sysinit.target, local-fs.target,
                     swap.target, emergency.target, emergency.service}
/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr -> /usr   (*)
```

(*) **The baked-host-path bridge.** systemd was built with
`--prefix=/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr` and
*never `meson install`ed*, so every executable path is compiled into `config.h`
as that host absolute path (e.g. `SYSTEMD_EXECUTOR_BINARY_PATH =
/home/…/usr/lib/systemd/systemd-executor`). At runtime pid1 does
`pin_callout_binary(SYSTEMD_EXECUTOR_BINARY_PATH)` **inside `manager_new()` and
returns its error verbatim** (`src/core/manager.c:1055`). On the first boot
this surfaced as:

```
Failed to pin executor binary: No such file or directory
Failed to allocate manager object: No such file or directory
Freezing execution.
```

Two fixes, both in the rootfs (no kernel change): (a) copy **all 69** systemd
helper ELFs into `/usr/lib/systemd/` (not just pid1 + journald), and (b) add the
single `…/target/riscv-cross/usr -> /usr` symlink so *every* baked host path
resolves to the guest's canonical `/usr`. The internal `.so` files are placed in
`/lib` (the loader's default search path), which is consulted after the
`$ORIGIN/src/{core,shared}` meson rpath placeholder misses.

## Result — how far systemd got

Stripped-of-ANSI serial transcript (all present, in order):

```
systemd 257.5 running in system mode (-PAM -AUDIT … +ZLIB … +SYSVINIT …)
Detected architecture riscv64.
Welcome to Asterinas RISC-V (systemd bootstrap)!
Hostname set to <asterinas-riscv>.
Found cgroup2 on /sys/fs/cgroup/, full unified hierarchy
[  OK  ] Reached target Local File Systems.
[  OK  ] Reached target Swap.
[  OK  ] Reached target System Initialization.
[  OK  ] Reached target Basic System.
Startup finished in 5.293s (kernel) + 2.425s (userspace) = 7.718s.
```

This satisfies the success criterion: **systemd prints its version and starts
executing units** (`default.target` → `basic.target` → `sysinit.target` →
`local-fs.target`/`swap.target`). The emergency-shell fallback was not needed.

## Gap list

### 1. Unimplemented syscalls (all return ENOSYS, all survivable)

| # | name | caller / impact |
|---|---|---|
| 258 | `riscv_hwprobe` | glibc startup CPU probe (harmless) |
| 293 | `rseq` | glibc per-thread registration (harmless) |
| 170 | `settimeofday` | systemd time sync — cannot set the RTC/clock |
| 264 | `name_to_handle_at` | systemd `fd_is_mount_point` path — mount detection degraded |
| 280 | `bpf` | cgroup BPF device/firewall controller — already `-D…=false`, degrade only |
| 285 | `copy_file_range` | journal/`journalctl` copy fast-path |

None of these block boot; systemd logs-and-continues. `bpf` and
`name_to_handle_at` are the two that would matter for a full systemd (device
policy, mount detection). All are one-line stubs if a minimal `ENOSYS`→`0`
shim is acceptable; real implementations are larger.

### 2. Functional gaps (not ENOSYS — behavior missing)

| Symptom (boot log) | Root cause | Fix path |
|---|---|---|
| `Failed to open netlink, ignoring: Protocol not available` / `Failed to start device monitor: Protocol not available` | **AF_NETLINK not implemented** — systemd's udev device monitor + `sd-device` enumerate over netlink | implement `AF_NETLINK` (socket) — large subsystem |
| `init.scope: Failed to set 'memory.max' attribute on '/init.scope' to 'max': Input/output error` | cgroup-v2 `memory.max` was registered **read-only** and `write_attr` was a `TODO` stub | **FIXED this milestone** (see below) |
| `Failed to bump fs.file-max / fs.nr_open` | `/proc/sys/fs/{file-max,nr_open}` writes unsupported | implement `procfs` sysctl write for these two keys |
| `Failed to enable kbrequest handling, ignoring: Inappropriate ioctl for device` | console `KIOCSOUND`/keyboard ioctl missing | minor tty ioctl |
| `Failed to acquire watch file descriptor: Invalid argument` | `inotify_add_watch` on `/proc/self/mountinfo` (mount-event watch) returns EINVAL | inotify mountinfo watch |
| `TFD_TIMER_CANCEL_ON_SET is not implemented yet and has no effect` | `timerfd_settime` flag | minor timerfd |
| `unsupported wait options are found: WEXITED` | `waitid(2)` options mask incomplete | minor waitid |

### 3. Cosmetic (taint) warnings

`System is tainted: unmerged-usr:unmerged-bin:var-run-bad` — systemd flags that
`/bin`/`/usr/bin` aren't merged and `/var/run` isn't a symlink to `/run`. Pure
cosmetics; fixed by `ln -s ../usr/bin /bin`-style merged-usr symlinks and
`/var/run -> /run`. Not worth the rootfs churn for this milestone.

## Kernel fix landed this milestone: cgroup-v2 `memory.max`

`kernel/src/fs/fs_impls/cgroupfs/controller/memory.rs` had `memory.max`
registered with `SysPerms::DEFAULT_RO_ATTR_PERMS` and both `read_attr_at` /
`write_attr` returning `Error::AttributeError` (TODO). Mirrored the existing
`pids` controller:

- `MemoryController` now holds `max_memory: AtomicU64` (`u64::MAX` = unlimited).
- `memory.max` registered `DEFAULT_RW_ATTR_PERMS`.
- `read_attr_at` prints `"max"` or the byte value; `write_attr` accepts `"max"`
  or a `u64` and stores it (limit is recorded, not yet *enforced* — enforcement
  needs mm-level accounting, out of scope).

This clears the only systemd log line that was a genuine resource-control
failure (**verified**: `memory.max` is absent from the post-fix boot transcript,
which still reaches `basic.target`). The other memory interfaces
(`memory.events`, `memory.stat`) remain read-only stubs and are documented as
future work.

## Reproduce

```bash
# 1. Build the initramfs (copies systemd binaries + glibc runtime from the
#    sibling tree /home/arch-anjie/Program/asterinas-riscv; see build script).
tools/riscv/systemd/build_systemd_boot.sh

# 2. Build + repack + boot + report (rebuilds the kernel first to pick up the
#    memory.max fix):
tools/riscv/systemd/gate.sh                # or: fm4-style --rebuild-kernel first
python3 tools/riscv/systemd/boot_systemd_smoke.py   # boot-only, against the repacked disk
# serial transcript: target/nixos/systemd/systemd-serial.log.smp1
```

The boot driver strips ANSI before matching and reports each milestone
(`init-launcher`, `systemd-banner`, `arch-riscv64`, `sysinit-target`,
`basic-target`, `startup-finished`) plus the unimplemented-syscall count and any
panic markers. Exit 0 when the banner and either `basic-target` or `emergency`
are seen.

## Next steps

1. **AF_NETLINK** — the single biggest remaining gap; unblocks udev device
   enumeration/monitoring, which is what `basic.target` → real multi-user
   systemd needs next.
2. **cgroup-v2 accounting** — implement `memory.current`/`memory.stat` reads and
   actually enforce `memory.max`/`pids.max` (pids is already enforced).
3. **`name_to_handle_at` + `bpf`** — for mount-point detection and device
   allow-lists.
4. **Expand the unit set** to the stock `systemd-journald`, `systemd-tmpfiles`,
   `systemd-sysctl`, `getty@ttyS0` units (the binaries are already in the
   rootfs) and observe the next layer of gaps.
5. **Clean `--prefix=/usr` install** — re-configure+install systemd with
   `--prefix=/usr` in the sibling tree (or a local copy) so the baked-host-path
   symlink bridge can be dropped.
