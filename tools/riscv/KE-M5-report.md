# KE-M5 Report — Namespace Matrix Closeout (setns stage 2, mount/ipc/uts audit)

Track B (kernel evolution), card KE-M5. Commits on `main`:

- `6ef240702` feat(process): setns support for PID namespaces with deferred semantics
- `b31959f66` feat(procfs): NSpid status line and namespace-translated Pid fields
- `42a796c7b` test(riscv): guest setns(CLONE_NEWPID) test and passing log
- `f8ed17dc8` test(riscv): guest mount/ipc/uts namespace audit tests (Nix sandbox paths)
- `c87c86535` fix(process): PID namespace polish — multithreaded unshare, group/broadcast signals, clone return value
- `15d560369` test(riscv): guest test for multithreaded unshare(NEWPID) and namespaced group/broadcast kill

Follows the namespace matrix design in
`docs/superpowers/specs/2026-08-22-nix-sandbox-namespace-matrix-design.md`.

## Task 1: setns for PID namespaces (stage 2)

- `PidNamespace` now records its owner user namespace and implements
  `NsCommonOps`, so it has nsfs paths (`pid:[ino]`) and supports
  `NS_GET_PARENT` / `NS_GET_USERNS` / `NS_GET_NSTYPE` ioctls.
- `setns(CLONE_NEWPID)` works from both a namespace file
  (`/proc/<pid>/ns/pid`) and a PID file (`pidfd_open`). Semantics are
  Linux's deferred join: only the caller's *children* namespace changes
  (`nsproxy.pid_ns_for_children`); the caller keeps its own PID and stays
  in its own namespace.
- Permission rules: CAP_SYS_ADMIN in the target namespace's owner user
  namespace and in the caller's current user namespace, and the target
  must be the same as or a descendant of the caller's current
  children-namespace (joining an ancestor is rejected with EINVAL).
- `/proc/<pid>/ns/` gains `pid` (the process's own namespace) and
  `pid_for_children` (the proxy's children namespace) entries. Dentry
  revalidation distinguishes the two by name — they diverge after
  `unshare(CLONE_NEWPID)` / `setns(CLONE_NEWPID)` (a bug caught by the
  guest test where a stale `pid_for_children` dentry was validated against
  the process's own namespace).
- `/proc/<pid>/status` reports `Tgid`/`Pid`/`PPid` translated into the
  reading process's namespace and gained the `NSpid` line (vpid chain from
  the outermost reader-visible ancestor namespace down to the process's
  own namespace).

### Guest verification (`tools/riscv/pidns/pidns_setns_test.c`)

`SETNS_TEST_PASS`: pid vs pid_for_children diverge after unshare; setns
via ns file keeps the caller's namespace and switches the children's; a
subsequently forked child observes `getpid()==2` with `getppid()==0`
(parent outside the namespace); setns via pidfd works; joining an ancestor
namespace is rejected (EINVAL when privileged, EPERM from a child user
namespace); `NSpid: <global> 1 1` shows the full chain for a level-2
namespace init.

## Task 2: mount/ipc/uts audit against the Nix sandbox contract

Method: replicate the exact mount sequence Nix's `linux-derivation-builder`
performs, in a guest test (`tools/riscv/nsmatrix/mount_ns_test.c`),
rather than reading code only.

**Result: no kernel changes needed.** The full sequence passes unmodified:

- `mount(NULL, "/", NULL, MS_REC|MS_PRIVATE, NULL)` (bookkeeping per mount,
  recursive);
- tmpfs workdirs; `MS_BIND` mounts with content visibility;
- `MS_BIND|MS_REMOUNT|MS_RDONLY` — writes fail with EROFS while the source
  stays writable (per-mount flags are enforced at the VFS layer);
- `MS_BIND|MS_REC` including submounts;
- `umount2` with `MNT_DETACH`;
- the `pivot_root` dance (pivot into a tmpfs, detach the old root).

IPC and UTS isolation were re-verified
(`tools/riscv/nsmatrix/ipc_uts_test.c`, `IPC_UTS_TEST_PASS`): SysV shm keys
are invisible across IPC namespaces, same-key segments coexist, and
`sethostname` stays inside the UTS namespace.

Remaining mount/ipc gaps (not exercised by Nix, documented):
cross-peer-group shared-mount propagation events (only propagation-type
bookkeeping exists) and the SysV permission-check TODOs in `ipc_ns.rs`.

## Task 3: PID namespace polish (KE-M4 gap list)

- `unshare(CLONE_NEWPID)` from a multithreaded process fails with EINVAL
  (Linux parity).
- `kill(-pgid)` signals only group members visible in the caller's PID
  namespace (ESRCH when none are); `kill(-1)` skips invisible processes
  and spares the namespace init.
- `clone`/`fork` return the child's *virtual* PID in the caller's
  namespace. Previously the global PID was returned, which made
  `waitpid()` on the returned value fail with ECHILD inside nested
  namespaces — caught by the new guest test.

Guest evidence: `tools/riscv/nsmatrix/kill_ns_test.c` (`KILL_NS_TEST_PASS`).

## Regression evidence

- KE-M4 guest tests re-run on the KE-M5 kernel: `PIDNS_TEST_PASS`,
  `PIDNS_CLONE_TEST_PASS`, `SETNS_TEST_PASS` all still pass.
- Xfce desktop chain (`tools/riscv/xfce/boot_xfce_desktop.py`, headless,
  `/tmp/xfce-m3/`): boots to `graphical.target`, xfwm4/xfce4-panel/
  xfdesktop up, no kernel panic.
- LTP process subset (78 runtest entries, LTP 20260529, musl, SMP=1 via
  `boot_ltp_gate.py`): `total=78 pass=68 fail=4 conf=6` — byte-identical
  verdict set to the KE-M4 run (the 4 fails are the known `clone08`,
  `clone304`, `tgkill02`, `waitpid01` gaps documented in the KE-M4
  report); verdict log in `tools/riscv/pidns/ltp-kem5-verdicts.log`.

## Namespace matrix status after KE-M5

| Namespace | clone/unshare | setns | Notes |
|-----------|---------------|-------|-------|
| user      | yes (KE-M3)   | EINVAL (deferred) | uid/gid maps, ancestor caps |
| pid       | yes (KE-M4)   | yes, deferred (this card) | procfs filtered, ns-init reap, NSpid |
| mount     | yes           | yes   | Nix sequence verified; no shared-propagation events |
| ipc       | yes           | yes   | SysV key isolation verified; perm-check TODOs remain |
| uts       | yes           | yes   | verified |
| cgroup    | yes           | yes   | untouched |
| net       | EINVAL        | EINVAL | biggest remaining Nix gap (default sandbox wants private net) |
| time      | EINVAL        | EINVAL | not needed by Nix |

## Known gaps / next steps

- net namespace (minimal loopback-only) is the next Nix blocker.
- tgkill/ptrace still use global TIDs; process-group *IDs* are not
  virtualized (kill(-pgid) filters membership but the pgid argument itself
  is global).
- `NS_GET_PID_FROM_PIDNS`-family ioctls are unimplemented.
- PID virtual-number reuse: the per-ns allocator is monotonically
  increasing (fine for sandbox lifetimes).
- The closed LTP `arch-riscv64` suite counts (138/1) still need re-pinning
  (139/0 with the current toolchain) before the full gate runs again.
