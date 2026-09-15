# Human-Friendly Physical Input Gate Design

## Decision

The human operator confirms and types one fresh four-digit decimal code per
cycle. Keep the existing 180-second interaction bound; do not extend it to
five minutes. A host-side start barrier after graphical preflight asks the
operator to reply with that same code before Firefox page setup begins. The
guest READY marker, emitted only after the page is loaded and focused, starts
the 180-second input budget. The host repeats the code at READY.

## Evidence contract

The code is generated with `secrets.randbelow(10000)`, formatted with leading
zeroes, and distinct across a three-cycle run. The host, guest witness, and
Stage1-carried HTML all require exactly four decimal digits. The existing
SHA-256 nonce identity, ordered DOM stage reports, browser PID/restart checks,
real evdev keyboard/pointer records, Firefox trusted input/click attributes,
and cyan screenshot checks remain mandatory. The input box is automatically
focused; a user may also click it before typing. Raw evdev may contain more
than one well-formed mouse click, while the page must still report exactly one
trusted click on the amber button. An unmatched mouse button release remains
an error. No browser-side input synthesis is allowed after READY.

## Host start barrier and failure behavior

The CLI adds an opt-in `--operator-start` mode for human physical testing.
After graphical readiness, it prints the four-digit code and waits at most
180 seconds for that exact line on host stdin. The Codex operator relays the
human's chat reply to the host process; no reply or a wrong reply fails closed
and enters the existing board recovery path. The barrier is disabled for
automated tests and QEMU. The existing operator cyan attestation uses the same
four-digit code; it is recorded only after the human explicitly confirms the
monitor is cyan. No partition-2 root rewrite is needed: rebuild and stage
only the deterministic Stage1 payload.

## Verification

Unit tests cover four-digit validation, distinct generation, wrong/no start
confirmation, 180-second bounds, extra well-formed input-box clicks, and
rejection of unmatched releases. Run the Firefox fast check, Stage1
determinism checks, three-cycle QEMU gate, then one synchronized physical
cycle with real keyboard/mouse evidence and fresh U-Boot recovery. Do not
integrate into remote main until that physical cycle passes.
