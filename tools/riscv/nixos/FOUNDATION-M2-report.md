# FOUNDATION-M2 report — LTP syscall gate on RISC-V

> 2026-08-14. Issue #14. Corresponds to plan
> `docs/superpowers/plans/2026-08-13-nixos-riscv-track.md` (the "LTP gate"
> milestone). Conclusion first: **the LTP syscall gate is integrated and runs
> end-to-end with one command** (`tools/riscv/ltp-gate.sh`); a first full pass
> on 446 of 533 enabled syscall tests yields **375 pass / 43 fail / 27 skip /
> 1 hang**, plus a kernel hang that stops the remaining tests.

## Deliverables

| File | Purpose |
|---|---|
| `tools/riscv/ltp-gate.sh` | One-command gate: build → pack → QEMU boot → report (SMP=1/4) |
| `tools/riscv/nixos/ltp/build_ltp.sh` | Cross-compile LTP syscalls for riscv64 + pack initramfs |
| `tools/riscv/nixos/ltp/ltp_runner.c` | In-guest runner: fork/exec each test under a watchdog, classify TPASS/TFAIL/TCONF/… |
| `tools/riscv/nixos/ltp/init_ltp.c` | `/init`: mount /proc /sys /tmp, exec the runner |
| `tools/riscv/nixos/ltp/boot_ltp_gate.py` | QEMU + U-Boot `booti` driver, SMP-parameterised |
| `tools/riscv/nixos/ltp/etc-{passwd,group}` | NSS files so `getpwnam("nobody")` resolves under musl |

## How to run

```bash
tools/riscv/ltp-gate.sh                 # build + SMP=1 + SMP=4
tools/riscv/ltp-gate.sh --smp 1         # one tier
tools/riscv/ltp-gate.sh --skip-build    # reuse existing initramfs
tools/riscv/ltp-gate.sh --rebuild-kernel --smp 1   # rebuild kernel, then gate
```

## Three kernel blockers found and worked around

The "obvious" integration paths each hit a kernel bug that is worth its own
follow-up; the gate routes around all three:

1. **Large-initramfs unpack hang.** Static-linked LTP binaries are ~267 KiB
   each (musl) or ~1.9 MiB (glibc); packing all 533 into the initramfs makes it
   ~140 MiB unpacked, and the kernel never finishes unpacking it (boot stalls
   at "spawn the virtio-block thread" at 100% CPU). Follow-up: investigate the
   initramfs/ramfs unpack path in `kernel/src/fs/rootfs.rs`.
2. **`mount -t ext2 /dev/vdb` → EINVAL.** Mounting the LTP binaries from a
   second virtio-blk disk (the upstream `LTP_DEV` approach, also M9's
   `/nix` persistence) fails; already documented in `m9/M9-report.md` §7.
3. **Static-link size.** Both glibc and musl *static* LTP binaries are too
   large for a single initramfs. Fix: build the test binaries **dynamically**
   against musl libc + a shared `libltp.so` (built from the libltp PIC objects).
   This shrinks the whole initramfs to **2.5 MiB**.

The chosen route: **dynamically-linked musl test binaries**, all in a 2.5 MiB
initramfs. The musl sysroot lacks the Linux UAPI headers, so the build
`-isystem`s the glibc cross sysroot's include dir (musl's own headers still win).

## Result (SMP=1)

446 tests ran before a hang (see below). Breakdown:

| Verdict | Count |
|---|---|
| PASS | 375 |
| FAIL | 43 |
| CONF (skip) | 27 |
| CRASH | 0 |
| TIMEOUT | 1 |

### Failures (43) — grouped

**rename (11)** — `rename01 rename03 rename04 rename05 rename06 rename07
rename08 rename10 rename12 rename13 rename15`. A systematic rename() bug on
tmpfs/ramfs.

**fork (4)** — `fork06 fork07 fork09 fork11`.

**fsopen/fsmount/fsconfig (6)** — `fsopen01 fsopen02 fsconfig01 fsconfig02
fsmount01 fsmount02` (the new mount API; implemented but failing).

**pwrite (4)** — `pwrite02 pwrite04 pwrite02_64 pwrite04_64`.

**sendfile (2)** — `sendfile07 sendfile07_64`.

**epoll (2)** — `epoll01` (hang), `epoll_wait04`.

**fcntl (2)** — `fcntl14 fcntl14_64`.

**readlink (2)** — `readlink03 readlinkat02`.

**posix_fadvise (2)** — `posix_fadvise03 posix_fadvise03_64`.

**singletons** — `access02 chdir02 pipe13 getitimer01 gethostname02 sbrk01
sched_setscheduler04`.

**environment gaps (not kernel bugs)** — `setrlimit04` (execlp `/bin/true`
→ ENOENT; the initramfs has no busybox/coreutils), `gethostbyname_r01`
(no `/etc/resolv.conf`/DNS under musl).

### Hang

The runner stops after `sigaltstack02` (the test after `sigaltstack01` in the
manifest) without printing its verdict or the summary. The guest is stuck there,
so `sigaltstack02` — alternate-signal-stack delivery — is a suspected kernel
deadlock and the highest-priority item to root-cause, since it blocks the
remaining ~87 tests.

## Follow-up work queue (priority order)

1. **`sigaltstack02` hang** — kernel signal/alt-stack deadlock; unblocks the tail.
2. **rename(2) family** — 11 tests, a likely single root cause in the VFS.
3. **fork(2) family** — 4 tests.
4. **pwrite(2) / sendfile / fcntl / epoll** — small clusters.
5. **missing `/bin/true` etc.** — add a static busybox to the initramfs to fix
   `setrlimit04` and friends; **`/etc/resolv.conf`** for `gethostbyname_r01`.
6. Re-run at **SMP=4** once SMP=1 is clean.

## Reproduce

```bash
# one-time LTP source (already done under target/ltp/src)
git clone --depth 1 --branch 20260529 \
    https://github.com/linux-test-project/ltp.git target/ltp/src
tools/riscv/ltp-gate.sh --smp 1
# serial transcript: target/ltp/ltp-gate-serial.log.smp1
```
