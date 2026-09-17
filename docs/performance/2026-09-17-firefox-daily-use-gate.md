# Firefox daily-use gate host qualification, 2026-09-17

## Scope and status

This record qualifies the Firefox daily-use gate's host-side contract,
orchestration, adapters, Stage1 packaging, and regression checks.
It is not a live QEMU run, a physical-board run, or a speedup result.
No Firefox or kernel speedup is claimed here.

The installed Stage1 entry point is
`/run/asterinas-tools/browser-daily-use-gate`.
It requires stable Firefox and Xorg PIDs and an absolute private evidence
directory; supply an explicit local fixture index URL for every operator run:

```bash
/run/asterinas-tools/browser-daily-use-gate \
  --firefox-pid "$FIREFOX_PID" \
  --xorg-pid "$XORG_PID" \
  --fixture-index-url http://10.0.2.2:17894/browser-quality/index.html \
  --evidence-dir /run/asterinas-browser-daily-use-smoke \
  --mode smoke
```

`smoke` defaults to a 30-second timeout and `profile` to 120 seconds.
Without `--mode`, the gate selects `smoke` normally and `profile` when
`--physical` is present.
The physical-board form uses
`http://10.100.19.216:17894/browser-quality/index.html` and adds `--physical`.
The flag records physical sampler provenance but does not establish physical
HDMI evidence or override an explicitly supplied `--mode smoke`.

## Contract and evidence boundary

The command prints exactly one terminal verdict:

```text
ASTERINAS_BROWSER_DAILY_USE_PASS functions=7/7 slow=<count> evidence_dir=<absolute-path>
ASTERINAS_BROWSER_DAILY_USE_FAIL reason=<canonical-reason>
```

PASS is standard output; FAIL is standard error and exits nonzero.
On success, the evidence directory contains the six capture artifacts
`browser-fixture-capture.json`, `browser-local-capture.json`,
`browser-context-switch.json`, `browser-composite-capture.json`,
`browser-system-time.json`, and `browser-thread-time.json`, then
`browser-daily-use-result.json`.
On failure, the gate makes a best-effort attempt to retract partially published
output and to publish only `browser-daily-use-checkpoint.json`.
Neither retraction nor checkpoint persistence is guaranteed after a storage
failure, so failure evidence is invalid: discard that directory and use a new
one for the next run.

The directory is a one-run reservation, not a workspace to reuse.
Exclusive/no-follow mode-0600 writes and fsync prevent replacement.
Existing artifacts, checkpoints, reservation files, or private staging paths
fail closed; the reservation and staging path remain after completion, and a
fresh empty directory is required for every retry.

The orchestrator makes one `WebDriver:NewSession` request and requires the
initial handle set to contain exactly one selected original window.
Pre-existing extra windows fail closed before the workload begins.
Its phase-facing wrapper forbids additional `WebDriver:NewSession`,
`WebDriver:DeleteSession`, and `Marionette:Quit` requests.
It closes its protocol transport without deleting the session, returns to the
original window after closing only windows not present in the captured baseline
(the gate-created context windows), and checks unchanged Firefox/Xorg PID and
start-time identities.
It does not restart either process, reboot the guest, rewrite partition 2, or
change the boot menu.
The exercised browser can still update its profile and leave the validated
download under `/home/asterinas/Downloads`; use the separate Stage1
`--volatile-home` handoff or a disposable image when those writes must not
persist.

## What is measured

The seven functional groups are `document`, `storage`, `execution`,
`rendering-media`, `navigation`, `download`, and `contexts`.
The five performance categories are `startup`, `input`, `scroll`,
`navigation`, and `context-switch`.

Input keyboard/pointer and scroll first/next-rAF p95 values greater than
100 ms are `slow`.
Navigation becomes `slow` above 2 s response-to-DOM, and a context switch is
`slow` above 500 ms for any component operation or the complete operation.
`slow` is diagnostic only: functional groups determine PASS/FAIL.
Startup records the one persisted guest-monotonic `BOOT_FIREFOX_EXEC` endpoint
through the one persisted `BOOT_FIRST_WINDOW_READY` endpoint for the selected
Firefox PID.
The two positive endpoints must appear in that strict order; current gate
session timing is not a substitute.
It is not a cold-start or restart measurement.

Browser `performance.now()` input/scroll values, guest-monotonic startup,
local-command, context, and sampler values, and browser Navigation Timing are
separate clocks and are never subtracted from one another.
A negative browser `fetchStart` is retained with `fetchStartValid=false`, not
clamped into a positive time.
Physical HDMI scanout is unsupported; synthetic events do not measure USB,
Xorg, framebuffer, or scanout delay; and public network pages are excluded.
The procfs system artifact may retain minor/major-fault deltas, but those
counters are not a daily-use performance category or kernel causal result.
The `kernel-diagnostics-unavailable` limitation is therefore a placeholder,
not a zero-fault claim.

## Implementation and host evidence

The TDD slices were committed in order:

- `3d1f16f06` Define Firefox daily-use evidence contract.
- `c86c55d59`, `6240182f8`, and `925da6be4` hardened the contract and retained
  negative navigation timing evidence.
- `faaee9993` added the bounded one-session orchestrator; `f9e456bec` and
  `49ab14b99` corrected failure publication and deterministic early-sampler
  coverage.
- `aea43df35` reused the fixture, timing, composite, and sampler adapters.
- `494c18d30` and `1695a2c02` repaired cleanup after Marionette command
  timeouts, including commands that were not sent.
- `ed665be58` packaged the command and contract into Stage1 and extended the
  fast host check.

The committed implementation plan specifies red tests before each contract,
orchestrator, adapter, and packaging slice.
The later hardening commits above are the review-driven fixes to failure
publication, sampler determinism, negative navigation timing, and timeout
cleanup.
Fresh on 2026-09-17, `tools/riscv/firefox_fast_check.sh` ran 354 tests in
22.504 seconds with `OK`, then completed its Python compilation, shell syntax,
and diff checks with `FIREFOX_FAST_CHECK_PASS`.
This is the current host-only count, not a copied count from an earlier record.

## Required next gates

1. Run the cached four-hart QEMU smoke gate with this Stage1 command.
2. Run one physical `profile` with `--physical`, then optionally a separately
   classified public page.
3. Use those results to choose between a renderer/IPC-wakeup investigation and
   a serialized resource-path investigation.

Only a controlled before/after run of the same qualified workload can support
a speedup claim.
