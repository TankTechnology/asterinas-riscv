# Firefox Daily-Use Performance Qualification Design

## Context

The physical Firefox daily-use profile currently cannot reach its measured
input, scroll, navigation, context-switch, and composite phases.

Command-level physical evidence separates two independent causes.
The initial Marionette `WebDriver:NewSession` takes roughly 196--233 seconds,
so it now has a dedicated 300-second physical setup budget.
After that setup succeeds, the local fixture reaches a terminal capability
report in which storage and canvas checks pass, while WebAssembly, Web Worker,
audio, and `fetch` checks fail.
The current daily-use adapter requires every optional browser capability to
pass before it accepts the fixture document.
It therefore retries the already-terminal failure until the 120-second phase
deadline and prevents all performance measurements.

This is a contract mismatch rather than evidence of a performance-phase
timeout.
The strict browser functionality gate must continue to reject these missing
capabilities.
The performance profile must report them honestly without allowing unrelated
feature coverage to prevent measurement of supported daily-use paths.

## Goals

- Preserve the strict, fail-closed browser functionality gate unchanged.
- Require every prerequisite used by the performance workload to pass.
- Record optional capability failures as bounded, explicit unsupported states.
- Permit a daily-use performance result to qualify only when it contains no
  functional failure and all performance prerequisites pass.
- Keep the exact capability report in the fixture artifact.
- Resume same-board baseline collection without claiming that unsupported
  Firefox features work.

## Non-goals

- Do not implement WebAssembly, Web Worker, audio, or `fetch` support here.
- Do not loosen the input, scroll, navigation, context-switch, sampler, process
  identity, or artifact-integrity checks.
- Do not classify slow measurements as speedups or kernel bottlenecks.
- Do not make public-network behavior part of the deterministic profile.

## Qualification Model

The existing function groups remain the public evidence vocabulary:

- Required performance prerequisites: `document`, `storage`, `navigation`,
  `download`, and `contexts`.
- Optional capability coverage: `execution` and `rendering-media`.

A required group must be `pass`.
An optional group may be `pass` or `unsupported`, but never `fail`.
Any `fail` state makes the daily-use result fail.
An `unsupported` optional group does not assert functionality;
it permits only the supported performance workload to qualify.

The `execution` group is derived from the WebAssembly, Web Worker, and `fetch`
checks.
The `rendering-media` group is derived from the canvas and audio checks.
The `storage` group continues to require local storage, session storage,
cookies, and IndexedDB.

An optional group is `unsupported` only when the fixture returns a terminal,
well-formed capability report and at least one owned check is false.
A missing, malformed, non-terminal, or internally inconsistent report remains
a phase failure.

## Fixture Data Flow

The daily-use fixture adapter will wait for both conditions below within the
existing bounded phase deadline:

1. The exact local document is ready, including origin, title, Latin/CJK text,
   required form controls, image, navigation timing, and bounded local resource
   entries.
2. The capability report is terminal (`complete` or `error`) and has the exact
   closed-schema set of checks.

The adapter will then:

1. Validate the required document and storage checks.
2. Derive `execution` and `rendering-media` as `pass` or `unsupported`.
3. Trigger and verify the existing controlled download.
4. Store the unchanged probe, snapshot, capability report, and download digest
   in `browser-fixture-capture.json`.

The standalone browser functionality gate will continue to require every
capability check to be true.

## Result and Limitation Semantics

The daily-use contract will derive top-level `state=pass` when all required
groups pass and optional groups are either `pass` or `unsupported`.
It will derive `state=fail` for every failed required or optional group.

Whenever an optional group is unsupported, the result must include the new
closed-schema limitation `fixture-capabilities-incomplete`.
Conversely, that limitation is invalid when all optional groups pass.
This bidirectional rule prevents silent weakening and stale limitations.

The physical runner's qualification predicate will use the same helper as the
guest contract.
It must reject:

- a failed required group;
- a failed optional group;
- an unknown group, reason, or limitation;
- unsupported capability groups without the required limitation;
- a limitation claiming incomplete capabilities when both optional groups pass.

Published function-group entries remain the authoritative feature statement.
A passing performance result with `execution=unsupported` does not claim that
WebAssembly, workers, or `fetch` work.

## Error Handling

- A fixture deadline remains a bounded phase failure.
- A capability report stuck in `running` remains a phase failure.
- A malformed capability report remains contract-invalid or phase-invalid.
- A false required storage check makes `storage` fail and prevents publication
  of a passing result.
- A failed DOM, local resource, form, download, context, sampler, identity, or
  cleanup check remains fatal.
- Failure checkpoints continue to preserve completed phases and validated
  function groups without publishing a result bundle as passing.

## Verification

Tests will be written before implementation and will cover:

- all capabilities passing, preserving the current all-pass result;
- a terminal report matching the physical observation, producing
  `execution=unsupported`, `rendering-media=unsupported`, and the required
  limitation;
- each required storage or document prerequisite failing closed;
- missing, malformed, or non-terminal capability reports failing closed;
- unsupported groups without the limitation being rejected;
- a stale limitation with all optional groups passing being rejected;
- the standalone browser functionality validator still rejecting every false
  capability;
- physical bundle qualification accepting only the new bounded combination;
- report generation retaining unsupported groups and limitations verbatim.

After focused tests pass, verification will run the complete daily-use unit
target in the pinned development container, rebuild the initramfs twice and
compare hashes, exercise the applicable QEMU gate, and run the same physical
profile on the same board.

Only qualified physical runs may enter the baseline set.
Performance optimization and A/B claims remain blocked until at least three
qualified current-main baseline runs have complete sampler coverage.

## Evidence Boundary

The diagnostic runs used to establish this design are not baseline samples.
They contain setup and capability timing only.
The current physical evidence records:

- successful graphical preflight and a stable Firefox process;
- successful Marionette session and local navigation;
- passing local/session storage, cookie, IndexedDB, and canvas checks;
- failing WebAssembly, Web Worker, audio, and `fetch` checks;
- automatic recovery to the U-Boot prompt.

No speedup or kernel bottleneck claim follows from this evidence.
