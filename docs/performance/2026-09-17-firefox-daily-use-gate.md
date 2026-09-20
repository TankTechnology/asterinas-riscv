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
The performance profile requires `document`, `storage`, `navigation`,
`download`, and `contexts` to pass.
The `execution` and `rendering-media` groups record optional capability
coverage and may be `pass` or `unsupported`, but never `fail`, in a passing
performance result.
`execution` is derived from WebAssembly, Web Worker, and `fetch`;
`rendering-media` is derived from canvas and audio; and `storage` still
requires local storage, session storage, cookies, and IndexedDB.
A terminal, exact capability report with a false optional check produces
`fixture-capability-unavailable` and the mandatory limitation
`fixture-capabilities-incomplete`.
The limitation is invalid when both optional groups pass.
Missing, malformed, non-terminal, or inconsistent reports remain fatal.

This qualification is intentionally narrower than the standalone browser Web
functionality gate.
That gate is unchanged and continues to require every capability check to be
true.
A passing performance result with `execution=unsupported` does not claim that
WebAssembly, workers, or `fetch` work; `rendering-media=unsupported` likewise
does not claim audio support.
The terminal `functions=7/7` field counts seven closed-schema verdicts, not
seven feature passes.
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
- `004ccc6ab` defined required and optional daily-use qualification;
  `820715a78` classified terminal fixture capability evidence without changing
  the standalone Web gate.
- `9e3e53a91` preserved unsupported states in failure uploads; `42d99792a`
  reused the guest qualification rule on the physical host; and `f547a5d8f`
  retained exact capability coverage in three-run reports.

The committed implementation plan specifies red tests before each contract,
orchestrator, adapter, and packaging slice.
The later hardening commits above are the review-driven fixes to failure
publication, sampler determinism, negative navigation timing, and timeout
cleanup.
Fresh on 2026-09-17, `tools/riscv/firefox_fast_check.sh` ran 354 tests in
22.504 seconds with `OK`, then completed its Python compilation, shell syntax,
and diff checks with `FIREFOX_FAST_CHECK_PASS`.
After the 2026-09-18 qualification amendment, the complete physical daily-use
unit target ran 399 tests in 34.165 seconds with `OK` and three Node-dependent
tests skipped.
The host `tools/riscv/firefox_fast_check.sh` then ran 483 tests in 28.590
seconds with `OK`, the same three skips, and completed Python compilation,
shell syntax, and diff checks with `FIREFOX_FAST_CHECK_PASS`.
These are host and contract checks, not physical performance samples.

## Required next gates after the 2026-09-18 contract amendment

1. Rebuild Stage1 twice and prove that both archives have the same digest.
2. Run the cached four-hart QEMU graphics/control gate with the rebuilt
   Stage1.
3. Run three fresh one-profile-per-boot physical samples on the same Megrez
   board and admit only samples with stable identities, complete samplers,
   matching capability coverage, successful upload, and automatic recovery.
4. Use the three-run mechanism classification to select one kernel variable
   for a separately reviewed before/after experiment.

Only a controlled before/after run of the same qualified workload can support
a speedup claim.

### Status, 2026-09-20

| Gate | Status | Evidence |
| --- | --- | --- |
| 1. Stage1 build determinism | **passed** | Three pairs, each byte-identical within its pair and each pair carrying a distinct digest: `stage1-a`/`stage1-b` at `816f8c4d`, `stage1-crossarch-a`/`stage1-crossarch-b` at `9bcf5f7b` (the digest the physical plans reference), and `stage1-qualification-a`/`stage1-qualification-b` at `0d4ebedf` (the digest the QEMU gate used). |
| 2. QEMU graphics/control gate | **passed** | `qemu-qualification`, `qemu-qualification-current`, `qemu-smoke-fixed5`, and `qemu-namespace` each record `passed=true`, `reason=pass`, `physical=false`, and three interaction cycles. `qemu-qualification` ran with the rebuilt Stage1 `0d4ebedf`; `qemu-smoke-fixed4` retains the earlier `QEMU interaction cycle 1 exited before READY` failure. |
| 3. Three physical samples | **passed** | `release-jit-crossarch-run-3/4/5`, all qualified and recovered, one profile per boot, published as [the 2026-09-18 baseline](../../performance/2026-09-18-firefox-daily-use-physical-baseline.md). |
| 4. Select one kernel variable | **applied, result not yet admitted** | Classification was `runnable-delayed` 3/3, so `53c4601a6` was selected as the one variable. Its A/B is recorded in [the 2026-09-19 result](../../performance/2026-09-19-firefox-wake-balance-physical-ab.md) as a directional improvement, not a speedup; the remaining gap is one RISC-V kernel-test run. |

The QEMU graphics/control gate and the RISC-V kernel-test suite are different
gates. The gate above boots the desktop and drives three interaction cycles; it
does not execute the new `select_cpu` `#[ktest]` regressions, which only the
kernel-test suite runs. That kernel-test run is the last open item behind gate 4.

Reading this gate's evidence requires care: the gate creates its output
directories root-owned mode `0700` inside the development container, so an
unprivileged host-side listing cannot read them. Read them from inside the
container or as root; an unreadable directory is not an empty one.
