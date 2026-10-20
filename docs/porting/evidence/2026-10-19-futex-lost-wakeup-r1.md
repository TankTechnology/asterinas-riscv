# futex-lost-wakeup r1

This is the first bounded baseline for the minimal reproducer. It does not
start Firefox, Xorg, DRM, the board, or a long QEMU gate.

## Scope and inputs

- Source: `tools/riscv/futex_lost_wakeup/futex_lost_wakeup_probe.c`
- Build helper: `tools/riscv/futex_lost_wakeup/build_probe.sh`
- Output directory: `/mnt/mydisk/futex-lost-wakeup-r1/`
- The riscv64 binary was cross-compiled in `asterinas-qemu-gl-env-r2` with
  `riscv64-linux-gnu-gcc -O2 -static -pthread -Wall -Wextra -Werror`.
- The x86_64 binary was built with the same flags using host `gcc`.

## x86_64 control run

Command (bounded at 60 seconds):

```text
/usr/bin/time -f 'elapsed=%e sec' timeout 60 /mnt/mydisk/futex-lost-wakeup-r1/futex-lost-wakeup-x86_64
```

Observed output, elapsed 4.49 seconds:

```text
FUTEX_H1 ping-pong rounds=100000 completed=100000 timeouts=0 errors=0 waits=292 wakes=11 signals=89 status=pass
FUTEX_H2 requeue rounds=100 waiters=4 failures=0 status=pass
FUTEX_H3 ppoll-pipe rounds=1000 timeouts=0 errors=0 status=pass
FUTEX_LOST_WAKEUP_PROBE status=pass
```

The first attempt was rejected as a test-classification error: a check-to-sleep
race returned `EAGAIN` and was incorrectly counted as an error. The probe now
retries `EAGAIN`, as required by the futex ABI; this is a test correction, not
a kernel change.

## Decision table

| Case | Workload | x86_64 result | RISC-V artifact | RISC-V Asterinas runtime |
|---|---|---:|---:|---:|
| H1 | two-thread ping-pong, `SA_RESTART` `SIGUSR1`, 100,000 rounds | pass; 0 timeouts | built | not run in r1 |
| H2 | four waiters, `FUTEX_REQUEUE_PRIVATE`, 100 rounds | pass; 0 failures | built | not run in r1 |
| H3 | `ppoll` + pipe, 1,000 rounds | pass; 0 timeouts/errors | built | not run in r1 |

## Hashes

```text
source b208e6ac0d403e73093eb837f46b32edae65e760c247e30b883536a64fbc8f65
riscv64 4605481353533c868374ad0cd44afa3d201d6a41407f4d5047a8c1964607a936
x86_64  a673acfe7c5862629d2fca88aeb29ccd303514bbf74a05aa4fbff7bb969b6b5d
run-log 0f69c75d992e82a41fae2ff36eeed284e829cacb45c4aa3ddef5c18bd53e0237
```

The x86 control is clean. No claim about a RISC-V lost wakeup is made until
the same binary runs as the only init program under Asterinas with `-m 512M
-smp 2`.
