# Browser schedstat ledger implementation plan

**Goal:** Extend the bounded Firefox thread sampler with strict, raw and delta
per-thread schedstat evidence so physical workloads can distinguish execution,
runnable delay, and sleeping/blocking time.

**Architecture:** Parse Linux's three-field schedstat ABI into a typed immutable
value, collect it beside each stable TID's `/proc/.../stat`, and publish schema
version 2 intervals that retain both endpoints and their validated deltas.  Keep
the existing process sampler at schema version 1.  Treat malformed input,
counter regression, PID/TID reuse, and implausible runtime growth as fatal rather
than silently degrading the new schema.

## Task 1: Specify strict parsing and interval behavior

- Add parser tests for the exact three unsigned 64-bit fields and newline.
- Reject extra fields, missing newline, non-ASCII/negative values, and overflow.
- Add snapshot tests that require a schedstat file for every accepted TID.
- Add interval tests for raw endpoint preservation, deltas, and counter
  regression.

## Task 2: Implement schema version 2

- Add a typed `ThreadSchedstat` parser.
- Read schedstat beside thread stat after bounded TID enumeration.
- Publish nested `before`, `after`, and `delta` counters for every common TID.
- Remove `per-thread-runnable-wait` from unsupported fields while retaining
  physical HDMI scanout as unsupported.
- Preserve the existing CPU tick and affinity evidence.

## Task 3: Verify and qualify

- Run the focused Python unit suite and bytecode compilation.
- Run the CLI thread sampler against host Linux procfs as a compatibility
  control.
- Check the complete diff and commit the sampler as one atomic change.
- Defer physical Firefox claims until the exact kernel/rootfs is deployed and a
  bounded same-boot workload is captured.
