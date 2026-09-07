# Current-main Megrez Physical Graphics Interaction Design

## Goal

Establish a current-`origin/main`, cold-booted Milk-V Megrez acceptance path
that proves one real keyboard interaction and one real pointer interaction
traverse the complete Asterinas graphics/input stack and visibly change a
deterministic Firefox page.

The accepted path is:

```text
PBMT/framebuffer mapping -> fbdev mmap -> Xorg/Openbox/Firefox
physical USB keyboard/mouse -> dual xHCI IRQ -> HID -> evdev/poll -> Xorg -> Firefox
```

## Scope

This milestone will:

- transplant the nine commits between the debug-console branch's merge base
  and `d77b05f58` onto the current fork `origin/main`;
- retain the opt-in, ephemeral root console and its automatic-recovery
  boundary;
- add a deterministic local Firefox interaction page;
- observe, but never synthesize, the physical keyboard and pointer actions;
- correlate raw evdev activity with the resulting browser DOM state;
- publish fail-closed serial and JSON evidence with immutable artifact hashes;
- preserve a supplied HDMI capture as part of the evidence set;
- repeat a bounded interaction cycle before accepting the run;
- return the board to a fresh U-Boot prompt automatically.

This milestone will not merge an open DRM, timer, signal, or scheduler PR. It
will not claim GPU acceleration, hotplug, arbitrary HID report support,
display-mode changes, or general desktop usability.

## Selected approach

The guest uses three independent witnesses during the operator window:

1. A root-owned evdev observer records keyboard key-downs, relative pointer
   motion, and left-button press/release pairs from `/dev/input/event*`.
2. A deterministic local Firefox page records trusted keyboard, input,
   pointer-move, and click events. The page renders the typed nonce, toggles a
   high-contrast button, and increments a visible click counter.
3. A read-only Marionette observer polls the page state and captures a PNG
   after the expected state is reached. Marionette is not allowed to dispatch
   keyboard or pointer events during the physical acceptance window.

The host accepts a cycle only when the raw evdev witness and the browser
witness both pass for the same nonce and cycle number. This is stronger than
a manual visual check and avoids the false claim that an `xdotool`-generated
event proves the physical xHCI/HID path.

Two alternatives were rejected:

- A manual screenshot alone cannot prove the source of the input events.
- A fully automated `xdotool` or Marionette click is reproducible but bypasses
  the physical USB input path that this milestone is intended to validate.

## Branch and source identity

Implementation occurs in an isolated worktree on branch
`codex/megrez-physical-graphics-current-main`, created from fork
`origin/main` commit `69a7b6e41ca74932f79d917f3638199da573b1e9`.

The transplant source is the exact ordered range after merge base
`374a42092145f143c4dcd0613c909e0d4b2d2800` through
`d77b05f58470877a8fca78594e3e936352d4ddea`. The resulting branch records the
new commit identities and the source-to-destination mapping in its evidence
manifest.

## Guest interaction page

The page is installed at:

```text
/usr/share/asterinas/physical-graphics/index.html
```

It contains:

- an instruction area that displays the cycle number and expected nonce;
- one focused text input;
- one large high-contrast button;
- visible counters for trusted key, pointer-motion, and click events;
- an immutable JSON-shaped state exposed to the observer.

The page accepts only a lowercase hexadecimal nonce of the exact length
provided by the guest gate. A successful click is accepted only after the
input value matches the expected nonce. Each accepted click toggles between
two explicitly named color states and increments the cycle-local click
counter. Reloading the page clears prior state so stale interactions cannot
satisfy a later cycle.

## Guest input witness

The evdev observer opens all current `/dev/input/event*` nodes read-only and
classifies event streams by observed event types instead of relying on event
node numbering. For each cycle it requires:

- at least as many keyboard key-down records as nonce characters;
- at least one non-zero `REL_X` or `REL_Y` pointer-motion record;
- one `BTN_LEFT` down record followed by one `BTN_LEFT` up record;
- no read error, device disappearance, or counter overflow.

Serial-console input cannot satisfy these requirements because it does not
produce evdev records. The observer reports counts and a digest, never the
individual key sequence.

## Guest browser witness

The Marionette observer navigates Firefox to the local page before the
operator-ready marker and then restricts itself to read-only scripts and a
screenshot command. It requires:

- the exact local file URL and completed document state;
- the exact nonce in both the input and rendered output;
- trusted keyboard/input, pointer-motion, and click flags;
- the requested cycle number and exactly one accepted click;
- the expected post-click color-state name;
- a structurally valid PNG with the expected dimensions.

The observer emits one canonical result record and the screenshot SHA-256.
It fails if Firefox restarts or its Marionette port changes during a cycle.

## Host orchestration and evidence

The host-side physical gate reuses the debug root-console protocol and the
existing bounded Megrez boot transaction. It:

1. validates and hashes the kernel, DTB, Stage1 initramfs, root image, root
   manifest, package lock, and package checksums;
2. cold-boots the board with the firmware framebuffer and both USB hosts;
3. waits for the root console, graphical target, Xorg, Openbox, and Firefox;
4. generates a random lowercase hexadecimal nonce for each cycle;
5. starts the guest observer and prints an operator instruction containing
   the nonce;
6. waits for the exact guest pass/fail record;
7. repeats the cycle three times within one bounded boot;
8. imports one operator- or capture-card-produced HDMI PNG/JPEG after the
   final pass and records its SHA-256;
9. drains the complete serial log, rejects fatal markers, and waits for a
   fresh automatic U-Boot prompt;
10. atomically publishes `result.json`, the serial transcript, browser PNGs,
    the HDMI capture, and an artifact hash manifest.

An HDMI file is mandatory for a passing physical result. The gate copies it
from an explicitly supplied regular file only after all guest cycles pass;
symlinks, hard-link aliases to outputs, unsupported formats, empty files, and
files larger than 64 MiB are rejected.

## Acceptance contract

A passing result requires all of the following in one run:

- exact latest-main source identity and transplanted debug-console identity;
- firmware framebuffer registration at 1920x1080, stride 7680,
  `x8r8g8b8`;
- both Megrez xHCI controllers started without startup or transfer failure;
- physical USB keyboard and mouse registered through evdev;
- Xorg using fbdev, with Openbox and Firefox alive;
- three complete keyboard-nonce plus mouse-move/click cycles;
- matching evdev, DOM, screenshot, and cycle records for every cycle;
- one retained HDMI image with a recorded digest;
- no panic, oops, fatal exception, OOM kill, xHCI fatal error, or framebuffer
  corruption marker anywhere in the drained transcript;
- an observed fresh U-Boot prompt after the Asterinas recovery timer fires.

Any missing, duplicated, reordered, mismatched, or stale marker produces a
failing result. Partial graphical success is retained as diagnostic evidence
but is never published as `passed: true`.

## QEMU-first verification

Before the physical run, the current-main branch must pass:

- all debug-console unit and protocol tests;
- all affected rootfs, browser-web, framebuffer, and board-session tests;
- the debug-root-console QEMU gate;
- the existing browser-web QEMU gate and non-blank framebuffer capture;
- a QEMU-only interaction contract test that drives the same guest witnesses
  with QEMU USB input injection and verifies three complete cycles.

The QEMU interaction run proves automation and regression coverage. It does
not satisfy the physical milestone.

## Failure handling

The host always drains the transcript and preserves diagnostics. A guest
interaction failure remains a failure even if the board later recovers. A
recovery failure remains a failure even when all interactions passed.

If the current-main QEMU gates regress after the transplant, the transplant
is repaired before physical execution. If only the physical interaction gate
fails, the evidence is used to isolate the lowest failing boundary:

- framebuffer registration or pixels: PBMT/framebuffer/fbdev workstream;
- missing raw USB records: xHCI/HID/evdev workstream;
- raw records present but no X11 event: evdev/poll/Xorg workstream;
- X11 event present but browser state absent: browser/userspace workstream;
- delayed or stalled cycles: scheduler/timer/RCU A/B investigation.

Only the isolated root cause is proposed as a separate kernel PR.

## Completion evidence

The implementation is complete only when the branch contains the code and
tests, all QEMU gates pass on the current-main kernel, and one physical result
directory contains a passing canonical `result.json`, complete serial log,
three browser screenshots, one HDMI image, and hashes for every frozen input
and output. Service-active markers or device-node enumeration alone are not
sufficient.
