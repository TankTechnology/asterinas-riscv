# Concurrency models

Executable models of the kernel protocols whose bugs are hardest to catch by
running the kernel: lost wakeups, double enqueues, exit/wake races. Each model
keeps the operation order from the kernel source, replaces the kernel's
primitives with the model checker's equivalents, and states the protocol's
invariant as an assertion the checker tries to violate.

They are cheap (the whole set runs in seconds) and they are the only part of
the test suite that *searches* interleavings instead of sampling them.

```sh
python3 tools/riscv/concurrency-models/run.py          # loom + TLC models
python3 tools/riscv/concurrency-models/run.py --kani   # ... plus the Kani proofs
python3 tools/riscv/concurrency-models/run.py --only pollee-poll
```

| Model | Tool | Kernel code | Bug it pins down |
| --- | --- | --- | --- |
| `scheduler-run-queue/` | loom | `ostd/src/task/scheduler/` | A wake re-enqueues a task that is exiting (`b6407ac8e`); reproduced by LTP `futex_cmp_requeue01` on 4 CPUs. |
| `waitqueue-waker/` | loom | `ostd/src/sync/wait.rs` | The `Waiter`/`Waker` handshake: a wake delivered before or during the park, and two wakers collapsing into one park. |
| `pollee-poll/` | loom | `kernel/src/process/signal/poll.rs` | Duplicate poll entries for one object replace each other's mask (`66d0290f7`); this is the OpenSSH hibernation hang on a pty. |
| `net-poll-scheduler/` | TLA+/TLC | `kernel/src/net/iface/{sched,poll}.rs` | `"no poll"` sharing the value of `"poll now"` strands an immediate request (`9a802125b`). |
| `cow-fork/` | loom | `kernel/src/vm/vmar/vmar_impls/fork.rs`, `vm_mapping.rs` | `fork`'s CoW rule: a writable mapping's frame must have no other reference (PTE or page-cache). |
| `process-lifecycle/` | TLA+/TLC | `kernel/src/process/{wait,status}.rs`, `posix_thread/exit.rs` | Who may reap whom: a reaped child exited first, only its parent reaps it, and no zombie is orphaned. |
| `fork-spec/` | TLA+/TLC | the design behind `kernel/src/vm/vmar` and `kernel/src/process` | What fork promises: a frame mapped in two processes is never writable in either, reference counts match the mappings, and the lifecycle rules hold. |
| `switch-handoff/` | loom | `ostd/src/task/processor.rs` | The `switched_to_cpu` handoff: two CPUs never run one task at once; the board's warning is the intended wait. |
| `lock-order/` | policy check | futex bucket pairs, VMAR, page table | Every two-lock path follows one total order, so those paths cannot deadlock. |
| `futex-bucket/` | loom | `kernel/src/process/posix_thread/futex.rs` | Futex wait/wake/requeue bookkeeping: a requeued waiter is woken on the target bucket and no wake is lost. |
| `kani:cow-decision` | Kani | `kernel/libs/cow-decision`, called from `vm_mapping.rs` | The shipped CoW decision function: a private mapping is upgraded in place only when it is the frame's sole reference. |

The buggy variants are kept and asserted to *fail*: `loom` must find the
interleaving, TLC must report the invariant violation. That makes the model a
regression test for the bug as well as for the fix — if either the kernel
protocol or the model drifts, the "buggy" run stops failing and the suite
turns red.

## What these models are, and are not

They are bounded model checking over a hand-extracted protocol, not a proof
about the kernel binary. Loom models the operations under the C11 memory model
with a bounded number of preemptions (`LOOM_MAX_PREEMPTIONS`); TLC explores
every reachable state of an abstract, discrete-time model. Neither covers the
kernel's interrupt masking, architecture barriers, or the parts of the code
that the model does not restate. Their value is that the invariants become
executable: change either side of a protocol and the model answers whether the
invariant still holds, in seconds, without booting anything.

## Adding a model

* **loom** — copy an existing crate (each has its own `[workspace]` so it is
  not part of the kernel workspace), keep the kernel's operation order, and
  write the invariant as an assertion. If the pre-fix protocol has a bug, add
  a second test asserting that loom finds it.
* **TLA+** — put a `Module.tla` plus `ModuleVariant.cfg` files in a directory;
  state the expected outcome in the config as a comment, which `run.py`
  enforces:

  ```tla
  \* EXPECT: ok
  \* EXPECT: violation SomeInvariant
  ```

## TLA+ tools

`run.py` looks for the TLC jar at `target/tla/tla2tools.jar`. Fetch it with:

```sh
tools/riscv/concurrency-models/fetch-tla.sh
```

The script downloads the current `tla2tools.jar` from the tlaplus release and
prints its checksum. The upstream release asset is replaced in place, so the
checksum is reported, not pinned.
