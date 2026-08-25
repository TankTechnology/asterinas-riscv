# KE-M3 Report — riscv_flush_icache, User Namespaces (Stage 1), CLONE_SYSVSEM

Track B (kernel evolution), card KE-M3. Commits on `main`:

- `724cfaafe` feat(syscall): implement riscv_flush_icache (259)
- `21c427583` feat(process): user namespace creation with ID maps
- `2c65b6e90` feat(procfs): writable uid_map/gid_map and setgroups files
- `b5485ebe4` fix(process): accept CLONE_SYSVSEM silently in clone/unshare

## Task 1: riscv_flush_icache (259)

RISC-V JITs (SpiderMonkey/V8) must flush the icache after writing code
pages; unlike x86 there is no user-space instruction covering remote harts,
so the syscall is mandatory.

- `ostd::arch::flush_icache(local_only)` (`ostd/src/arch/riscv/mod.rs`):
  local `fence.i`, plus an SBI RFENCE `remote_fence_i` broadcast for the
  all-harts case (a safe superset of Linux's per-mm hart mask).
- `sys_riscv_flush_icache` (`kernel/src/syscall/riscv_flush_icache.rs`):
  validates flags (only `SYS_RISCV_FLUSH_ICACHE_LOCAL` allowed, EINVAL
  otherwise); like Linux, the address range is not validated. Registered in
  the riscv64 table (259).

Verified in a 2-hart QEMU guest (`/tmp/kem3/icache_smoke.c`): a program
writes `li a0,42; ret` into an RWX page, flushes with flags 0 and LOCAL,
executes it (returns 42), and confirms reserved flags are rejected with
EINVAL.

## Task 2: user namespaces, stage 1

Implements stage 1 of the design in
`docs/superpowers/specs/2026-08-22-nix-sandbox-namespace-matrix-design.md`.

### Scope implemented

- `UserNamespace` is no longer a singleton stub: it has a parent chain, an
  owner UID (creator's euid), write-once UID/GID ID maps, and a
  setgroups-denied flag. The init namespace keeps the identity map.
- `clone(CLONE_NEWUSER)` / `unshare(CLONE_NEWUSER)` create a child
  namespace and grant the process the full capability set *within* it
  (Linux semantics). `unshare` creates the user namespace first so sibling
  namespaces created in the same call are owned by it — this is what makes
  `unshare(CLONE_NEWUSER|CLONE_NEWUTS)` work for unprivileged users.
- The capability LSM performs a namespace-aware walk (target ns and its
  ancestors: capability held by a member, or the owner rule from the
  parent). Capabilities granted in a child namespace never apply to
  resources owned by ancestors.
- `getuid`/`geteuid`/`getgid`/`getegid` report IDs in the caller's user
  namespace; unmapped IDs appear as 65534 (overflow), as in Linux
  `from_kuid`.
- `/proc/[pid]/{uid_map,gid_map}`: real map on read; write-once writes with
  Linux-style permission rules (CAP_SETUID/CAP_SETGID in the parent ns for
  arbitrary maps; unprivileged self-write of a single extent mapping the
  writer's own effective ID; gid_map additionally requires setgroups=deny).
- New `/proc/[pid]/setgroups`: reads `allow`/`deny`; `deny` writable only
  before the GID map is set; `allow` requires CAP_SETGID in the parent ns.

### Guest verification (`/tmp/kem3/userns_test.c`, riscv64, headless)

As an unprivileged uid-1000 process: unshare(CLONE_NEWUTS) fails with
EPERM (baseline) → unshare(CLONE_NEWUSER) succeeds → `setgroups=deny` +
`uid_map "0 1000 1"` + `gid_map "0 1000 1"` accepted → `getuid()==0` in
the new ns → unshare(CLONE_NEWUTS) + sethostname succeed → second uid_map
write fails EPERM → clone(CLONE_NEWUSER|SIGCHLD) grandchild sees overflow
uid 65534 before its map is written. All assertions pass.

Regression: the Xfce desktop chain still boots to the graphical target
with the userns kernel (no panic, xfwm4/xfce4-panel/xfdesktop up).

### Known gaps (deliberately out of scope)

- File ownership (`chown`, inode owner display in `stat`, procfs status
  Uid lines) is not namespace-translated; credentials store global kuids
  and access checks still compare global IDs, which is safe but means
  in-sandbox `chown` semantics are not Linux-complete.
- `setns` into a user namespace is still rejected (EINVAL).
- Multi-level nested map display: `lower_first` is printed as a global ID;
  correct for first-level namespaces (the sandbox case).
- `NS_GET_USERNS`/owner plumbing works; `NS_GET_PARENT` on user namespaces
  returns EPERM as in Linux.

## Task 3: CLONE_SYSVSEM noise

The warns came from `clone_sysvsem` (clone path) and `unshare_sysvsem`.
Since `SEM_UNDO` itself is unsupported (semop rejects it with EINVAL),
there is no semaphore undo list whose sharing semantics could be observed;
both flags are now accepted as no-ops with a debug-level note. This removes
~40 warn lines per Xfce boot.

## Housekeeping

- Only task-chain files were committed; pre-existing dirty files
  (`boot_xfce_desktop.py`, M4 leftovers, `stash@{0}`) remain untouched.
- All QEMU runs headless under `/tmp/kem2` and `/tmp/kem3`; no other
  session's QEMU was disturbed.

## Next steps

- N2 stage 2: PID namespaces (see the design doc's risk analysis: global
  pid table coupling in kill/wait4/procfs, namespace-init semantics).
- Sandbox hardening of mount propagation / remount-ro under
  userns+mountns, driven by real nix build traces.
- Persistent guest test for riscv_flush_icache in the regression suite
  (currently verified ad hoc in /tmp/kem3).
