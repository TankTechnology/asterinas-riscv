# Run-queue / exit / wake model

A loom model of the protocol in `ostd/src/task/scheduler/` and
`ostd/src/task/processor.rs`: a task is woken from another CPU while it is
leaving the run queue to exit. The model keeps the real algorithm (including
the `set_if_is_none` double-check) and substitutes loom atomics/mutexes for
the kernel's `AtomicCpuId` and run-queue spin lock.

Two tests:

- `buggy_protocol_re_enqueues_an_exiting_task` checks that loom *finds* the
  interleaving that puts a dead task back into a queue. This is the race fixed
  in `b6407ac8e` and reproduced by LTP's `futex_cmp_requeue01` on four CPUs
  (`exit_current`'s `unreachable!()`).
- `fixed_protocol_never_re_enqueues_an_exiting_task` asserts the invariant
  holds in every interleaving loom explores.

```sh
cargo test                       # default preemption bound
LOOM_MAX_PREEMPTIONS=6 cargo test
```

This is bounded model checking over a hand-extracted protocol, not a proof
about the kernel binary: it covers the interleavings of the modeled
operations under loom's C11 memory model, not the kernel's actual IRQ
disabling or architecture barriers. Its value is that the invariant becomes
executable: change either side of the protocol and the model says whether the
invariant still holds.

To add a protocol, copy this crate, keep the exact operation order from the
kernel, and write the invariant as an assertion that loom tries to violate.
