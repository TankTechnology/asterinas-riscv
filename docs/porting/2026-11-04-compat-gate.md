# Compatibility gate: developer workflow and LTP on RISC-V

Date: 2026-11-04

## What the gate is

Three independent layers, each a short command with its own evidence:

1. **Developer workflow** (`tools/riscv/compat/`): ten checks — ssh, a forced
   pty, scp, rsync, python3, gcc, git, tar+xz, pip, firefox. Each prints one
   `CHECK <name> PASS|FAIL|SKIP <detail>` line. The same checks run against the
   board over ssh and against a QEMU boot of the image.
2. **LTP arch-riscv64** (`tools/riscv/ltp_gate.py --suite arch-riscv64`): 158
   reviewed syscall tests, built for riscv64/musl and run in QEMU.
3. **LTP syscalls** (`--suite syscalls`): the 767-test subset used upstream.
   This is the broad sweep; it stays in the baseline list below.

Alongside them, `tools/riscv/concurrency-models/` model-checks the four kernel
protocols whose bugs only appear under specific interleavings (loom for the run
queue, `Waiter`/`Waker` and the pollee, TLA+/TLC for the network poll
scheduler); see [2026-11-04-concurrency-models.md](2026-11-04-concurrency-models.md).

```sh
# workflow checks against the board (key already authorized)
python3 -m tools.riscv.compat.run_workflow_checks \
    --host 58.206.232.122 --key ~/.ssh/asterinas-gate

# the same checks against a QEMU boot of the image (boots and tears down)
python3 -m tools.riscv.compat.run_qemu_workflow_checks

# LTP: build once, then run; each run leaves serial.log/progress.log/result.json
python3 tools/riscv/ltp_gate.py build --suite arch-riscv64
python3 tools/riscv/ltp_gate.py run  --kernel target/osdk/aster-kernel-osdk-bin.Image \
    --suite arch-riscv64 --skip-build

# a single test while debugging
python3 tools/riscv/ltp_gate.py run ... --tag rt_sigqueueinfo01
```

The runner dumps the captured output of every non-pass test, so a `CONF`
verdict carries the test's own `TCONF:` reason.

## Results, 2026-11-04

| Layer | Result |
| --- | --- |
| Workflow, board image (flashed before this round) | 9/10 — `tar_xz` fails, image has no `xz` |
| Workflow, QEMU image (rebuilt) | 10/10 |
| LTP arch-riscv64 (fixed kernel) | 141/158 pass, 17 CONF, 0 FAIL/CRASH/TIMEOUT |
| LTP syscalls | 642/767 pass, 57 FAIL, 68 CONF, 0 CRASH/TIMEOUT |

The board's single failure was real and is fixed in the image build:
`tar(1)` shells out to `xz` for `-J`, and the image shipped gzip only.
`profiles.py` now installs `xz-utils`; the rebuilt image passes in QEMU, and
the board will once it is reflashed.

## Found and fixed by this gate

- **`rt_sigqueueinfo` / `rt_tgsigqueueinfo` were missing entirely** (ENOSYS).
  Implemented with Linux semantics: `si_signo` comes from the syscall argument,
  a null `siginfo` is EFAULT, and only kernel/`kill()`/`tgkill()` `si_code`s
  are refused with EPERM. `kill(2)` now also resolves a non-leader thread ID
  to its thread group, which the test relies on. Both LTP tests pass.
- **`FUTEX_CMP_REQUEUE` returned ENOSYS.** Implemented with the compare under
  the bucket lock; the requeue operations now return woken+requeued (Linux's
  contract), reject negative counts with EINVAL, and treat a zero wake count
  as "wake nobody". `futex_cmp_requeue02` passes.
- **Scheduler panic under multi-process futex wake/requeue churn.** LTP's
  `futex_cmp_requeue01` (1000 children) panicked `exit_current` on 4 CPUs and
  passed on 1: a wake that arrived while a task was leaving the run queue
  pushed it back into another CPU's queue, and the stale entry later resumed
  the exited task's saved context. The run queue now refuses to re-enqueue a
  task whose `exit_pending` flag is set (checked under the queue lock). The
  full arch suite completes: 141 pass, 17 conf, 0 fail.
- **LTP `CONF` reasons were invisible.** The runner only dumped a test's output
  for FAIL/CRASH/TIMEOUT; it now dumps every non-pass, so the 21 skips in the
  arch suite are explained (e.g. `cacheflush` does not exist on riscv,
  `MAP_DROPPABLE`/`hugetlbfs` are unimplemented, several tests list x86-only
  architectures in `supported_archs`).

## Open findings, most severe first

1. **System V IPC and POSIX mqueue are absent**: `msgget`, `msgsnd`, `mq_open`
   (`ENOSYS`).
2. **fcntl family (24 of the 57 failures)**: lease (`F_SETLEASE`), `F_DUPFD`,
   pipe-size, and record-lock corner cases; several also need
   `/proc/sys/fs/pipe-max-size`.
3. **/proc gaps**: `/proc/self/smaps` (mlock05) and `/proc/sys/fs/pipe-max-size`
   (fcntl30) are missing.
4. **Socket option errno mismatches**: `getsockopt`/`setsockopt` return
   ENOPROTOOPT where LTP expects EINVAL; `socket`/`socketpair` swap
   EAFNOSUPPORT and EPROTONOSUPPORT.
5. **Semantics details**: `tgkill` should report EAGAIN at the pending-signal
   limit, `waitpid01` expects a core-dump flag, `sbrk01` hits ENOMEM,
   `readlink`/`readlinkat` accept a zero-length buffer, `pidfd_send_signal01`
   fails with EBADF.
6. **Timing-sensitive tests** (`select02`, `timerfd04`, `times03`, `sysinfo03`)
   may be TCG artifacts rather than kernel defects; re-check on hardware
   before treating them as gaps.

Full lists from the baseline run:

- FAIL: epoll_ctl04, fcntl01/01_64/11/11_64/12/12_64/16/16_64/17/17_64/19/
  19_64/20/20_64/21/21_64/23/23_64/27/27_64/30/30_64/31, gethostbyname_r01,
  gethostname02, getsockopt01, mlock05, mq_open01, msgget01, msgsnd01,
  name_to_handle_at01, madvise02, pidfd_send_signal01, pipe06, preadv203,
  preadv203_64, readlink03, readlinkat02, recv01, recvfrom01, recvmsg01,
  sbrk01, select02, semget02, send01, sendmsg01, sendto01, setfsuid04,
  setsockopt01, socket01, socketpair01, sysinfo03, tgkill02, times03,
  timerfd04, waitpid01
- CONF: chown01_16, clone10, eventfd06, execl01, execv01, execve01, execve06,
  execveat03, fchown01_16, fcntl24/24_64/25/25_64/26/26_64, fork05,
  fsconfig02, fsopen02, ftruncate04, futimesat01, getcontext01, getgid01_16,
  gethostid01, get_robust_list01, getuid01_16, listmount01, lstat03,
  mallinfo02, mallinfo2_01, mallopt01, mlock201/202/203, mremap07, madvise06/
  07/08/09/11/12, prctl06/07/10, preadv03, preadv03_64, sendfile09,
  sendfile09_64, setgid01_16, setregid01_16, setreuid01_16, setuid01_16,
  shmget02, statmount01, sync_file_range01, unlink09, umount01, utimensat01,
  futex_wake04, statx04/05/06/07/08/09/10/11

## gVisor conformance is x86-only

The repository's gVisor syscall suite is a set of **prebuilt x86-64 binaries**
(`GVISOR_PREBUILT_DIR`, with `ld-linux-x86-64.so.2` copied into the initramfs),
and gVisor does not target riscv64. The suite cannot be wired to RISC-V
without porting gVisor's test framework; the LTP suites above are the syscall
semantics layer for this architecture, and `test/initramfs/src/regression/`
remains the place for focused, fast regression tests.
