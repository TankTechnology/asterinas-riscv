# FOUNDATION-M4 report — security trio (fanotify / keyrings / seccomp) on RISC-V

> 2026-08-14. Corresponds to plan
> `docs/superpowers/plans/2026-08-13-nixos-riscv-track.md` (Route B kernel
> wrap-up, the "nix-daemon + store operations" milestone M4). Conclusion first:
> the M4 smoke gate **passes 3/3** — `fanotify_init`/`fanotify_mark`,
> `add_key`/`request_key`/`keyctl`, and `seccomp(SECCOMP_SET_MODE_STRICT)` all
> return correct results on a freshly built kernel, and the three syscalls that
> were `ENOSYS` at the start of the session are now wired.

## Deliverables

| File | Purpose |
|---|---|
| `kernel/src/fs/vfs/notify/fanotify.rs` | `FanotifyFile` — fanotify mark + `fanotify_event_metadata` queue |
| `kernel/src/syscall/fanotify.rs` | `fanotify_init(2)` + `fanotify_mark(2)` |
| `kernel/src/syscall/keyctl.rs` | `add_key`/`request_key`/`keyctl` — keyring serial allocator |
| `kernel/src/syscall/seccomp.rs` | `seccomp(2)` strict mode + SIGSYS delivery |
| `kernel/src/process/posix_thread/mod.rs`, `builder.rs` | per-thread `seccomp_mode` field |
| `tools/riscv/fm4-gate.sh` | One-command gate: build → pack → QEMU boot → report |
| `tools/riscv/nixos/fm4/{init.c,build_fm4.sh,boot_fm4.py}` | In-guest smoke test + driver |

## How to run

```bash
tools/riscv/fm4-gate.sh                  # build initramfs, repack, boot, report
tools/riscv/fm4-gate.sh --rebuild-kernel # rebuild kernel first
```

The gate forces the same three traps as fm3 (`OSDK_TARGET_ARCH=riscv64`,
`--features riscv_sv39_mode`, prebuilt `vdso_riscv64.so`, `rust-objcopy` on
`PATH`).

## Result (SMP=1)

```
[FM4] fanotify: OK  __FM4_fanotify_OK__
[FM4] keyctl: OK  __FM4_keyctl_OK__
[FM4] seccomp: OK  __FM4_seccomp_OK__
__FM4_DONE__ __FM4_PASS__
```

## What landed (commit-by-commit)

1. **`test(riscv): FOUNDATION-M4 smoke gate harness`** — a single static `/init`
   exercises each syscall with the riscv64 asm-generic numbers and prints a
   `__FM4_<name>_{OK,FAIL}__` marker, so a crash/ENOSYS is attributed to the
   exact syscall.

2. **`feat(fs): fanotify_init/fanotify_mark — notification class, no fd delivery`**
   — `fanotify_init(2)` accepts `FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK |
   FAN_UNLIMITED_QUEUE | FAN_UNLIMITED_MARKS` and returns a `FanotifyFile`.
   `fanotify_mark(2)` supports `FAN_MARK_ADD`/`FAN_MARK_REMOVE`/`FAN_MARK_FLUSH`
   on an inode path (`dirfd`-relative, `FAN_MARK_DONT_FOLLOW` honoured). Marks
   register a `FanotifySubscriber` on the inode's existing `FsEventPublisher`;
   events come back via `read(2)` as `struct fanotify_event_metadata` (24-byte
   header, `vers=3`, `fd=FAN_NOFD`, `pid` = triggering process).

3. **`feat(syscall): add_key/request_key/keyctl — minimal keyring serial allocator`**
   — a monotonic serial allocator plus a lazily-allocated session-keyring serial.
   `add_key` validates the type/description strings and returns a fresh serial;
   `keyctl(KEYCTL_GET_KEYRING_ID)` and `keyctl(KEYCTL_JOIN_SESSION_KEYRING)`
   return the session keyring; `keyctl(KEYCTL_REVOKE)` returns `0`;
   `request_key` returns `ENOKEY`. This unblocks `libkeyutils`-style lookups
   that only need a non-zero keyring serial.

4. **`feat(syscall): seccomp SECCOMP_SET_MODE_STRICT with SIGSYS delivery`** —
   a per-thread `seccomp_mode` field, `seccomp(2)` with
   `SECCOMP_SET_MODE_STRICT`, and a check in `handle_syscall` that blocks any
   syscall outside the strict allowlist (`read`, `write`, `exit`,
   `rt_sigreturn` — `exit_group` deliberately excluded, per the man page). The
   blocked syscall is not executed; the thread is enqueued a `SIGSYS` with
   `si_code = SYS_SECCOMP` and the syscall returns `ENOSYS` (the value visible
   only if `SIGSYS` is ignored/blocked).

## Why this order (fanotify → keyctl → seccomp)

Route B's security trio is intentionally smallest-first. `fanotify` reuses the
already-present `FsEventPublisher`/`FsEventSubscriber` machinery (inotify is its
sibling), so it was the natural warm-up. `keyctl` is a self-contained stub with
no kernel-wide state. `seccomp` is the largest — it touches the syscall dispatch
hot path, the thread model and signal delivery — so it landed last and was
verified with a `fork`+`wait4` round-trip that proves the child dies with
`SIGSYS`.

## Known limitations (honest scope)

- **fanotify — notification class only.** `FAN_CLASS_CONTENT` /
  `FAN_CLASS_PRE_CONTENT` (permission events), all `FAN_REPORT_*` event-info
  flags and `FAN_ENABLE_AUDIT` are rejected with `EINVAL`. Events carry no file
  descriptor (`fd = FAN_NOFD`), so the `FAN_EVENT_INFO_*` record types are not
  emitted. `FAN_MARK_MOUNT` / `FAN_MARK_FILESYSTEM` and ignore marks
  (`FAN_MARK_IGNORED_*`) are rejected. On queue overflow events are dropped
  rather than replaced with a `FAN_Q_OVERFLOW` record.
- **keyrings — keys are not retained.** No key storage exists; `add_key` hands
  out a serial but discards the payload, so `keyctl(KEYCTL_READ/UPDATE/SEARCH/…)`
  return `EOPNOTSUPP` and `request_key` always returns `ENOKEY`. Only
  `GET_KEYRING_ID`, `JOIN_SESSION_KEYRING` and `REVOKE` are implemented.
- **seccomp — strict mode only.** `SECCOMP_SET_MODE_FILTER` (BPF) returns
  `EINVAL`. The `_sigsys` detail fields of the `SIGSYS` `siginfo_t`
  (`si_call_addr` / `si_syscall` / `si_arch`) are not populated — `si_code` is
  correctly `SYS_SECCOMP`, but a handler inspecting the details sees zeroes.
  Seccomp mode is **not inherited across `fork`/`clone`** (new threads start
  disabled), whereas Linux inherits it.

## syscall-number correction

The FOUNDATION-M3 report listed seccomp as "syscall 317". That is the x86_64
number; on riscv64 (asm-generic) the numbers are `seccomp = 277`,
`add_key/request_key/keyctl = 217/218/219`, `fanotify_init/fanotify_mark =
262/263`. This report uses the riscv64 numbers throughout, matching the existing
`generic.rs` table (`SYS_RENAMEAT2 = 276`, `SYS_GETRANDOM = 278` bracket
`seccomp`; `SYS_WAIT4 = 260`, `SYS_PRLIMIT64 = 261`, `SYS_SYNCFS = 267` bracket
`fanotify_*`; `SYS_MREMAP = 216`, `SYS_CLONE = 220` bracket the key syscalls).

## Build traps (documented so they are not re-hit)

- **`get_file_fast!` lives in `file::file_table`, not `file::`.** The macro is
  exported from `fs::file::file_table`; importing it from `fs::file` yields
  `E0432`, and because the downcast then types as `!`, every later method call
  on the file (`add_mark`, `flush`, …) fails with `E0599` (cascade).
- **`borrow_fs()` must be bound before `resolver().read()`.** Splitting
  `ctx.thread_local.borrow_fs().resolver().read()` across a `let` drops the
  `borrow_fs` guard early and borrows a freed temporary — `E0716`. Bind the fs
  guard first, then take the resolver lock (as `access.rs` does).
- **`VmWriter::write_val` returns `ostd::Error`**, not the crate `Error`; a
  manual `try_read` loop must `.into()` it (`From<ostd::Error> for Error` exists)
  or mirror inotify's `copy_to_user` helper.
- The three fm3 traps still apply: `rust-objcopy` off-`PATH`,
  `OSDK_TARGET_ARCH=riscv64`, and dev-profile (no `--release`).

## SMP note

The gate passes at `SMP=1`. `--smp 4` on this U-Boot `booti` pipeline does not
reach userspace (boot splash prints, then no `/init` output, no panic) — this
reproduces identically on the **FOUNDATION-M3** harness, so it is a pre-existing
boot-pipeline limitation, not a regression from the M4 syscalls. The M4 code is
SMP-safe by construction (per-thread atomic seccomp mode, per-inode fanotify
locks, atomic key serials), but the multi-CPU boot path remains out of scope for
this gate.

## Reproduce

```bash
# 1. boot the harness against the pre-M4 kernel to see all three ENOSYS:
#    git checkout c5ab0a70d -- kernel/ && tools/riscv/fm4-gate.sh
# 2. build the M4 kernel and re-run:
tools/riscv/fm4-gate.sh --rebuild-kernel
# serial transcript: target/nixos/fm4/fm4-serial.log.smp1
```
