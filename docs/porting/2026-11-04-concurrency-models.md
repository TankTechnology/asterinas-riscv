# Concurrency models in the gate

Date: 2026-11-04

## Why

Four bugs in this port were concurrency bugs that took days to find by
running the kernel, and each one had a short, exact protocol behind it:

| Bug | Found by | Protocol |
| --- | --- | --- |
| OpenSSH sessions stopped responding | QEMU reproducer + LTP | `poll(2)` duplicate entries replacing each other's mask (`66d0290f7`) |
| `exit_current` panic on 4 CPUs | LTP `futex_cmp_requeue01` | A wake re-enqueuing a task that is leaving the run queue (`b6407ac8e`) |
| Network stalled after an immediate poll request | Board and QEMU runs | `"no poll"` sharing the value of `"poll now"` (`9a802125b`) |
| Lost wakeups under load | Review of `Waiter`/`Waker` | Wake/park ordering in `ostd/src/sync/wait.rs` |

Sampling interleavings by booting the kernel is how these were found; it is not
how they should be kept fixed. The models below make each protocol's invariant
executable.

## What runs

```sh
python3 tools/riscv/concurrency-models/run.py                 # ~20 s
python3 tools/riscv/concurrency-models/run.py --only pollee-poll
tools/riscv/concurrency-models/fetch-tla.sh                   # TLC jar into target/tla/
```

| Model | Tool | Kernel code |
| --- | --- | --- |
| `scheduler-run-queue/` | loom | `ostd/src/task/scheduler/` (exit, dequeue, wake, enqueue, pick) |
| `waitqueue-waker/` | loom | `ostd/src/sync/wait.rs` (`Waiter`, `Waker`) |
| `pollee-poll/` | loom | `kernel/src/process/signal/poll.rs` (`poll_with`, `register_poller`, `notify`) |
| `cow-fork/` | loom | `kernel/src/vm/vmar/vmar_impls/fork.rs`, `vm_mapping.rs` |
| `process-lifecycle/` | TLA+/TLC | `kernel/src/process/{wait,status}.rs`, `posix_thread/exit.rs` |
| `fork-spec/` | TLA+/TLC | design-level `fork`: CoW isolation, reference counts, lifecycle |
| `net-poll-scheduler/` | TLA+/TLC | `kernel/src/net/iface/{sched,poll}.rs`, `aster-bigtcp` `PollKey` |

Every model keeps the kernel's operation order and states the invariant the
kernel documents. The pre-fix variants are kept and asserted to **fail**:

- loom tests such as `buggy_protocol_re_enqueues_an_exiting_task` assert that
  loom *finds* the interleaving;
- TLC configs declare `\* EXPECT: violation NoLostImmediate` or `\* EXPECT: ok`,
  and `run.py` enforces both.

That inverts the usual failure mode. A model that silently drifts away from the
kernel would otherwise pass forever; here a drift makes the "buggy" run stop
producing a counterexample, and the suite fails.

## What the models check

- **Run queue**: a task that has started exiting is never put back into a run
  queue. loom explores the two-CPU interleaving where the wake's
  `set_if_is_none` fails, the exiting CPU clears its assignment, and the wake
  pushes the dead task; the fix is the `exit_pending` check under the queue
  lock.
- **Waiter/Waker**: a wake delivered before or during the park is not lost;
  two concurrent wakes deliver exactly one; a closed waiter is never woken.
- **Pollee**: duplicate poll entries for one object keep the union of their
  masks, and "if there are interesting events, either the events are returned
  or the poller is notified" holds when `poll_with` and `notify` race — both
  orders (notify first, registration first) are checked.
- **Network poll scheduler**: an immediate-poll request is never stranded
  behind a sleeping or indefinitely waiting thread, the thread never sleeps
  past the earliest deadline, and a real deadline never leaves it waiting
  without a timer.
- **Process lifecycle**: a reaped child exited before it was reaped, only its
  parent (or init after re-parenting) reaps it, a zombie is never orphaned,
  and init never exits. The pre-fix variants (reap before exit, no
  re-parenting, reap by anyone) are each asserted to break one invariant.
- **Copy-on-write fork**: a writable mapping's frame is otherwise
  unreferenced — no other address space maps it and the page cache does not
  hold it — under fault-vs-fault, fault-vs-unmap and fault-vs-remap races. The
  variant without the `reference_count() == 1` check is asserted to break the
  invariant.

## Limits

These are bounded model checks over extracted protocols, not proofs about the
kernel binary.

- loom runs the modeled operations under the C11 memory model with a bounded
  number of preemptions (`LOOM_MAX_PREEMPTIONS`, 6 in CI). It does not model
  interrupt masking, architecture barriers, or the code the model does not
  restate.
- TLC explores every reachable state of a discrete, time-bounded abstraction
  (`TimeLimit = 3`); the caller's "minimum over pending sockets" is a single
  `pending` variable, so the model checks the scheduler/thread protocol rather
  than the pending-set bookkeeping.
- The models duplicate the kernel's operation order by hand. The duplication is
  the point: changing either the kernel or the model requires re-establishing
  the correspondence, and the "buggy" variant tells you when the model no
  longer describes the old protocol.

## CI

`.github/workflows/test_riscv.yml` has a `concurrency-models` job: stable Rust,
a JRE, `fetch-tla.sh`, then `run.py --loom-max-preemptions 6`. It needs no
QEMU, no container and no board, and it finishes in about half a minute.
