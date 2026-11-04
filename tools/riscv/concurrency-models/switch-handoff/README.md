# Scheduler switch handoff model

A loom model of the `switched_to_cpu` protocol in `ostd/src/task/processor.rs`:

- `before_switching_to` spins until it can CAS the task's flag from false to
  true, and logs "Switching to a task already running in the foreground" once
  per failed attempt;
- `after_switching_to`, running at the top of the task that was switched to,
  clears the *previous* task's flag.

The board prints that warning regularly. The model answers what it is:

| Test | Result |
| --- | --- |
| `the_handoff_keeps_two_cpus_from_running_one_task` | two CPUs never execute one task at a time |
| `the_handoff_wait_is_observable` | loom reaches the interleaving that makes the CPU wait, i.e. the warning is the handoff wait |
| `without_the_flag_two_cpus_can_run_one_task` | negative control: drop the flag and loom finds the double execution |

Conclusion: the flag protocol is correct; the warning is expected contention,
not a correctness bug. Whether the contention is *frequent* enough to hurt is a
scheduling/performance question, not a safety one.

## Scope

The model covers the flag, not the run queues: enqueue/dequeue, the wake path
and the CPU assignment are in `scheduler-run-queue/`. It does not model the
context switch itself or interrupt timing.
