# KE-M4 Report — PID Namespaces (Stage 1)

Track B (kernel evolution), card KE-M4. Commits on `main`:

- `21d5e6612` feat(process): PID namespace type and CLONE_NEWPID creation
- `a8a435cb5` feat(process): namespace-aware PID translation at syscall and procfs boundaries
- `d4a2f54e7` test(riscv): guest PID namespace smoke tests and passing logs

Implements stage 1 of the PID-namespace item in
`docs/superpowers/specs/2026-08-22-nix-sandbox-namespace-matrix-design.md`.
(The session-inherited working tree already contained a compiling draft of
the two kernel commits; it was reviewed, verified in the guest, and
committed in the two slices above.)

## Scope implemented

- `PidNamespace` (`kernel/src/process/namespace/pid_ns.rs`): parent chain
  plus a per-namespace virtual PID allocator (`BTreeMap<vpid, Weak<Process>>`).
  The global PID table stays the source of truth for kernel-unique IDs; in
  the initial namespace virtual PIDs are the global PIDs.
- `Process` records its own PID namespace and its virtual PID in every
  namespace from its own out to the initial one (`ns_vpids`, allocated at
  `Process::new` via `register_process`, dropped at reaping).
- `NsProxy` carries the PID namespace **for children**, exactly like Linux:
  `clone(CLONE_NEWPID)` and `unshare(CLONE_NEWPID)` only move subsequently
  forked children into the new namespace; the first child is the namespace
  init (vpid 1). Creation requires `CAP_SYS_ADMIN` in the current user
  namespace via the capability LSM — after `unshare(CLONE_NEWUSER)` that is
  the new user namespace, so unprivileged `unshare(NEWUSER|NEWPID)` works.
- Boundary translation, all keyed on the *caller's/reader's* namespace:
  - `getpid`/`getppid` report virtual PIDs; a parent outside the caller's
    namespace reports as 0.
  - `kill` resolves a positive PID in the caller's namespace (outsiders are
    invisible, `ESRCH`).
  - `wait4` resolves a positive PID in the caller's namespace (`ECHILD` if
    invisible) and reports the reaped child's virtual PID.
  - procfs `/proc/<pid>` lookup, readdir filtering, and dentry revalidation
    operate on the reading process's namespace: a namespaced reader sees
    only its own namespace's members, under their virtual PIDs.
- Namespace-init semantics: when a namespace init exits, every remaining
  member of its namespace (including members of nested namespaces, which are
  registered in every ancestor's map) gets `SIGKILL`
  (`kernel/src/process/exit.rs`, mirrors `zap_pid_ns_processes`).
- `setns(CLONE_NEWPID)` is explicitly rejected with `EINVAL`; the
  representation (children-namespace in the proxy vs. own namespace in
  `Process`) already matches Linux's deferred-join semantics, so stage 2
  needs no redesign.
- Sanity check: `clone` with `CLONE_THREAD|CLONE_NEWPID` is rejected with
  `EINVAL`, as in Linux.

## Guest verification (`tools/riscv/pidns/`, riscv64, headless QEMU)

Two static guest binaries packed as a newc initramfs; both pass
(`PIDNS_TEST_PASS`, `PIDNS_CLONE_TEST_PASS`; logs committed next to the
sources):

- `pidns_test.c` (unshare path): `unshare(CLONE_NEWUSER)` +
  `unshare(CLONE_NEWPID)` succeed as an unprivileged-adjacent flow; the
  caller keeps its old PID (deferred semantics); the forked child observes
  `getpid()==1`, `getppid()==0`; a grandchild is vpid 2; `/proc` inside the
  namespace lists exactly 2 numeric entries; `waitpid(2)` and
  `kill(3, SIGTERM)` work by virtual PID; when the namespace init exits,
  the sleeper left behind is reaped by the outer init with `SIGKILL`.
- `pidns_clone_test.c` (clone path): `clone(CLONE_NEWPID|CLONE_NEWUSER|SIGCHLD)`
  makes the child the namespace init immediately; same vpid assertions.

Boot command (note: this kernel build requires the SV39/Svade profile;
with QEMU 11.0.3's default `rv64` CPU — SV48+SvAdu — the kernel resets in
early boot. This matches the registered `generic-sv39` profile):

```
qemu-system-riscv64 -cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true \
    -machine virt -m 2G -smp 1 --no-reboot -display none \
    -serial file:serial.log \
    -kernel target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
    -initrd <test>.cpio -append "console=ttyS0 loglevel=info init=/init"
```

The trailing "Uncaught panic: The init process terminates" in the serial
log is the kernel's expected reaction to PID 1 exiting
(`kernel/src/init.rs`), i.e. test completion.

## Regression evidence

- Xfce desktop chain (`tools/riscv/xfce/boot_xfce_desktop.py`, headless,
  own boot disk under `/tmp/xfce-m3/`): boots to `graphical.target`
  ("Reached target Graphical Interface"), xfwm4/xfce4-panel/xfdesktop all
  running, zero kernel panics, and the desktop screendump is byte-identical
  to the pre-KE-M4 known-good capture (`/tmp/kem2/desktop-final.png`).
- LTP process subset: 78 runtest entries covering clone/fork/vfork/execve/
  exit/wait/waitpid/waitid/kill/tgkill/tkill/getpid/getppid/getpgid/setsid
  process syscalls, cross-compiled from LTP 20260529 (musl dynamic), run at
  SMP=1 via `boot_ltp_gate.py` (verdict logs in `tools/riscv/pidns/`):
  - KE-M4 kernel: `total=78 pass=68 fail=4 conf=6 crash=0 timeout=0`.
  - Pre-KE-M3 baseline kernel (`kernel-known-good` @ `9a5034261`, prebuilt
    image): `total=78 pass=67 fail=5 conf=6` — no new failures from KE-M4.
  - The four failures on the KE-M4 kernel are all known, pre-existing gaps:
    `clone08` (musl does not implement CLONE_THREAD; documented in
    POLISH-M27), `tgkill02` (musl tgkill EAGAIN semantics; POLISH-M27),
    `waitpid01` (no core-dump support; POLISH-M27), and `clone304`
    (expects EPERM for CLONE_NEWNET — net namespaces are unsupported —
    and set_tid; the test only started running its assertions once
    `clone3` landed in `6e065bb56`, so the baseline kernel TCONF'd it).
  - Baseline-only failures `execve04` and `execveat03` pass/skip cleanly on
    the KE-M4 kernel (improvements, not regressions).

## Known gaps (deliberately out of scope for stage 1)

- `setns` into a PID namespace (deferred children-only join) — stage 2.
- Process groups and sessions are still global objects: `kill(-pgid)` and
  session enumeration are not namespace-filtered.
- `wait4` on ptraced *thread* sources reports the global TID (no
  translation yet).
- `/proc/[pid]/ns/pid` and `/proc/[pid]/ns/pid_for_children` nsfs entries
  are not registered; `NSpid:` in `/proc/[pid]/status` is not emitted.
- No per-namespace PID reuse policy beyond a monotonically increasing
  allocator (fine for sandbox lifetimes; not Linux-complete).
- `unshare(CLONE_NEWPID)` from a multithreaded process is not rejected with
  `EINVAL` as Linux does.
- The closed LTP suite packaging counts (`tools/riscv/ltp_suite.py`)
  currently pin 138 selected / 1 unavailable for `arch-riscv64`, but with
  today's toolchain all 139 requested binaries build, so the full gate
  refuses to package. Left untouched (out of task scope); the process
  subset above was packaged manually through the same `ltp_manifest.py`
  selector with zero unavailable entries.

## Housekeeping

- Only task-chain files were committed; pre-existing dirty files
  (`tools/riscv/xfce/boot_xfce_desktop.py`, `docs/当前架构.md`, M4
  leftovers under `tools/riscv/systemd/`, `Cargo.lock.bak`) remain
  untouched.
- All QEMU runs headless with private disks/serial logs under `/tmp/kem4`
  and `/tmp/xfce-m3`; no other session's QEMU was disturbed
  (`timeout`-wrapped, never `pkill`).
- The LTP source clone and build products live under `target/ltp/`
  (regenerable; `git clone --depth 1 --branch 20260529` + `build_ltp.sh`).
  The shared `target/qemu-uboot/current/boot.ext4` was re-packed with the
  KE-M4 kernel + process-subset initramfs for the gate run (this is the
  gate's normal mode of operation; a pre-run copy is at
  `/tmp/kem4/boot.ext4.bak`).

## Next steps

- Stage 2: `setns(CLONE_NEWPID)` deferred join, `/proc/[pid]/ns/pid*`
  entries, `NSpid:` status line.
- Namespace-aware process-group/session signal delivery (`kill(-pgid)`).
- Fix or re-pin the `arch-riscv64` LTP suite counts (139/0) so the full
  gate runs again.
