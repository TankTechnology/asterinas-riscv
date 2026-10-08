# Scheduler handoff probe

This standalone Linux-compatible user-space probe measures round trips between
two threads pinned to requested CPUs.
It helps compare scheduler and synchronization costs under controlled placement.
It does not change kernel scheduling policy or boot settings.

## Build and run

Build inside the project development container:

```sh
cc -O2 -pthread -Wall -Wextra -Werror \
    tools/benchmarks/sched_handoff/sched_handoff.c -o /tmp/sched_handoff
/tmp/sched_handoff yield 0 0 10000
/tmp/sched_handoff blocking 0 0 10000
/tmp/sched_handoff blocking 0 1 10000
```

The CLI is `sched_handoff MODE CPU_A CPU_B ITERATIONS`.
`MODE` is `yield` or `blocking`;
`ITERATIONS` must be between 1 and 100000, inclusive.
CPU IDs must fit in `cpu_set_t` (0 through 1023 on the supported Linux targets).
Choose CPUs available to the process and its enclosing cpuset.
Cross-compile with a cached target compiler and `-static` when deploying to a
target without a compatible dynamic loader.

The main thread is A and the responder is B.
Each independently sets its affinity to a single CPU and verifies the full mask
by reading it back before entering a startup barrier.
Both must already use `SCHED_OTHER` with priority zero;
other scheduling policies are rejected with a diagnostic.
Timing begins in A after the barrier.

In `yield` mode, the threads exchange a release/acquire atomic turn;
a waiting thread repeatedly calls `sched_yield()` until it owns the turn.
In `blocking` mode, A posts a request semaphore and waits on a response semaphore;
B waits for the request and posts the response.
Semaphore waits retry `EINTR`.
On distinct CPUs a semaphore wait can find an already available token,
so a round trip need not include a blocked thread or a context switch.

## Results and limits

Success writes exactly one JSON object on one line after both threads finish:

```json
{"mode":"blocking","cpu_a":0,"cpu_b":0,"confirmed_cpu_a":0,"confirmed_cpu_b":0,"policy":"SCHED_OTHER","iterations":100,"completed":100,"elapsed_ns":500000,"samples":100,"min_ns":4000,"p50_ns":4800,"p95_ns":6000,"p99_ns":8000,"max_ns":9000}
```

These illustrative values are not benchmark results.
`confirmed_cpu_a` and `confirmed_cpu_b` report each verified singleton affinity;
`policy` confirms the checks for both threads.
`completed` and `samples` both equal `iterations` on success.

A records one `CLOCK_MONOTONIC` round-trip sample per iteration into a
preallocated array bounded at 100000 entries (800000 bytes).
Each sample starts immediately before sending the request/turn and ends after
receiving the response/turn.
`elapsed_ns` spans all iterations, including clock reads, loop accounting,
and storing the samples.
Startup, thread creation, affinity checks, joining, sorting, and printing
are outside that elapsed interval.
Every sample-array entry is written before thread creation to fault in its pages
outside the timed interval.
There is no allocation or printing in the handoff loop.

Statistics are computed after sorting samples in ascending order.
For percentile `p`, nearest rank selects the one-based sample
`ceil(p * samples / 100)`.
`min_ns` and `max_ns` are the first and last samples;
`p50_ns`, `p95_ns`, and `p99_ns` use that nearest-rank definition.
These are round-trip measurements including synchronization and clock overhead,
and must not be reported as raw one-way context-switch latency.

Invalid input, unavailable affinity, unsupported policy, or a runtime error
exits nonzero with a stderr diagnostic and no success record.
A 20-second process alarm terminates with `_exit(124)`
if startup, measurement, or result output stalls.
Timeout has no message because writing even to stderr may block;
the raw exit status 124 is the timeout diagnostic.
The alarm remains active until the result has successfully flushed to stdout.
Keep the raw exit status alongside stdout and stderr when collecting runs.
Record the exact binary, kernel, CPU placement, load, and logging settings;
this probe alone cannot establish the cause of an operating-system difference.

## Native integration tests

```sh
python3 tools/benchmarks/sched_handoff/test_probe.py
```

The standard-library test runner builds the real executable with
`cc -O2 -pthread -Wall -Wextra -Werror` in a temporary directory.
It checks rejected arguments, unavailable affinity in either thread,
same-CPU yield/blocking, distinct-CPU blocking when two CPUs are available,
affinity confirmations, completion counts, and ordered statistics.
The one-iteration case checks that every reported quantile equals that sample.
Blocked-output tests fill a pipe and check that the real process alarm
terminates the probe with status 124, both with separate stderr
and with stdout and stderr sharing the full pipe.
These tests add about 40 seconds to the suite.
The tests require Linux affinity APIs and the sysfs online-CPU list;
they do not require root privileges or downloads.
Native test timing is a functional smoke check, not target performance evidence.
