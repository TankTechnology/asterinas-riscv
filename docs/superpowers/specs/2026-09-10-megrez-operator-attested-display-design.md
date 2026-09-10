# Megrez operator-attested display acceptance design

## Goal

Complete the current-main physical graphics interaction acceptance when no HDMI
capture device is available. Preserve the machine-verified real USB input,
Firefox DOM, guest screenshot, terminal identity, and recovery requirements,
while replacing only the external HDMI image with an explicitly weaker live
operator confirmation.

This mode must never describe a guest screenshot as HDMI evidence. It must not
change or rebuild the deployed kernel, initramfs, root filesystem, DTB, or MMC
contents.

## Alternatives

The selected approach is a first-class operator-attested mode in the existing
host gate. Letting the existing HDMI mode time out would skip terminal checks
and publish a failed result. Waiting for capture hardware would preserve the
strongest evidence but would prevent the current experiment from proceeding.

External HDMI capture remains the preferred release-grade mode. The new mode is
appropriate only for the fork's explicitly experimental development acceptance.

## Command contract

The CLI requires exactly one display-evidence option:

- `--hdmi-capture ABSOLUTE_PATH` retains the existing image-backed behavior.
- `--operator-display-attestation` selects the new confirmation behavior.

The Makefile preparation target accepts the same mutually exclusive choice and
prints a fully explicit command. Both modes retain the existing phase deadlines,
900-second guest recovery timer, MMC-only artifact selection, and private output
directory.

The CLI also accepts `--cycles {1,3}` and defaults to the existing release-grade
three-cycle contract. The current experimental operator-attested run explicitly
uses `--cycles 1`. A one-cycle result proves one complete interaction path but
does not claim repeated stability; three distinct nonces remain required for the
stronger release-grade statement.

## Lifecycle

Both modes boot the same frozen artifacts and require graphical readiness with
exactly two physical USB input devices, one keyboard, one relative mouse,
framebuffer, Xorg fbdev, Openbox, a stable Firefox service PID, and zero browser
restarts. They then execute the requested one or three nonce-bound interaction
cycles. Every cycle requires physical evdev key presses, relative mouse
movement, a complete left click, trusted Firefox DOM events, the cyan state,
and a hash-bound guest PNG transferred over serial.

After the final requested cycle:

- HDMI mode waits for a new, stable external PNG or JPEG as before.
- Operator mode prints one bounded prompt asking the operator to confirm that
  the physical monitor shows the cyan final-cycle PASS page. The exact response
  is `confirm-cyan-pass <suffix>`, where `<suffix>` is the last eight characters
  of the final interaction nonce. The host waits only for the configured display
  timeout and accepts exactly one newline-terminated response.

After either display evidence succeeds, the gate rechecks the Firefox PID,
restart count, profile, and final DOM nonce, emits the completion marker, waits
for the fixed guest reboot, and requires a fresh U-Boot epoch. Confirmation
failure, invalid input, EOF, timeout, terminal drift, or recovery loss fails
closed and retains bounded evidence.

## Evidence model

`PhysicalGraphicsResult` becomes schema version 2, records
`cycles_requested` as exactly 1 or 3, and carries exactly one of:

- `hdmi`: the existing `FileEvidence`, with `operator_display=null`; or
- `operator_display`: an immutable object with kind `operator-attested`, final
  nonce SHA-256, state `cyan-cycle-3-pass`, and `confirmed=true`, with `hdmi`
  set to null.

An external image pass keeps reason `physical-graphics-pass`. An operator pass
uses reason `physical-graphics-operator-attested-pass`. The publisher writes
`operator-display-attestation.json` only in operator mode, includes its digest in
`sha256sums.txt`, and never creates `hdmi-evidence.png` or
`hdmi-evidence.jpg` for that mode. The result and evidence report must state that
no external HDMI image exists.

## Implementation boundaries

The guest page, physical interaction witness, root image, and kernel remain
unchanged. Only the host orchestration, result model, CLI/Makefile selection,
tests, and operator documentation change. The confirmation reader is injected
into the real adapter for unit tests and uses a monotonic absolute deadline in
production. It reads no passwords, credentials, or arbitrary commands.

## Verification

Tests must prove CLI mutual exclusion, cycle-count validation, exact confirmation
syntax and nonce binding, deadline/EOF/duplicate rejection, one- and three-cycle
lifecycle ordering, terminal-state and recovery preservation, result schema and
pass invariants, private publication, hash coverage, absence of fake HDMI files,
and unchanged external-capture behavior. The complete physical graphics,
desktop, stability, and probe suites run in the existing persistent container
without downloads.

The physical run is admitted only after all host tests pass. During that run the
agent relays each printed nonce to the operator immediately, waits for the
operator's explicit visual confirmation before sending the final confirmation
line to the host process, and never repeats a completed physical identity merely
to improve presentation.
