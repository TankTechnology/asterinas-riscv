# POLISH-M2 — sigaltstack02 hang resolved + seccomp fork inheritance + pwrite fix

> 2026-08-15. Continuation of POLISH-M1. Three follow-ups closed: (1) the
> `sigaltstack02` kernel hang is gone on the current kernel — a full re-run now
> completes the previously-blocked tail; (2) seccomp mode/filter is now inherited
> across `fork`/`vfork`/`clone` (non-`CLONE_THREAD`), matching Linux; (3) `pwrite`
> now works through musl (was `EOPNOTSUPP` everywhere) by recognising
> `RWF_APPEND`/`RWF_NOAPPEND` in `pwritev2`.

## Conclusion first

| Item | Status |
|---|---|
| `sigaltstack02` kernel hang (POLISH-M1 #1) | **Resolved** — passes on HEAD; full gate now runs the tail |
| seccomp filter inheritance (POLISH-M1 #5) | **Done** — committed `1e7437a0f` + `1f284a1c1`, QEMU-verified |
| `pwrite` through musl (POLISH-M1 #3) | **Done** — committed `9756a2e9f`, QEMU-verified (`pwrite(-1)`→`EBADF`, O_APPEND appends) |
| LTP gate | 462 pass / 42 fail / 29 conf / 1 timeout (4 `pwrite*` tests fixed → ~466/38) |

---

## 1. LTP re-run — the hang is gone

`tools/riscv/ltp-gate.sh --smp 1` on HEAD produces
`total=533 pass=462 fail=42 conf=29 crash=0 timeout=1`.

The POLISH-M1 report triaged a **stale** serial log and flagged `sigaltstack02`
as a kernel deadlock blocking the ~87-test tail. Re-running on the current kernel
shows `sigaltstack02` **passes** and the tail (`signal*`, `signalfd*`, `socket*`,
`stat*`, `wait*`, `write*`, …) completes. The `sigaltstack02` hang is therefore
**not reproducible** on HEAD — it was either fixed by a commit between the old log
and HEAD, or an artifact of a stale kernel image. No kernel change was required.

### Current failure taxonomy (43 = 42 fail + 1 timeout)

**Environment gaps — 22 (harness, not kernel):**

| Count | Tests | Root cause |
|---|---|---|
| 17 | `rename01/03–08/10/12/13/15`, `fsopen01/02`, `fsconfig01/02`, `fsmount01/02` | `tst_device: Failed to acquire device` — no `/dev/loop*` |
| 5 | `posix_fadvise03(_64)`, `setrlimit04`, `gethostbyname_r01`, `getitimer01` | missing `/bin/cat`/`/bin/true`, no DNS, `clock_getres(MONOTONIC_COARSE)` ENOSYS |

**Kernel bugs — 21 (20 fail + 1 timeout):**

| Count | Tests | Note |
|---|---|---|
| ~~4~~ | ~~`pwrite02(_64)`, `pwrite04(_64)`~~ | **fixed** by `9756a2e9f` (see §3) |
| 2 | `fcntl14(_64)` | POSIX record-lock behaviour |
| 2 | `sendfile07(_64)` | sendfile to pipe blocks → LTP 30 s watchdog |
| 1 | `sbrk01` | `sbrk(±8192)` → `ENOMEM` (heap `resize_mapping` fails) |
| 1 | `readlink03` | succeeds where `EACCES` expected |
| 1 | `readlinkat02` | **musl behaviour, not a kernel bug** — musl's `readlinkat` short-circuits `bufsize==0` (returns 0 with a 1-byte dummy buffer) instead of issuing syscall 78, so the kernel's `EINVAL` check is never reached |
| 1 | `gethostname02` | `gethostname` short-buffer succeeds where `ENAMETOOLONG` expected |
| 1 | `epoll_wait04` | zero-timeout `epoll_wait` slept ~2.5 ms |
| 1 | `fork06` | 1000 forks exceed 30 s test timeout (slow fork) |
| 1 | `access02`, `chdir02`, `pipe13`, `sched_setscheduler04` | singletons |
| 1 | `symlink03` | **new**: `mkdtemp(/tmp/…)` → `EACCES` (tmpfs `/tmp` mode/ownership) |
| 1 | `epoll01` | timeout |

---

## 2. seccomp filter inheritance across fork/vfork/clone

POLISH-M1 left seccomp mode/filter as **not inherited** across fork. This is now
implemented the way Linux does it:

- `PosixThreadBuilder` gains `seccomp_mode` + `seccomp_filter` fields and setters;
  `build()` initializes the child thread's state from them (default disabled).
- `clone_child_process` (the fork/vfork/clone-without-`CLONE_THREAD` path) copies
  the parent thread's `seccomp_mode()` + `seccomp_filter()` into the builder.
- `clone_child_task` (the `CLONE_THREAD` path) is unchanged — a new thread starts
  with seccomp disabled, matching Linux (no `TSYNC` support yet).

### Verification

`tools/riscv/pm1-gate.sh` now runs a third case `test_filter_inherit`: a child
installs an `ERRNO(EPERM)` filter on `getpid`, forks a grandchild, and asserts the
grandchild's `getpid` returns `-1/EPERM`:

```
[PM1] seccomp_filter_errno: OK
[PM1] seccomp_filter_kill:   OK
[PM1] seccomp_filter_inherit: OK
__PM1_DONE__ __PM1_PASS__
```

Commits: `1e7437a0f feat(syscall): inherit seccomp mode and BPF filter across fork/vfork`,
`1f284a1c1 test(riscv): pm1 gate seccomp filter inheritance smoke case`.

### Remaining seccomp gaps

- `SECCOMP_FILTER_FLAG_TSYNC` / `NEW_LISTENER` still rejected.
- `SIGSYS` `siginfo_t` still lacks `si_call_addr` / `si_syscall` / `si_arch`.

---

## 3. `pwrite` through musl — `RWF_APPEND`/`RWF_NOAPPEND` recognition

musl's `pwrite()` is **not** the raw `pwrite64` syscall: it is implemented via
`pwritev2` (287) with `RWF_NOAPPEND` (0x20), using `RWF_NOAPPEND` to get
POSIX "write at offset" semantics and falling back to `pwrite64` only on
`EOPNOTSUPP`/`ENOSYS`. Asterinas's `RWFFlag` bitflags omitted `RWF_APPEND`
(0x10) and `RWF_NOAPPEND` (0x20), so `RWFFlag::from_bits(32)` returned `None`
and `sys_pwritev2` reported `EOPNOTSUPP` for every musl `pwrite` — including
`pwrite(-1, …)` (which must be `EBADF`) and `pwrite` on an `O_APPEND` file.

Adding the two flags (commit `9756a2e9f`) makes `pwritev2` accept them and fall
through to `do_sys_pwritev`, whose `write_at` already honours `O_APPEND`.
QEMU-verified with a **musl**-linked probe:

```
pwrite(-1)        -> errno=9  (EBADF; was EOPNOTSUPP)
pwrite(O_APPEND)  -> size=3072 (appends; pwrite04 semantics)
```

This fixes LTP `pwrite02`/`pwrite04` + `_64` (4 tests) and is the likely fix for
the systemd journald `pwrite` blocker noted in SYSTEMD-BOOT-M2.

> Probe note: a **glibc** `syscall(68, …)` probe returns `EBADF` for `pwrite(-1)`
> directly, which masked this bug in early triage — musl and glibc issue
> different syscalls for `pwrite`.

---

## Next steps (re-prioritized)

1. **Loop device** (`/dev/loop*` + `LOOP_SET_FD`) — unblocks 17 tests at once.
2. **`readlink` permission/size checks** (`readlink03`, `readlinkat02`).
3. **`sbrk` heap expand** (`ENOMEM`), **`gethostname` short-buffer**.
4. **`symlink03` tmpfs `/tmp` `EACCES`** (new — investigate `/tmp` mode).
5. **seccomp `TSYNC`** + `SIGSYS` detail fields.
