# Futex wake identity regression

After adding pid/tid fields to the opt-in `ASTERINAS_FUTEX_WAKE_DETAIL`
marker, the existing QEMU `pthread_cond_handoff` regression still passed:

```text
pthread_cond_handoff: passed 4096 handoffs
pthread condition handoff regression passed.
```

This confirms the diagnostic-only change did not alter futex wake behavior.

| Artifact | SHA-256 |
|---|---|
| kernel | `ef3155c722548e6422a53a630829c9305b7a99aa89e26c5ac3382aadb190ded8` |
| `qemu-serial.log` | `bb3642e382fc2bb0351f4064704b81ac80750883fa37f37832500d77b254be61` |
| `qemu.log` | `757d41a6fc49086126973a841c47b8e9dbe0edbaacf8ad3ec946f2bd1d2c7fe9` |

Command:

```text
tools/docker/run_dev_container.sh -- make run_kernel \
  AUTO_TEST=pthread_cond_handoff TARGET_ARCH=riscv64 SMP=4
```
