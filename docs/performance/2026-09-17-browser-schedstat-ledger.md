# Browser schedstat ledger qualification, 2026-09-17

This record qualifies the host-side and guest-side collection path for the
Linux-compatible per-thread schedstat interface.  It follows the kernel ABI
qualification in `2026-09-17-schedstat-qualification.md` and does not claim a
Firefox speedup or a physical bottleneck result.

## Schema version 2

Thread-mode `browser_system_time.py` now requires one canonical schedstat
record for every sampled TID.  It accepts exactly three unsigned 64-bit decimal
fields separated by single spaces and terminated by one newline.  Missing,
malformed, overflowing, or regressing counters fail the capture rather than
silently falling back to schema version 1.

Each common TID in an interval retains:

- the existing user/kernel CPU-tick deltas and last-CPU endpoints;
- raw schedstat `before` and `after` values;
- schedstat deltas for CPU runtime, completed runnable-queue wait, and dispatch
  count.

Process start time and per-thread start time continue to guard PID/TID reuse.
The sampler also rejects implausible per-thread runtime growth.  The report no
longer lists per-thread runnable wait as unsupported; physical HDMI scanout
latency remains explicitly unsupported.

The process-only sampler remains schema version 1 because its format did not
change.  Existing artifacts are therefore not silently relabelled.

## Verification

The implementation is commit
`33baca470` (`Record per-thread schedstat in browser ledger`).  Its focused
suite passed all 26 tests:

```bash
python3 -m unittest tools.riscv.tests.test_browser_system_time -v
```

The adjacent browser timing, capture, provenance, and latency suites passed all
59 tests:

```bash
python3 -m unittest \
  tools.riscv.tests.test_browser_system_time \
  tools.riscv.tests.test_browser_interaction_perf \
  tools.riscv.tests.test_browser_perf_capture \
  tools.riscv.tests.test_browser_performance_provenance \
  tools.riscv.tests.test_browser_latency_contract -v
```

Bytecode compilation completed without error.  Two Stage1 builder tests also
passed, including deterministic raw-newc construction, so the changed script is
actually carried into the ephemeral `/run/asterinas-tools` payload used on the
board.

A one-interval CLI capture against the development host's real Linux procfs
produced schema version 2 with `before`, `after`, and `delta` schedstat records.
This is an ABI compatibility control only; its zero-delta shell sample is not a
Firefox performance measurement.

## Next physical experiment

The next accepted evidence must deploy the exact kernel and Stage1 hashes, then
collect fixed-duration idle, local interaction, and local navigation phases in
one Asterinas boot.  Analysis will compare per-thread wall time with schedstat
CPU and completed runnable-wait deltas.  A kernel optimization is admitted only
after the same delay class repeats across bounded phases; public Baidu timing is
corroborating evidence because network and site variance are uncontrolled.
