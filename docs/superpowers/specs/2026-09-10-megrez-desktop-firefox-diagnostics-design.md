# Megrez one-command desktop and Firefox diagnostics design

## Outcome

Provide one configured command for starting the existing Asterinas Debian
desktop from immutable MMC artifacts, and one separate bounded command that
identifies the first missing boundary in the physical Firefox
`WebDriver:NewSession` path.  The startup command leaves a ready desktop
running.  The diagnostic command retains private evidence and returns the
board to a fresh U-Boot epoch.

This design does not rebuild or reinstall the 2-GiB Debian root, transfer boot
artifacts during a routine run, modify partition 2, broaden network support,
or claim a Firefox fix before a causal experiment passes.  The former
repository-local `aster-code-review` workflow remains deleted; normal diff
review, focused tests, QEMU, and physical gates provide review evidence.

## Current evidence and problem boundary

The existing immutable deployment already boots Debian 13.6 with systemd,
the 1920-by-1080 firmware framebuffer, Xorg `fbdev`, Openbox, both physical USB
input devices, and an active Firefox ESR service with zero restarts.  Three
unattended physical boot cycles passed.  Their measured readiness times were
175.223, 220.509, and 209.402 seconds.

The most complete physical interaction attempt reached all of those readiness
conditions and then failed in cycle 1 after printing only:

```text
ASTERINAS_PHYSICAL_SETUP cycle=1 phase=WebDriver:NewSession state=start
ASTERINAS_PHYSICAL_GRAPHICS_FAIL cycle=1 reason=Marionette gate deadline expired
```

It recovered to a fresh U-Boot prompt.  No physical interaction cycle or HDMI
acceptance was published.  The equivalent RISC-V QEMU interaction gate has
completed three nonce-bound keyboard, pointer, click, DOM, guest screenshot,
and framebuffer cycles after the System V shared-memory lifetime repair.

The physical result therefore proves neither that Firefox is deadlocked nor
that it is merely slow.  Earlier measured QEMU `NewSession` calls took from
169 to 419 seconds depending on instrumentation and cache state.  The physical
workflow gives Firefox a fresh tmpfs home and profile because partition-2
writeback is not supported, so every boot is a cold-profile startup.  This is
a material performance difference, but it is not yet a demonstrated root
cause.

Protocol-v6 evidence narrows the observer error further.  Firefox kept one PID
with zero restarts, started its Marionette listener, completed the protocol
greeting, completely received `WebDriver:Status`, and returned a complete
662-byte error response.  Firefox ESR 140 has no `WebDriver:Status` entry in
its Marionette command table; Mozilla's upstream discussion explicitly says
that WebDriver HTTP status is not implemented in Marionette itself.  The
former positive control was therefore invalid and prevented the selected
`WebDriver:NewSession` command from ever running.  This is not evidence of an
Asterinas network or Firefox correctness failure.

## Chosen architecture

### Configured desktop bundle

Add a small host module, `tools/riscv/megrez_desktop.py`, with an immutable,
strictly validated configuration bundle.  The bundle records:

- the schema version;
- the stable serial device path;
- the schema-2 physical graphics plan path and SHA-256;
- the RockOS deployment attestation and measurement-log paths plus hashes;
- the three versioned MMC artifact names;
- the default private evidence root.

`configure` reads and validates every input, rejects duplicate JSON keys and
unsafe artifact names, and atomically replaces
`target/megrez-desktop/current.json` with mode `0600`.  It performs no serial
I/O and no deployment.  A routine command consumes only this bundle and the
already attested MMC artifacts.

The startup tool may wait through a currently progressing OpenSBI/U-Boot
epoch and may interrupt U-Boot autoboot through the existing board-session
contract.  It must not enter credentials, boot RockOS, upload a file, write an
MMC partition, change the U-Boot environment, or silently select a different
artifact.  A state that cannot be normalized without one of those actions is
reported as a concise maintenance requirement.

### `start` lifecycle

`python3 -m tools.riscv.megrez_desktop start` validates the configured bundle
and attestation, opens the serial device, verifies all three MMC byte counts
and CRC32 values through U-Boot, boots Asterinas, and applies the existing
isolated graphical startup steps.  It requires systemd, `/dev/fb0`, Xorg
holding the framebuffer, Openbox, and the original active Firefox service
with zero restarts.

On success it publishes an atomic private result containing artifact and plan
identities, boot arguments, readiness state, a bounded serial-log hash, and
phase timings.  It then closes the host serial descriptor and leaves the
desktop running.  It does not arm the current 900-second diagnostic reboot
timer.  If startup fails after the guest begins, it attempts the existing
bounded software recovery.  Firmware or SBI loss of all serial progress is
reported as `manual-reset-required`; software cannot recover such a failure
without an independent board reset controller.

The initial implementation does not promise a lower guest boot time.  It
removes repeated operator work and measures where the current 175-to-220-second
readiness interval is spent.  Later performance changes require separate
causal evidence.

### `diagnose-firefox` lifecycle

`python3 -m tools.riscv.megrez_desktop diagnose-firefox` uses the same
configured inputs but remains a bounded acceptance experiment.  It preserves
the current kernel recovery timer, starts one clean desktop, runs one Firefox
diagnostic sequence, captures evidence, requests an immediate reboot, and
requires a fresh U-Boot epoch.  It never waits for physical keyboard, mouse,
or HDMI input.

The diagnostic sequence fixes the kernel, root image, Firefox binary, tmpfs
profile policy, screen geometry, and all deadlines.  It performs these steps:

1. Record the Firefox PID, start identity, service restart count, profile
   identity, X socket, and framebuffer.
2. Capture the bounded pre-command Firefox-tree snapshot.
3. Connect once to the loopback Marionette endpoint, verify its greeting, and
   issue exactly one `WebDriver:NewSession` with the physical gate's existing
   parameters and 300-second absolute setup deadline.  Connection readiness is
   part of this selected command rather than a separate synthetic gate.
4. Capture one bounded Firefox-tree snapshot while the selected command remains
   outstanding and one after response or timeout.  Collection runs outside the
   selected command's critical path, for three snapshots in total.
5. Export the bounded kernel ring-buffer interval, process-lifecycle records,
   Marionette transport records, process/thread syscall snapshots, selected
   fd metadata, and service/Firefox logs before printing the terminal result.
6. Recheck the Firefox PID identity and restart count, then recover the board.

The existing transport format already records request ID, command, send
completion, response-header bytes, expected response-body bytes, received
body bytes, exception type, errno, and guest monotonic time without recording
the payload, page URL, script, or nonce.  The diagnostic mode enables that
format and uses the existing bounded `firefox-diagnostic-snapshot` collector.
For the one fixed offline `NewSession` request, it also enables the already
implemented Marionette error record so a complete semantic rejection cannot
force another physical boot merely to reveal its reason.  The serial and
publication byte caps remain authoritative.

### Classification and stopping rules

The result classifier reports the earliest supported boundary, rather than a
generic timeout:

- `listener-not-ready`: no complete Marionette greeting before the selected
  command deadline;
- `new-session-not-sent`: the selected command is not fully written;
- `new-session-response-absent`: send completes but no response header byte
  arrives;
- `new-session-response-partial`: a bounded header or body begins but does not
  complete;
- `new-session-rejected`: a complete, correctly identified response contains
  a Marionette error;
- `new-session-complete`: a valid response and session ID arrive;
- `evidence-incomplete`: required identity, snapshot, log, or recovery evidence
  is missing.

Retained protocol-v6 data is classified separately as
`status-command-rejected`; this historical label explains the invalid
precondition but is not part of the protocol-v7 live sequence.  Protocol v7
removes the Status phase and its timeout from the runtime identity rather than
silently treating an unsupported command as success.

The obsolete `status_complete` field is removed from boundary evidence.  This
changes the canonical result shape, so new Firefox diagnostic results use
schema version 2; previously published schema-version-1 evidence remains
immutable and is consumed only through retained transcript replay.

A timeout is never converted into success and the 300-second selected-command
deadline is not extended.  Missing lifecycle records after a reported kernel
log budget exhaustion are classified as missing evidence, not as proof that an
event did not occur.  Guest ticks and host monotonic timestamps remain separate
clock domains.

No kernel or Firefox semantic change is permitted from the diagnostic run
alone.  If the snapshots identify one persistent outstanding syscall, the
next change is a Linux-referenced minimal regression for that syscall's exact
wait/wakeup pattern.  If the transport and thread sequence keep advancing but
exceed the deadline, the next work item is a separately measured cold-profile
startup optimization.  If `NewSession` completes, the existing physical
keyboard, pointer, DOM, screenshot, HDMI, and recovery gate becomes the next
acceptance step.

## Data and evidence handling

Both actions create a new mode-`0700` evidence directory and refuse to reuse a
nonempty run directory.  Raw serial, process, syscall, and kernel-log evidence
is mode `0600`.  `result.json` is atomically replaced last and binds the hashes
of every retained input and evidence file.  Environment contents, credentials,
Marionette request and successful-response payloads, page contents, and
arbitrary user buffers are not captured.  The bounded error object from the
one fixed offline NewSession request is the only response-content exception.

Failure after guest start still attempts bounded evidence collection and
recovery.  Evidence collection has its own byte and time limits and cannot
extend the selected Firefox deadline.  An oversized or malformed diagnostic
frame fails closed while preserving the earlier raw serial transcript.
Protocol-v6 generated a complete 43,068-byte diagnostic frame, but the
60-second host phase expired while its serial bytes were still arriving and
recovery input corrupted the frame.  Protocol v7 uses a measured 90-second
diagnostic host budget; it does not widen the 300-second selected-command
deadline or the 900-second total experiment budget.

## Investigation cost budget

The diagnostic workflow exists to prevent another sequence of low-information
Firefox reruns.  Runtime cost and experimental count are part of the result,
not informal operator notes.  Every diagnostic result records total host
seconds, guest-selected-command seconds, QEMU runs, physical boots, artifact
bytes transferred, the tested hypothesis, the expected contrary outcome, and
the earliest classified boundary.

An experiment is admitted only when its manifest states one hypothesis and one
observation that would reject it.  Changing a deadline, adding unrelated
instrumentation, replacing the root image, and modifying a kernel semantic in
the same experiment is forbidden.  The following upper bounds apply:

- retained transcript replay and host unit tests: two minutes;
- one Linux or Asterinas kernel microtest boot: 90 seconds;
- one selected-command QEMU Firefox experiment: 15 host minutes;
- one physical Firefox diagnostic boot including evidence and recovery: 15
  host minutes, followed by an independently bounded recovery wait;
- artifact transfer during an unchanged-identity experiment: zero bytes.

Only one live run with the same kernel, root, Firefox payload, profile policy,
command sequence, and diagnostic flags is allowed.  A repeat is admitted only
when the preceding evidence was incomplete because of a demonstrated tooling
defect, that defect has a failing regression, and the repaired tool passes the
regression before the repeat.  Waiting longer is a changed experiment and
requires an explicit performance hypothesis; it is not a retry.

If a live Firefox run does not classify a first missing boundary, work stops at
the diagnostic layer.  The next action must improve and test evidence capture
against retained data; another Firefox boot is forbidden.  If a boundary is
classified, the next experiment is the smallest Linux-referenced reproducer
for that boundary.  No production fix is attempted until that reproducer
fails on Asterinas and passes on Linux, or until source and runtime evidence
prove that the defect is Firefox-specific rather than a Linux ABI violation.

After two causally distinct, well-formed micro-hypotheses are rejected without
narrowing the boundary, the investigation pauses for an architecture review
of the observer and experiment contract.  It does not proceed by adding a
third broad trace or another unchanged browser run.

## Test and verification strategy

All implementation changes use test-first development and the persistent
development container without dependency installation or network access.

Host unit tests cover strict bundle parsing, atomic configuration publication,
path and hash validation, unsafe-state refusal, boot-argument sanitization,
start success/failure lifecycles, diagnostic recovery, phase timing, output
permissions, and exact result schemas.  Real local sockets cover every
Marionette classification, including partial headers, partial bodies, EOF,
invalid response identity, and a response arriving at the deadline boundary.

Guest/source integration tests require the diagnostic environment to be
enabled only for `diagnose-firefox`, preserve the normal Firefox launcher, and
prove that snapshot collection is bounded and outside the selected command's
deadline.  Existing physical-graphics, boot-stability, Firefox diagnostic,
Stage1, and probe tests must continue to pass.

Retained-data regressions must prove that protocol-v6's complete Status error
is `status-command-rejected`, that protocol v7 emits no `WebDriver:Status`, and
that one successful greeting may directly precede NewSession.  Socket tests
must cover a complete NewSession error separately from absent and partial
responses.  The combined shell command remains within the existing 768-byte
limit.

The runtime sequence is cost-gated and simulation-first:

1. Implement and run the diagnostic classifier against synthetic records and
   the retained `mmc-graphics-final-19` physical transcript.  No guest boots.
2. Run focused native/unit tests and all applicable sub-minute microtests.
3. Run at most one QEMU control using the exact current kernel and root inputs.
4. Review the classification before authorizing physical execution.
5. Stage only a changed versioned kernel or Stage1 through RockOS, if their
   identities changed; never reinstall partition 2.
6. Run at most one physical `diagnose-firefox` experiment for the recorded
   identity and hypothesis.
7. Implement a kernel fix only after a Linux/Asterinas microtest isolates the
   violated contract.
8. Run the physical interaction and HDMI gate on the repaired exact release.

## Acceptance criteria

The operator-complexity milestone passes when a configured immutable
deployment can be started with one command, reaches the existing graphical
readiness contract, publishes phase timings, and remains running without a
RockOS boot, build, upload, partition-2 write, or U-Boot environment change.

The diagnostic milestone passes when one command produces a complete,
hash-bound classification of the first missing `NewSession` boundary and
returns the board to a fresh U-Boot epoch.  It does not require the Firefox bug
to be fixed.  It also requires the run-count and time budgets above to be
present and satisfied; an accurate classification reached through repeated
unchanged Firefox boots does not pass this milestone.

The Firefox milestone passes only when the exact current release completes the
existing three physical nonce interactions, renders the expected page,
retains a fresh HDMI capture, keeps the same Firefox process with zero service
restarts, and recovers as specified.  A QEMU pass, an active Firefox process,
or a longer timeout cannot substitute for that physical result.

## Network-stack handoff after the local Firefox milestone

The local Firefox milestone deliberately boots with
`asterinas-desktop-m5-network.service` masked and
`ASTERINAS_BROWSER_WEB_BASIC_ONLY=1`.  Passing it proves the browser process,
local X11 graphics, loopback Marionette transport, DOM execution, framebuffer,
and input path without making network behavior a confounding variable.

After that pass, the separately developed network stack can be merged without
changing the local acceptance workload.  Online browsing is admitted in
layers: first NIC/link and address assignment, then route and DNS, then HTTP to
a controlled endpoint, then HTTPS with correct wall time, CA trust, SNI, and
certificate verification, and finally an interactive public page.  Each layer
must retain the preceding offline Firefox gate.

Adding a network stack is therefore the correct next subsystem, but it does
not by itself prove normal browsing.  Firefox also depends on socket readiness,
nonblocking connect, `poll`/`epoll`, DNS, TCP close/error behavior, system time,
the Debian CA store, and TLS.  The staged gates distinguish those failures
without reopening graphics, process, or Marionette diagnosis.  External
network success is never allowed to replace the deterministic local Firefox
acceptance result.
