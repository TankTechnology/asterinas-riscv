# Bounded futex wake pairing instrumentation

The opt-in `asterinas.futex_profile=1` path now emits at most 128 `ASTERINAS_FUTEX_WAKE_DETAIL` records containing futex address, bitset, requested count, actual woken count, and visibility. Default boots do not execute this logging path.

This complements the existing `ASTERINAS_FUTEX_WAIT_DETAIL` records and is intended to pair Firefox r12 wait/timeout observations with actual wake attempts. No Firefox runtime was rerun after this instrumentation.

Verification:

```text
tools/docker/run_dev_container.sh -- make run_kernel AUTO_TEST=pthread_cond_handoff TARGET_ARCH=riscv64 SMP=4
pthread_cond_handoff: passed 4096 handoffs
pthread condition handoff regression passed.
```

Source SHA-256: `53f0d7b84d92b02b2881948a42e7279a7d404983996b6c055841e69a6c5c06e4`
