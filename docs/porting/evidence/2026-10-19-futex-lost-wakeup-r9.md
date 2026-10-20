# futex-lost-wakeup r9

This is the first complete bounded Asterinas RISC-V run of the minimal probe.
It uses the current retained kernel, `-m 512M -smp 2`, and no Firefox, Xorg,
DRM, network, or real hardware.

## RISC-V command and result

The static RISC-V `/init` was compiled with:

```text
riscv64-linux-gnu-gcc -DPROBE_PING_PONG_ROUNDS=5000 -DPROBE_REQUEUE_ROUNDS=1 -DPROBE_PPOLL_ROUNDS=1 -DPROBE_REQUEUE_SETTLE_MS=20 -O2 -static -pthread -Wall -Wextra -Werror futex_lost_wakeup_probe.c -o futex-lost-wakeup-r8-probe-riscv64
```

The runner waited for the complete `status=pass|fail` line before closing
QEMU. Earlier runs stopped at the buffered `status=` prefix; that was a host
runner error and is corrected in `run_qemu.py`.

Observed markers (46.47 seconds):

```text
FUTEX_H1 ping-pong rounds=5000 completed=5000 timeouts=0 errors=0 waits=3263 wakes=3168 signals=4378 status=pass
FUTEX_H2 detail ready=4 moved=1 woken=3 failed=0
FUTEX_H2 requeue rounds=1 waiters=4 failures=0 status=pass
FUTEX_H3 ppoll-pipe rounds=1 timeouts=0 errors=0 status=pass
FUTEX_LOST_WAKEUP_PROBE status=pass
```

The two long waits recorded by the existing bounded diagnostic stream ended
with `outcome=woken`. H1 therefore has no lost wakeup in this bounded RISC-V
run. The RISC-V round count is reduced from the x86 stress count to keep the
TCG run under the required 60-second bound; the x86 control remains 100,000
rounds.

## x86 control

The same source with the default 100,000 H1 rounds passed on x86_64 in 4.49s:

```text
FUTEX_H1 ... rounds=100000 completed=100000 timeouts=0 errors=0 status=pass
FUTEX_H2 ... failures=0 status=pass
FUTEX_H3 ... timeouts=0 errors=0 status=pass
FUTEX_LOST_WAKEUP_PROBE status=pass
```

## Interpretation

- H1 is **not reproduced** on either control or bounded RISC-V Asterinas.
- H2 and H3 pass in the RISC-V smoke run after correcting the H2 assertion:
  `FUTEX_REQUEUE` returns the directly woken count (`moved=1`); the remaining
  three waiters were woken from the destination queue (`woken=3`).
- No kernel semantic patch was applied. The changes in this round are only
  the reproducer, stage markers, and bounded runner/evidence handling.

Artifacts:

- RISC-V boot disk: `/mnt/mydisk/futex-lost-wakeup-r8/boot.ext4`
- RISC-V serial log: `/mnt/mydisk/futex-lost-wakeup-r9-serial.log`
- Runner result: `/mnt/mydisk/futex-lost-wakeup-r9-result.json`
- RISC-V binary: `/mnt/mydisk/futex-lost-wakeup-r8-probe-riscv64`
