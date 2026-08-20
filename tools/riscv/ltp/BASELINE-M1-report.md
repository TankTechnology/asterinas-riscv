# RISC-V LTP BASELINE-M1

## Scope and identity

This milestone records the first reproducible RISC-V LTP syscall baseline on
the generic Sv39 QEMU platform. It is a characterization baseline, not a claim
that every selected syscall conforms.

- Asterinas source: `37c5661cea2a2f193d1b1ccfe2bced8d62f91864`
- Source branch: `codex/riscv-ltp-integration`
- LTP source: tag `20260529`, commit
  `3a64d78f58bdceba93ed321e91215fb969a047ed`
- Reviewed manifest: 779 unique enabled names
- Runtime manifest: 767 tests
- Explicitly unavailable: 12 tests
- Kernel SHA-256:
  `661ca5e7de2275c5c48d560bc4932df56a5f5faffc4e5a62f745a18901baf3b0`
- LTP initramfs SHA-256:
  `36c975e2deb7982b240a187aadbc650f704f17d5e39468ab99dba186b7c78249`
- Runtime: QEMU 10.2.1, OpenSBI 1.7, U-Boot 2026.07

The gate preserved one verdict per runtime-manifest entry, one DONE marker,
one PID-1 terminal marker, no Asterinas panic, and matching live/final serial
logs. `SHA256SUMS` passed for every retained full-run artifact. A follow-up
SMP=4 smoke on gate revision `eed8bd94f1f2a089f7e159dbedf8b0551d58da10`
also passed all checksums after prepared boot artifacts were isolated by run
ID.

## Results

| Run | Infrastructure | Total | PASS | FAIL | CONF | CRASH | TIMEOUT |
|---|---:|---:|---:|---:|---:|---:|---:|
| SMP=1 full (`baseline-m1-smp1-r2`) | PASS | 767 | 537 | 140 | 81 | 5 | 4 |
| SMP=4 smoke (`baseline-m1-smp4-smoke-r3`) | PASS | 5 | 5 | 0 | 0 | 0 | 0 |
| SMP=4 full (`baseline-m1-smp4-r2`) | PASS | 767 | 536 | 141 | 81 | 5 | 4 |

The legacy aggregate failure count for SMP=1 is 149: 140 ordinary failures,
5 crashes, and 4 timeouts. For SMP=4 it is 150: 141 ordinary failures,
5 crashes, and 4 timeouts. The normalized counters above are mutually
exclusive.

The two full runs differ on exactly one test: `getcpu01` passes with SMP=1
and fails with SMP=4. The failing run pins the process to CPU 3 but `getcpu()`
reports CPU 2. This matches the existing `sched_setaffinity` limitation: the
kernel updates the affinity mask but does not migrate a thread that is already
running on a newly-disallowed CPU. Treat this as a focused SMP scheduler fix,
not as guest-fixture noise.

### Per-run artifact provenance

- `baseline-m1-smp1-r2` at
  `37c5661cea2a2f193d1b1ccfe2bced8d62f91864`:
  kernel `661ca5e7de2275c5c48d560bc4932df56a5f5faffc4e5a62f745a18901baf3b0`,
  initramfs `36c975e2deb7982b240a187aadbc650f704f17d5e39468ab99dba186b7c78249`,
  DTB `cec9cb5896bcac2bd76d301ca7f74c9c44f678ace02574532b870211fccabaa6`,
  boot disk `efcf5d05418cf76cefd971d50b816413e3595e1ad5eb924d6c328b2baabfbc79`.
- `baseline-m1-smp4-r2` at
  `37c5661cea2a2f193d1b1ccfe2bced8d62f91864`:
  kernel `661ca5e7de2275c5c48d560bc4932df56a5f5faffc4e5a62f745a18901baf3b0`,
  initramfs `36c975e2deb7982b240a187aadbc650f704f17d5e39468ab99dba186b7c78249`,
  DTB `f934c85dd8a0fc3ebd51432f01ca359cdd752fd9600a0c4557989d142fd3b17f`,
  boot disk `dc67dc14b77aa1da3dafb43a5f5c4d002d5175f01dbb43fd96dd377ef39f0e59`.
- `baseline-m1-smp4-smoke-r3` at
  `eed8bd94f1f2a089f7e159dbedf8b0551d58da10`:
  kernel `661ca5e7de2275c5c48d560bc4932df56a5f5faffc4e5a62f745a18901baf3b0`,
  initramfs `454844dcac650ebcffa3d8c1cc394d033571f7656fa1a264da31b2f11daca7f1`,
  DTB `a6bf3d66552c943ea9e80a7d3e57bc0afa0a239436fdd1f56a7d816656fea7d7`,
  boot disk `f42b323f8c6c32c80e8c92236bbf7dd404a7f5a2727c09264cd3f2efc5d898d9`.

## High-signal failure groups

The five crashes are `connect01`, `recv01`, `recvfrom01`, `send01`, and
`sendto01`. Each first reports `bind(INADDR_ANY)=EADDRNOTAVAIL`; the old LTP
cleanup then calls `kill(0, SIGKILL)`. Asterinas already initializes loopback
as `127.0.0.1/8` with UP/RUNNING flags. The shared kernel bind resolver accepts
only an address equal to one concrete interface address, so it rejects the
wildcard `0.0.0.0`. This is an `INADDR_ANY` kernel-semantics group, not missing
guest loopback initialization.

The four SMP=1 timeouts are:

- `epoll01`: combinatorial legacy test with about 27,648 `epoll_ctl` cases;
- `fcntl14` and `fcntl14_64`: two manifest names for a 5,000-operation,
  two-variant file-lock/fork test;
- `setfsgid03`: drops to nobody and then calls `getgrgid(1)`. The image does
  contain GID 1, but `/etc/group` was packaged as mode `0600`; lookup returns
  EACCES and the legacy loop scans upward until the watchdog fires. The same
  account-file mode causes `setgid02` to report `getpwnam(root)=EACCES`.

The 140 ordinary failures cluster into these next-step areas:

1. Guest fixtures: no loopback setup, no block/loop device, and no BusyBox
   applets for shell-out tests.
2. VFS and metadata: open/access, rename, mount/new mount API, xattr, statfs,
   and statx cases.
3. Memory management: brk, mmap/mprotect/mincore, mlock, madvise, fallocate,
   and fadvise cases.
4. Process, scheduler, and signal behavior: clone, capability, pidfd, prctl,
   scheduler, affinity, tgkill, and waitpid cases.
5. Networking and IPC: socket options/message APIs plus SysV/POSIX IPC cases.

The 81 CONF outcomes are retained separately and must not be counted as FAIL.
They include unsupported compatibility variants, configuration-dependent
features, and tests whose external helpers are absent from the minimal image.

## Build omissions

Four LTP syscall directories did not build: `rt_sigtimedwait`, `utils`,
`fmtmsg`, and `timer_create`. The selector recorded 12 unavailable enabled
names rather than silently dropping them: `fmtmsg01`, `munmap02`,
`pipeio_1` through `pipeio_8`, `timer_create01`, and `timer_create03`.

## Recommended next batches

1. Fix deterministic guest fixtures first: install passwd/group as `0644` and
   rerun `setfsgid03` plus `setgid02`.
2. Add the LTP-required BusyBox helpers and classify block-device-dependent
   failures before introducing the loop subsystem.
3. Port small, reviewed kernel fixes by failure group, beginning with wildcard
   IPv4 bind and the SMP affinity migration gap; run focused subsets before
   each 767-test regression.
4. Keep the roughly 600-line loop-device subsystem as a separate change after
   the non-loop VFS baseline is stable.
