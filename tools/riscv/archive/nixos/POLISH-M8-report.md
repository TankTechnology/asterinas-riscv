# POLISH-M8 — systemd service-management gaps: WEXITED, CLONE_INTO_CGROUP, sync_file_range2/rseq

Date: 2026-08-16
Branch: `track/nixos`
Status: **All four systemd service-management syscall/feature gaps are closed.**
A systemd boot that previously logged
`unsupported wait options are found: WEXITED`,
`cgroup is not supported`,
`Unimplemented syscall number: 264`, and
`Unimplemented syscall number: 293` now logs **none** of them, and still
reaches `multi-user.target` → `Login Prompts` → the getty shell. The fixes are
committed as three focused kernel commits on `track/nixos` and flowed to
`main` as cherry-pick PRs.

---

## 1. The four gaps (reproduced from the serial log)

`target/nixos/systemd/systemd-m2-serial.log.smp1` (pre-fix) showed, on every
boot:

| Signature | Count | Source |
|---|---|---|
| `unsupported wait options are found: WEXITED` | 1+ | `process/wait.rs` `WaitOptions::check()` |
| `cgroup is not supported` | 1 | `syscall/clone.rs` `clone3` `cgroup` field |
| `Unimplemented syscall number: 264` | ~10 | `sync_file_range2` (RISC-V) not dispatched |
| `Unimplemented syscall number: 293` | ~12 | `rseq` not dispatched |

The other `Unimplemented syscall` lines in the same log (`170` set_mempolicy,
`258` riscv_hwprobe, `280` bpf, `285` copy_file_range) are out of scope here —
they are the known-harmless systemd/glibc startup probes already documented in
POLISH-M7.

---

## 2. Fix 1 — `WEXITED` wait option (`f2ce0fd10`)

**Root cause.** `WaitOptions::check()` enumerated the "supported" wait options
as `WNOHANG | WSTOPPED | WCONTINUED | WNOWAIT`, omitting `WEXITED`. systemd's
PID 1 passes `WEXITED` on every `waitid(2)`, so every wait tripped the
`warn!("unsupported wait options are found: …")` path — even though exited
children are *always* reported by `try_wait_children`, i.e. `WEXITED` was
already fully honoured.

**Fix.** Add `WaitOptions::WEXITED` to the recognized set. One line + comment.
The stopped/continued cases remain gated on `WSTOPPED`/`WCONTINUED`, so the
semantics are unchanged — only the spurious warning is gone.

---

## 3. Fix 2 — `sync_file_range2` (264) + `rseq` (293) (`c295a4715`)

### 3.1 `sync_file_range2` (RISC-V / asm-generic 264)

The RISC-V variant takes `flags` as the *second* argument
(`sync_file_range2(fd, flags, offset, nbytes)`), unlike x86's
`sync_file_range(fd, offset, nbytes, flags)`. Asterinas has no per-range dirty
tracking, so `sys_sync_file_range2` conservatively `fdatasync`s the whole file
(`path.sync_data()`), which is always at least as strong as the requested range
flush. Registered in `arch/generic.rs` as `SYS_SYNC_FILE_RANGE2 = 264`.

### 3.2 `rseq` (restartable sequences, 293)

A minimal but correct register/unregister:

- **register** (`flags == 0`): validate `rseq_len >= 32`, 32-byte alignment,
  and a non-null pointer; write the signature at offset 32 and a stable
  `cpu_id_start`/`cpu_id` of `0`; remember the area per-thread.
- **unregister** (`RSEQ_FLAG_UNREGISTER`): write `cpu_id = RSEQ_CPU_ID_UNINITIALIZED`
  (`u32::MAX`) into the area and forget it.
- **thread exit** (`process/posix_thread/exit.rs`): unregister the area, the
  analogue of Linux's `rseq_reset_rseq_cpu_node_id`.

The per-thread state lives in `ThreadLocal::rseq` (a `RefCell<Option<Rseq>>`,
`Rseq { ptr }`), mirroring the existing `robust_list`/`clear_child_tid`
pattern. Asterinas does not currently migrate a thread across CPUs, so
`cpu_id == 0` never needs updating after registration — this is the documented
limitation (see §6).

This is sufficient for glibc to stop falling back to non-rseq code paths; in
the pre-fix log `rseq` was retried once per new thread (~12 times) and now
succeeds silently.

---

## 4. Fix 3 — `clone3` `CLONE_INTO_CGROUP` (`6e065bb56`)

**Root cause.** systemd spawns each service process with
`clone3(CLONE_INTO_CGROUP)` so the child lands in the service's cgroup. The
`Clone3Args → CloneArgs` conversion stored the `cgroup` fd but warned
`"cgroup is not supported"` and never used it, so every child silently stayed
in the parent's cgroup — per-service resource accounting/isolation was broken.

**Fix.** Resolve the cgroup directory fd to a `CgroupNode` and fork the child
into it:

- `cgroupfs::cgroup_node_from_fd(fd, ctx)` — looks the fd up in the file
  table, downcasts the inode to `CgroupInode`, and extracts the
  `SysTreeNodeKind::Branch(..)` `CgroupNode` (added in `cgroupfs/mod.rs`).
- `process/clone.rs` `clone_child` — when `clone_args.cgroup` is `Some(fd)`,
  the target cgroup is the resolved node instead of the parent's; the same
  pids pre-charge + `move_forked_process_to_node` path then places the child
  in that node. A bad fd now returns `EBADF`, a non-cgroup fd `EINVAL`
  (matching Linux), instead of silently ignoring the request.
- `syscall/clone.rs` — `CloneArgs::try_from` no longer warns; it stores
  `None` when `cgroup == 0` and `Some(fd)` otherwise.

---

## 5. Verification — systemd boot smoke test

Rebuilt the kernel (`cargo osdk build --scheme riscv --features riscv_sv39_mode`)
and booted the systemd rootfs (`boot_systemd_smoke.py`, `--smp 1`). The serial
log (`target/nixos/systemd/systemd-m8-serial.log.smp1`) reached:

```
Reached target Basic System
Reached target System Initialization
Reached target Multi-User System
Reached target Login Prompts
[  OK  ] Started Getty on ttyS0.
asterinas-riscv login:
```

And the four signatures are **all absent** (count `0` each):

```
unsupported wait options            → 0
cgroup is not supported             → 0
Unimplemented syscall number: 264   → 0
Unimplemented syscall number: 293   → 0
```

No panic / Oops / regression. Remaining `Unimplemented syscall` noise is the
out-of-scope set only (`170`, `258`, `280`, `285`).

**One newly-surfaced gap (not a regression):** with `CLONE_INTO_CGROUP` now
working, systemd proceeds further with cgroup bookkeeping and logs
`Failed to get cgroup ID of cgroup /sys/fs/cgroup/…, ignoring: Bad file
descriptor`. That is a *different* syscall (`name_to_handle_at` / `statx`
file-handle lookup on a cgroup directory) returning `EBADF`, unrelated to
`clone3`. It is cosmetic (systemd ignores it) and is filed as a follow-up in §6.

---

## 6. Next steps

1. **Per-CPU rseq.** The current rseq keeps `cpu_id == 0` forever. If Asterinas
   ever migrates a thread across CPUs, the scheduler must update
   `cpu_id_start`/`cpu_id` on migration (Linux's `rseq_update_cpu_node_id`),
   otherwise userspace critical sections could corrupt. Out of scope while the
   systemd gate runs at `--smp 1`.
2. **cgroup file handles.** `name_to_handle_at`/`statx` on a cgroup directory
   return `EBADF` (the `Failed to get cgroup ID` message). Low priority —
   systemd ignores it.
3. **Remaining syscalls** (all already known-harmless, documented in POLISH-M7):
   `riscv_hwprobe` (258), `set_mempolicy` (170), `bpf` (280),
   `copy_file_range` (285).

---

## 7. Commits / PRs

`track/nixos` commits:

- `f2ce0fd10` `fix(wait): recognize WEXITED wait option`
- `c295a4715` `feat(syscall): implement sync_file_range2 (264) and rseq (293)`
- `6e065bb56` `feat(cgroup): clone3 CLONE_INTO_CGROUP`

All three cherry-pick cleanly onto `origin/main` (verified on a test branch)
and are flowed as focused PRs.
