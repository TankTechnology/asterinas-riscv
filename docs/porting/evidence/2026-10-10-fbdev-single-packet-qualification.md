# fbdev and single-packet qualification (2026-10-10)

This evidence index records the implementation and the gates that were
actually run.  DRM, GPU, Firefox, and network stress are outside this goal.

## Implemented contracts

- `tools/riscv/megrez_single_packet_fixture.py` accepts one nonce-bound UDP
  datagram, echoes it once, and rejects a duplicate or wrong peer.
- `tools/riscv/megrez_single_packet_gate.py` requires one guest marker, one
  fixture request, one fixture response, matching payload SHA-256, and zero
  TX/RX errors.
- `tools/riscv/megrez_single_packet_qemu_gate.py` starts that fixture beside
  QEMU and publishes a bounded result without the existing 20-request HTTP
  workload.
- `tools/riscv/debian/rootfs/gmac_single_packet_evidence.py` is installed by
  the `desktop-m5-network` rootfs profile as a conditional systemd service.
  It sends one datagram and waits for one matching echo; it never retries.
- `tools/riscv/debian/rootfs/desktop_m4_framebuffer_rw.py` performs one 16x16
  BgrReserved write/read/restore transaction and verifies the saved and
  restored hashes match.
- `tools/riscv/megrez_display_gate.py` is an incomplete physical runner. It
  requires a passed QEMU fbdev receipt and an explicitly confirmed fresh
  U-Boot prompt before arming the watchdog or selecting the desktop entry;
  `--allow-pattern-only` is an explicit lower-claim mode for the deterministic
  fb0 scanout canary and never asserts application painting.
- `tools/riscv/megrez_single_packet_physical_gate.py` is the corresponding
  one-shot UDP runner.  It requires the passed QEMU single-packet receipt and
  a separate `asterinas-single-packet` rootfs installation receipt before it
  can arm the watchdog or load Asterinas. Neither physical runner is yet
  qualified for execution: serial reopen/root attestation and failure-path
  orchestration still need integration tests.

## QEMU single-packet gate: passed

Receipt: `target/qemu-single-packet-20261010/result.json`  
Fixture: `target/qemu-single-packet-20261010/single-packet-fixture.json`  
Serial: `target/qemu-single-packet-20261010/single-packet.serial.log`

The receipt records:

- `passed=true`, `status=pass`;
- `tx=1`, `rx=1`, `fixture_request_count=1`,
  `fixture_response_count=1`;
- payload length 56 and SHA-256
  `30073dffe6941e02be45a77ca4f7be5b1fbe0239b6f02a221200ccc631b4d023`;
- final derived root image SHA-256
  `b5d6c5d86bd9405e81e697ab1f7d1b51bf07e0e7cfd84af1da24c91f7b4408f9`;
- kernel SHA-256
  `0525e34578f9dc6933d8a409e76150aa76bc8db7f88178a90e424a36b2a49f31` and
  U-Boot SHA-256
  `738775f52dcfad98dfae4b851a6dc3118db30bbe6455c7df3573a84133db8a5d`.

The focused implementation tests passed:

```text
python3 -m unittest \
  tools.riscv.tests.test_megrez_single_packet_evidence \
  tools.riscv.tests.test_megrez_single_packet_gate \
  tools.riscv.tests.test_megrez_single_packet_qemu_gate \
  tools.riscv.tests.test_megrez_single_packet_physical_gate \
  tools.riscv.tests.test_megrez_display_gate \
  tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe -q
Ran 32 tests ... OK
```

## QEMU fbdev gates

### Deterministic fbdev scanout canary: passed

Receipt: `target/qemu-desktop-m4-fbdev-canary-20261010/result.json`  
Serial: `target/qemu-desktop-m4-fbdev-canary-20261010/desktop-m4-fbdev-canary.serial.log`  
Scanout: `target/qemu-desktop-m4-fbdev-canary-20261010/desktop-m4-fbdev-canary.ppm`

The canary uses the same Xorg/session and `/dev/fb0` handoff as M4, then writes
three full-width BGR bands for a bounded interval and restores every saved
framebuffer byte.  Its receipt records `passed=true`, three sampled host
scanout colors, `874496` non-background pixels, the ordered M4 session markers,
and one equal saved/restored framebuffer hash.  The boot argument is placed
before Asterinas' `-- --root-init=systemd` delimiter so it cannot be parsed as
an init argument.  This is a fbdev-to-scanout qualification only.

### Normal desktop application paint: blocked at host scanout

The retained strict framebuffer result is the diagnostic record in
`docs/porting/evidence/2026-10-08-qemu-desktop-m4-render-debug.md`.  The guest
can read and write `/dev/fb0`, and a direct fixed-color mapping reaches the
QEMU scanout, but the normal Xorg client frame remains effectively uniform.
The fresh normal M4 run with the repaired QEMU Xorg configuration still ends
with `PPM framebuffer contains a single color`.  The strict application-paint
gate therefore remains failed.  The canary is deliberately marked
`qualification=fbdev-pattern-only` and `application_paint_verified=false`; it
cannot authorize a physical application-paint claim.  A trial that disabled
GLX/DRI in the M4 command did not reach the desktop protocol and was reverted;
it is not counted as a fix.

### X11-to-fb0 boundary diagnostic: passed once

Receipt: `target/qemu-desktop-m4-x11-boundary-20261010/result.json`  
Serial: `target/qemu-desktop-m4-x11-boundary-20261010/desktop-m4-x11-boundary.serial.log`  
Scanout: `target/qemu-desktop-m4-x11-boundary-20261010/desktop-m4-x11-boundary.ppm`

This was a throwaway, bounded diagnostic rootfs; it is not a replacement for
the immutable M4 application gate. After the desktop clients were ready, one
X11 client filled three 320-pixel bands and synchronised the request. For each
band, `XGetImage`, the corresponding `/dev/fb0` pixel, and the early QEMU
scanout capture agreed (`cc6622`, `22cc66`, and `6622cc`). The host capture
contained 86 sampled colors and 614350 non-background pixels. This isolates a
working client-to-Xorg-fbdev-to-guest-framebuffer-to-QEMU-scanout path for one
bounded frame. It does not yet prove that the normal wallpaper/window layout
remains visible at the later strict-gate capture point, and it does not prove
HDMI output on the physical board.

## Physical status

No physical boot was issued in this cycle.  The board was left in the known
RockOS state.  The physical display runner rejects the pattern-only receipt by
default; an operator must explicitly pass `--allow-pattern-only` to qualify a
fb0 session while retaining `application_paint_verified=false`.  Both physical
runners also require a separate partition-2 installation receipt:
`asterinas-single-packet` for the network run and `desktop-m4-framebuffer-rw`
for the display run.  Neither receipt exists for the current board image, so
neither runner was authorized.  This avoids silently testing an old image or
writing a new root filesystem without a separate installation receipt.

The physical objectives remain part of this goal. After a strict QEMU fbdev
receipt and an installed-image receipt are available, run one
watchdog-bounded fb0 desktop cycle and one independent exact-one UDP cycle.
Each must return to RockOS, close and reopen the serial owner, and produce a
fresh root nonce, UID 0, and boot-id. Without a capture device, the physical
display result can claim guest fb0/session evidence only, not visible HDMI
pixels.

### Test audit following the X11 diagnosis

The display plan now defines fixed D0-D6 layout, isolated fb0 transaction,
X11 paint, QEMU pixel comparison, normal desktop, recovery and failure-injection
cases. The existing physical tests mostly cover receipt parsing; they did not
yet prove the actual boot/recovery state machine. Task 6's completed flags have
been corrected to pending.

Both physical runners previously accepted `uid=1000` as recovery. The shared
recovery parser now rejects non-root responses, malformed boot-id, duplicate
markers and stale nonces. Display/control-chain/physical-packet focused tests
pass (28 cases). This parser proves one root response only; it is not a
replacement for actual serial close/reopen, distinct nonces and same recovered
boot-id. No board operation was performed for these tests.

The latest persistent-container run includes the framebuffer layout and
write/restore tests as well as the shared recovery parser (40 cases, passed).
The 16x16 transaction test checks every untouched byte and padded row; the
short-read case rejects the save before mmap or a write. These are host-side
helper/protocol tests with simulated device metadata, not proof of the kernel
fbdev implementation or physical display timing.

```text
tools/docker/run_dev_container.sh --workspace /mnt/shared/xaj/Program/asterinas -- \
  python3 -m unittest \
  tools.riscv.tests.test_megrez_control_chain \
  tools.riscv.tests.test_megrez_display_gate \
  tools.riscv.tests.test_megrez_single_packet_physical_gate \
  tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe \
  tools.riscv.tests.test_debian_desktop_m4_framebuffer_rw -q
Ran 40 tests ... OK
```

The current candidate menu and the prior Basic/DWC3/GMAC recovery receipts
remain under `target/megrez-physical-step1/`; they are not mixed with this
QEMU-only receipt.

## Safety boundary

Until the QEMU application-paint host-scanout gate is green, do not run
`megrez_display_gate.py` on the board.  When it is run later, use a fresh
U-Boot prompt, `--from-uboot`, the hardware watchdog, and one output directory
for the complete serial/recovery receipt.  A timeout is a failed receipt and
must end the run rather than trigger an automatic retry.
