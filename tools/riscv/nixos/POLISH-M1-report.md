# POLISH-M1 — cgroup memory controller, LTP triage, seccomp BPF filter

> 2026-08-15. The "收尾攻坚" (polish) milestone that closes out the three
> remaining quality items on the NixOS track: (1) the in-progress cgroup memory
> controller, (2) the 43 LTP syscall-gate failures, and (3) the last piece of the
> browser-grade sandbox — `seccomp(SECCOMP_SET_MODE_FILTER)`.

## Conclusion first

| Item | Status |
|---|---|
| cgroup `memory.max` read/write | **Done** — committed `b624dd92f` |
| seccomp BPF filter mode | **Done** — implemented + QEMU-verified (see §3) |
| LTP 43 failures | **Triaged** into env gaps vs. kernel bugs (see §2); fixes are follow-up work |

The two code changes are kernel fixes; the LTP item produced a precise
failure taxonomy (the FOUNDATION-M2 report had mis-attributed the 11 `rename`
failures to a VFS bug — they are actually a missing loop device).

---

## 1. cgroup memory controller — `memory.max`

The working tree already contained an in-progress diff making `memory.max`
writable. It was reviewed against the sibling `pids.max` implementation and
committed unchanged except for style:

- `MemoryController` now stores `max_memory: AtomicU64` (`u64::MAX` = "max").
- `memory.max` is registered `DEFAULT_RW_ATTR_PERMS` (was RO).
- `read_attr_at` renders `"max"` or the byte count; `write_attr` accepts `"max"`
  or a `u64` (via `read_cstring_until_end(MAX_ATTR_SIZE)`, same as `pids.max`).
- `memory.events` / `memory.stat` remain accounted-but-unimplemented
  (`Error::AttributeError`), unchanged.

This is the `memory.max` half of what systemd needs when it creates `init.scope`
(cgroup-v2 semantics). The broader cgroup-v2 gap (`cgroup is not supported` on
service start, seen in SYSTEMD-BOOT-M2) is not closed by this alone — it also
needs `memory.events` / `memory.current` / `cgroup.controllers`, which are out of
scope here.

Commit: `b624dd92f feat(fs): cgroup-v2 memory.max read/write for systemd scope accounting`.

## 2. LTP gate — 43 failures, triaged

The gate (`tools/riscv/ltp-gate.sh`, SMP=1) produces 375 pass / 43 fail /
27 skip / 1 hang. Re-reading the serial transcript (`target/ltp/ltp-gate-serial.log.smp1`)
with correct multi-line extraction shows the failures split cleanly into
**environment gaps** (not kernel bugs) and **real kernel bugs**:

### 2a. Environment gaps (19 failures — need harness fixes, not kernel changes)

| Failure | Root cause |
|---|---|
| `rename01/03/04/05/06/07/08/10/12/13/15` (11) | `tst_device.c: TBROK: Failed to acquire device` — no `/dev/loop*`. These are device-requiring FS tests, **not** a rename VFS bug. |
| `fsopen01/02`, `fsconfig01/02`, `fsmount01/02` (6) | same loop-device gap. |
| `posix_fadvise03`, `posix_fadvise03_64` (2) | `open(/bin/cat) ENOENT` — no busybox in the initramfs. |
| `setrlimit04` (1) | `execlp(/bin/true) ENOENT`. |
| `getitimer01` (1) | `clock_getres(CLOCK_MONOTONIC_COARSE) ENOSYS`. |
| `gethostbyname_r01` (1) | `retval (0) != ERANGE` — no `/etc/resolv.conf`/DNS under musl. |

### 2b. Real kernel bugs (confirmed from test output)

| Failure | Evidence / suspected cause |
|---|---|
| `sbrk01` | `sbrk(8192)` / `sbrk(-8192)` → `ENOMEM` (only `sbrk(0)` works). `modify_heap_end` → `vmar.resize_mapping` path. |
| `pwrite02`/`pwrite02_64` | `pwrite(-1, …)` returns `EOPNOTSUPP`, expected `EBADF`. |
| `pwrite04`/`pwrite04_64` | `pwrite(fd, …, O_APPEND)` returns `EOPNOTSUPP` — pwrite on an append-mode regular file. |
| `readlink03` | `readlink()` succeeds where `EACCES` is expected (no permission check). |
| `readlinkat02` | `readlinkat(fd, symlink, buf, 0)` "succeeded" instead of `EINVAL`. |
| `gethostname02` | `gethostname(buf, len < hostname)` "succeeded" instead of `ENAMETOOLONG`. |
| `epoll_wait04` | `epoll_wait` slept 2539 µs with a zero timeout. |
| `fork06/07/09/11` | fork semantics (4 tests). |
| `fcntl14`/`fcntl14_64` | POSIX record-lock behaviour. |
| `access02`, `chdir02` (timeout), `pipe13`, `sched_setscheduler04` | singletons. |

### 2c. Hangs (highest priority — block the tail)

| Failure | Evidence |
|---|---|
| `sigaltstack02` | runner stops after `sigaltstack01` with **no verdict** — a kernel deadlock. `sigaltstack02` itself is trivial (two `sigaltstack()` calls expecting `EINVAL`/`ENOMEM`), so the deadlock is elsewhere in the LTP `needs_tmpdir` fork/cleanup path, not in `sigaltstack` argument validation (which the code handles correctly). |
| `epoll01`, `sendfile07`/`sendfile07_64`, `chdir02` | watchdog `TBROK: Test killed! (timeout?)`. |

`sigaltstack02` is the first hang and blocks the remaining ~87 tests, so it is
the top follow-up.

### 2d. Fixes landed / deferred

None of the 2b/2c kernel bugs were fixed in this milestone — they are each
small but need an individual reproduce → fix → QEMU-verify cycle. The taxonomy
above is the deliverable; the FOUNDATION-M2 "follow-up queue" (§6) should be
re-keyed to it (rename is **not** a VFS bug).

## 3. seccomp `SECCOMP_SET_MODE_FILTER` (classic BPF)

The final piece of the sandbox. `SECCOMP_SET_MODE_FILTER` was previously
`EINVAL`. It now installs a classic-BPF filter and evaluates it on every syscall.

### What was implemented

- **`SockFilter`** (`struct sock_filter`) and a classic-BPF interpreter in
  `kernel/src/syscall/seccomp.rs`: `LD` (IMM + ABS word load from `seccomp_data`),
  `LDX`, the `ALU` arithmetic/logic ops, the `JMP` comparisons (`JEQ/JGT/JGE/JSET/JA`)
  and `RET` (`K`/`A`). Unknown/out-of-bounds instructions fail secure
  (`SECCOMP_RET_KILL_THREAD`).
- **`seccomp_data`** is built per-syscall: `nr`, `AUDIT_ARCH_RISCV64`,
  `instruction_pointer`, and the six syscall args.
- **Actions**: `ALLOW`/`LOG` → allow; `ERRNO` → return the errno without a
  signal; `KILL`/`TRAP`/`TRACE`/`USER_NOTIF` → deliver `SIGSYS` and return
  `ENOSYS` (no ptrace/user-notif, so the signal path is the closest behaviour).
- **`PosixThread`** gains a `seccomp_filter: Mutex<Option<Arc<[SockFilter]>>>`
  field + accessors. `handle_syscall` now dispatches on a `SeccompDecision`
  (`Allow` / `Kill` / `Errno`) instead of the old `should_block: bool`.
- **`sys_seccomp`** validates `flags == 0` (no `TSYNC`/`NEW_LISTENER`), reads
  `struct sock_fprog` (two field reads to dodge the padding), copies the
  program, and validates that every jump stays in-bounds before installing.

### Verification

A new QEMU smoke gate (`tools/riscv/pm1-gate.sh`,
`tools/riscv/nixos/pm1/{init.c,build_pm1.sh,boot_pm1.py}`) forks a child,
installs a filter, and checks both an `ERRNO(EPERM)` action and a `KILL` action
against `getpid`:

```
[PM1] seccomp_filter_errno: OK   (getpid → -1/EPERM)
[PM1] seccomp_filter_kill:   OK   (getpid → SIGSYS)
__PM1_DONE__ __PM1_PASS__
```

### Known limitations (unchanged from FOUNDATION-M4)

- Seccomp mode/filter is **not inherited across `fork`/`clone`** (new threads
  start disabled). Linux inherits it — this is the next seccomp gap.
- The `SIGSYS` `siginfo_t` still does not populate `si_call_addr` / `si_syscall`
  / `si_arch`.
- `SECCOMP_FILTER_FLAG_TSYNC` / `SECCOMP_FILTER_FLAG_NEW_LISTENER` are rejected.

## Reproduce

```bash
# cgroup memory.max — exercised by the systemd boot gate
tools/riscv/systemd/gate_m2.sh --rebuild-kernel

# seccomp BPF
tools/riscv/pm1-gate.sh --rebuild-kernel

# LTP (full)
tools/riscv/ltp-gate.sh --smp 1
```

## Follow-ups (priority order)

1. **`sigaltstack02` kernel hang** — unblocks the LTP tail (~87 tests).
2. **Loop device** (`/dev/loop*` + `LOOP_SET_FD`) — unblocks 17 LTP tests at once.
3. **`pwrite` O_APPEND / `EBADF`** — also the likely journald-write blocker from
   SYSTEMD-BOOT-M2 (journald uses `pwrite`).
4. **`brk`/`sbrk`** (`ENOMEM` on expand) and **`readlink` permission/size checks**.
5. **seccomp fork/clone inheritance** + `SIGSYS` `siginfo` detail fields.
