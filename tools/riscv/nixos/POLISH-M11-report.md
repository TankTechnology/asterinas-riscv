# POLISH-M11 — per-item deep-dive of the 31 residual LTP failures

Date: 2026-08-16
Branch: `track/nixos`
Status: **Complete — every residual FAIL/TIMEOUT re-examined one-by-one with a
raw-syscall isolation probe; zero genuine kernel bugs confirmed, two of M10's
aggregate classifications corrected.**

M10 produced the full 533-test scorecard (473/28/29/3) and concluded "zero
genuine kernel bugs" at the *class* level. M11 digs one level deeper: it
classifies each of the 28 FAIL + 3 TIMEOUT items individually, minimally
reproduces the three TIMEOUTs, and — for the six items that *look* like kernel
boundary-semantics bugs — isolates kernel behaviour from the musl libc wrapper
with raw `syscall(N, …)` probes. The kernel is correct in every one of them.

---

## 1. Method: raw-syscall isolation

The LTP suite is cross-compiled against **musl libc** (see `build_ltp.sh`).
Several residual failures could be either (a) a kernel errno bug or (b) a musl
wrapper that diverges from Linux/glibc. To tell them apart we extended
`tools/riscv/nixos/ltp/repro.c` — the existing minimal static initramfs probe —
with the exact syscalls in question, called **both** through musl and through
`syscall(SYS_*, …)` (which bypasses musl and hits the kernel raw ABI directly):

| # | Probe | musl call | raw `syscall()` | Verdict |
|---|---|---|---|---|
| 1 | `readlink(sym, buf, 0)` | returns **0** (success) | `SYS_readlinkat(78,…)` = **-1 EINVAL** | kernel correct |
| 2 | `readlinkat(dfd, "sl", buf, 0)` | returns **0** | `SYS_readlinkat(78,…)` = **-1 EINVAL** | kernel correct |
| 3 | `sbrk(+8192)` / `sbrk(-8192)` | **-1 ENOMEM** | `SYS_brk(cur+8192)` = **cur+8192** | kernel correct |
| 4 | `sched_setscheduler(FIFO\|RESET_ON_FORK)` | n/a | `SYS_sched_setscheduler(119)` = **0** | kernel correct |
| 5 | `sched_getscheduler(child)` | **-1 ENOSYS** | `SYS_sched_getscheduler(120)` = **0 (SCHED_NORMAL)** | kernel correct |
| 6 | `sched_getparam(child)` | **-1 ENOSYS** | `SYS_sched_getparam(121)` = **0, prio 0** | kernel correct |

Full probe output (one QEMU boot, `run_repro.sh`):

```
[FAIL] readlink(sym,0) -> EINVAL (ret=0 errno=0)
[raw] SYS_readlinkat(dfd,sl,buf,0) ret=-1 errno=22 Invalid argument
[FAIL] sbrk(+8192) extends (ret=-1 errno=12 Out of memory)
[raw] SYS_brk: cur=1007616 brk(1015808)=1015808 expect=1015808 OK
[raw] sched_setscheduler(FIFO|RESET_ON_FORK,prio=10) ret=0 errno=0
[sched] child policy=-1 ... parent policy=-1   (musl wrappers ENOSYS)
[raw] SYS_sched_getscheduler(2) = 0            (child reset to SCHED_NORMAL)
[raw] SYS_sched_getparam(2) ret=0 errno=0 prio=0
[raw] SYS_sched_getscheduler(parent) = 1       (parent still SCHED_FIFO)
[fork] 20 fork+wait cycles = 3910788 us (195.5 ms/fork)
```

---

## 2. Classification table — 31 residual items → 3 buckets

| Bucket | Count | Items | Nature |
|---|---|---|---|
| **A. 测试环境 (environment)** | 8 | `epoll_wait04`, `sendfile07`, `sendfile07_64`, `timerfd01`, `fork06`, `epoll01`, `fcntl14`, `fcntl14_64` | TCG latency / host-load / slow-fork-perf |
| **B. 配置 (missing feature)** | 17 | `rename01/03/04/05/06/07/08/10/12/13/15`, `fsopen01/02`, `fsconfig01/02`, `fsmount01/02` | no `/dev/loop*` block device |
| **C. libc 语义 (musl, not kernel)** | 6 | `gethostbyname_r01`, `gethostname02`, `readlink03`, `readlinkat02`, `sbrk01`, `sched_setscheduler04` | musl wrapper diverges from glibc/Linux |

The **内核边界语义 (kernel boundary-semantics) bucket is empty**. All six items
in C were *suspected* kernel bugs; §3 disproves each with raw-syscall evidence.
The M7–M9 fix chain remains sound.

### 2.1 Bucket A — environment (8)

| Item | Verdict | Evidence |
|---|---|---|
| `fork06` | FAIL → TBROK | `fork_procs -n 1000`; 1000 fork+wait × ~196 ms ≈ **196 s** > LTP 240 s timeout (`Test timeouted, sending SIGKILL!`) |
| `epoll01` | TIMEOUT | hangs in `Testing epoll_ctl`, which wraps each `epoll_ctl` in `tst_fork()` (fork-per-test) |
| `fcntl14` | TIMEOUT | `op_nums = 5000` forks × ~196 ms ≈ **980 s** ≫ 300 s runner watchdog |
| `fcntl14_64` | TIMEOUT | same as `fcntl14` (`.test_variants = 2`) |
| `epoll_wait04` | FAIL | `epoll_wait() waited for 5264us with a timeout equal to zero` — returns the correct **0**, just 5 ms of single-syscall TCG latency vs a 1 ms threshold |
| `sendfile07(_64)` | FAIL → TBROK | 65536 one-byte `SOCK_DGRAM` writes, slow-fill under TCG; **PASSes in isolation** (M10 §4.1) |
| `timerfd01` | FAIL | `got 5 tick(s) expected 3` — host-load sleep-vs-deadline drift; **PASSes in isolation** (M10 §4.1) |

Fork latency measured directly: **195.5 ms/fork** under `-smp 1` QEMU TCG. The
root cause is `Vmar::fork_from` → `cow_copy_pt` walking every page-table entry
(an O(address-space) COW), amplified ~100× by TCG. This is a *performance*
characteristic, not a correctness bug — every fork is semantically correct, it
is just slow. The single fix that collapses all four fork-perf items (and the
known SMP=4 `/init` hang) is lazy/shared-page-table COW — a VM rewrite tracked
as future work (M10 §6.1), not a point bug.

### 2.2 Bucket B — missing loop device (17)

All 17 tests `TBROK` *before* reaching their syscall-under-test:

```
tst_device.c:149: TINFO: No free devices found
tst_device.c:440: TBROK: Failed to acquire device
```

They use LTP's `.mount_device = 1` / `.needs_device`, which acquires a free
`/dev/loop*` block device. Asterinas has **no loop device**, so `tst_device`
finds nothing and the harness breaks out.

**Correction to M10:** M10 split these into "missing mount API (`fsopen/
fsconfig/fsmount`, 6)" + "missing loop device (`rename*`, 11)". That is wrong
for the fs\* six: they fail at device acquisition (`Failed to acquire device`),
**not** at `fsopen()` returning `ENOSYS`. The single underlying gap for all 17
is the loop device. (`fsopen/fsconfig/fsmount` are also unimplemented, but the
tests never get there.)

### 2.3 Bucket C — musl libc semantics (6, none a kernel bug)

| Item | LTP expectation | musl behaviour | Kernel behaviour (raw) |
|---|---|---|---|
| `gethostbyname_r01` | `gethostbyname_r` returns `ERANGE` for a 991-char name (GHOST CVE-2015-0235) | returns **0** | n/a — pure libc/NSS, no kernel syscall |
| `gethostname02` | `gethostname(host, len-1)` returns `ENAMETOOLONG` | returns **0** (silently truncates) | n/a — musl `gethostname` is built on `uname`, never `ENAMETOOLONG` |
| `readlink03` | `readlink(buf, 0)` returns `EINVAL` | returns **0** | `SYS_readlinkat(…,0)` = **EINVAL** ✓ |
| `readlinkat02` | `readlinkat(fd, p, buf, 0)` returns `EINVAL` | returns **0** | `SYS_readlinkat(…,0)` = **EINVAL** ✓ |
| `sbrk01` | `sbrk(±8192)` returns old break | **-1 ENOMEM** | `SYS_brk(cur+8192)` = **cur+8192** ✓ |
| `sched_setscheduler04` | child reset to `SCHED_NORMAL`/prio 0 after fork | `sched_getscheduler`/`getparam`/`setscheduler` all **ENOSYS** | `SYS_sched_getscheduler(120)` child = **0**, `getparam` prio = **0**, parent = **SCHED_FIFO** ✓ |

Notes:

- **`sched_setscheduler04`** is the most subtle. The raw syscalls prove the M7
  `SCHED_RESET_ON_FORK` work is fully correct: the forked child's policy is
  `SCHED_NORMAL` (0) and priority 0, while the parent stays `SCHED_FIFO` (1).
  The LTP test's *syscall variant* drives `sched_setscheduler` via raw syscall
  (which works), but then checks the child with the **musl libc**
  `sched_getscheduler()`/`sched_getparam()`, which return `ENOSYS` on this
  riscv64 musl build. So the test reports "Policy NOT reset" — a musl-wrapper
  artifact, not a kernel defect. (The *libc variant* is skipped outright:
  `TCONF: sched_setscheduler not supported`.)

- **`readlink03`/`readlinkat02`** fail only their `bufsiz == 0` case. musl's
  `readlink`/`readlinkat` short-circuit a zero buffer and return 0 instead of
  forwarding to the kernel; the kernel's `sys_readlinkat` correctly returns
  `EINVAL` (as the raw probe shows). All seven other errno cases (EACCES,
  EINVAL-for-non-symlink, ENAMETOOLONG, ENOENT, ENOTDIR, ELOOP, EFAULT) pass.

- **`sbrk01`**: the kernel `brk` syscall extends/shrinks correctly (raw probe
  returns the requested address), but musl's `sbrk()` returns `ENOMEM` for any
  nonzero increment — a musl stub limitation, not a kernel gap.

- **`gethostbyname_r01`** is a glibc-specific GHOST (CVE-2015-0235) regression;
  the test itself is tagged `glibc-git`/`CVE`. musl is not vulnerable and does
  not reproduce the `ERANGE` return. **Correction to M10**, which filed it as
  "Env gap (no DNS in initramfs)": it is musl semantics, not DNS (the name is
  991 zeros, resolvable without DNS; the assertion is on the return code).

- **`gethostname02`**: musl implements `gethostname(3)` over `uname(2)` (there
  is no `gethostname` syscall on riscv64, and Asterinas — correctly — only
  implements `sethostname`), so it silently truncates and never returns
  `ENAMETOOLONG`.

---

## 3. The three TIMEOUTs — kernel vs environment

| Item | Minimal repro | Root cause | Kernel or env? |
|---|---|---|---|
| `epoll01` | hangs in `epoll_ctl` (fork-per-test) | fork ~196 ms under TCG | **env** (TCG fork perf) |
| `fcntl14` | 5000 forks → ~980 s | fork ~196 ms under TCG | **env** (TCG fork perf) |
| `fcntl14_64` | same as `fcntl14` | fork ~196 ms under TCG | **env** (TCG fork perf) |

All three are the slow-fork class, measured at **195.5 ms/fork** by the probe
(§1). None blocks, deadlocks, or mis-waits; they simply fork more times than a
240 s/300 s budget allows at TCG speed. On a lazy-COW kernel (or bare metal)
they would complete. This is an *environment/perf* issue, not a kernel
correctness bug, and it shares its root with the SMP=4 `/init` hang.

---

## 4. Fixes applied

**None.** M11 found no genuine kernel bug. Every item in the suspected
"kernel boundary-semantics" bucket was disproven with raw-syscall isolation
(§2.3): the kernel returns the exact Linux errno/value the tests expect; the
failures are musl-wrapper divergences. Fixing those would mean patching musl
(or switching the LTP cross-compile to glibc), which is out of scope and
would not touch Asterinas.

The only *fixable* residuals remain two large, previously-tracked features,
not point bugs:

- **loop device** (17 tests) — unblocks `rename*` + `fs*` in one feature.
- **lazy COW** (4 tests + SMP=4 hang) — `fork_from`/`cow_copy_pt` rewrite.

---

## 5. Wrap-up

- `tools/riscv/nixos/ltp/repro.c` extended with the sched + fork-latency probes
  (committed with this report); it is now the canonical one-boot minimal repro
  for the "boundary-semantics" class.
- No kernel source changed, so no new flow-to-main PR is needed this round.

### Next steps

1. **loop device** — single feature unblocking 17/31 residual tests.
2. **lazy COW** — collapses `fork06`/`fcntl14(_64)`/`epoll01` + the SMP=4 hang.
3. **glibc LTP variant** — re-baselining the suite against a glibc-linked LTP
   would clear `sched_setscheduler04`/`readlink03`/`readlinkat02`/`sbrk01`/
   `gethostname02`/`gethostbyname_r01` in one move (they are all musl-wrapper
   artifacts), but that is a build-tooling change, not a kernel change.
4. **Quiescent-host re-run** — confirm the timing flakes return to PASS with no
   concurrent QEMU on the host.
