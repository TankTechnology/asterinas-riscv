# AUDIO-M1 — virtio-sound from zero to audible PCM (issue #26)

Date: 2026-08-15
Branch: `track/nixos`
Status: **PASS** — guest writes 192 000 bytes of sine-wave PCM; host QEMU `wav`
backend receives exactly 192 000 bytes (ratio 1.000).

## Goal

Audio was the last core subsystem with zero code. This milestone delivers the
first audible path:

1. a **virtio-sound** device driver (`kernel/comps/virtio/src/device/sound/`),
2. a minimal user-space path — a char device node opened directly (no ALSA),
3. QEMU verification with a sine-wave PCM burst and a host-side byte-count check.

## What was implemented

### 1. virtio-sound spec + driver (`kernel/comps/virtio/src/device/sound/`)

- `config.rs` — the 3-field config space (`jacks`/`streams`/`chmaps`) and the
  `VIRTIO_SND_F_CTLS` feature bit (cleared on negotiation — we don't drive
  control elements yet).
- `mod.rs` — wire types (`virtio_snd_hdr`, `virtio_snd_query_info`,
  `virtio_snd_pcm_info`, `virtio_snd_pcm_set_params`, `virtio_snd_pcm_hdr`,
  `virtio_snd_pcm_xfer`, `virtio_snd_pcm_status`), request/status codes, and the
  global device registry (mirrors the entropy registry).
- `device.rs` — the `SoundDevice`:
  - control queue (idx 0): `PCM_INFO` → `SET_PARAMS` → `PREPARE` → `START`;
  - TX queue (idx 2): PCM playback;
  - RX (capture) and event queues are deliberately left dormant for this MVP.

The device is wired into the existing virtio dispatch: `VirtioDeviceType::Sound = 25`
in `device/mod.rs`, and `init`/`negotiate_features` arms in `lib.rs`.

### 2. User-space path (`kernel/src/device/misc/sound.rs`)

A char device `/dev/snd/pcmC0D0p` (misc major 10, minor 116) whose `write()` hands
PCM frames straight to `SoundDevice::play()`. `read()` returns `EOPNOTSUPP`
(capture is not implemented yet). The device is registered only when a
virtio-sound device was discovered at boot.

### 3. QEMU harness (`tools/riscv/nixos/audio/`)

- `init.c` — static riscv64 `/init` that opens the node and writes 1 s of 440 Hz
  S16LE/48 kHz/stereo sine (192 000 bytes).
- `build_audio.sh` — packs it into a cpio.gz initramfs.
- `boot_audio.py` — boots the existing U-Boot/`booti` flow with
  `-audiodev wav,… -device virtio-sound-device,audiodev=…`, then checks the host
  WAV file byte count against the guest's reported write.
- `audio-gate.sh` — one-command gate (build initramfs → re-pack boot disk → boot).

## Design decisions

- **Synchronous, polling TX** (like the console's transmit path): each `play()`
  fills `[xfer header][PCM data]` (device-readable) plus `[status]` (device-writable)
  in one DMA page, submits the 2-out + 1-in message, then polls the used ring until
  the status is written. No IRQ handler is registered for the control/TX queues;
  this is the simplest correct path and matches `ConsoleDevice::send_diagnostic`.
- **Playback stream discovery**: `PCM_INFO` is queried at `init()` and the first
  `VIRTIO_SND_D_OUTPUT` stream is selected. QEMU pre-populates stream info at
  `realize()`, so `PCM_INFO` is valid before any `PREPARE`.
- **TX message layout** follows Linux exactly: playback sends 2 out SGs
  (`virtio_snd_pcm_xfer` + data) and 1 in SG (`virtio_snd_pcm_status`).

## Gotchas found (and worth remembering)

1. **Control request codes increment by 1, not 0x10.** `VIRTIO_SND_R_PCM_*` are
   `0x0100, 0x0101, 0x0102, 0x0103, 0x0104, 0x0105` (SET_PARAMS = 0x0101, not
   0x0110). An initial 0x0110 guess made QEMU reject every command with
   `virtio snd header not recognized: 272` → `S_BAD_MSG`.
2. **The `wav` backend resamples to 44100 Hz by default.** The first run wrote
   192 000 bytes but the file held only 176 400 (44100 frames). Adding
   `-audiodev wav,…,out.frequency=48000` pins the backend to the stream rate and
   makes the byte count match exactly.
3. **`wav` is output-only.** QEMU logs `Can not open 'virtio-sound.in' (no host
   audio driver)` for the capture stream — non-fatal, expected for a playback-only
   MVP. Capture verification will need an input-capable backend (e.g. `pa`/`alsa`
   or a `wav`-like input source).
4. **QEMU's `wav` backend leaves the RIFF and `data` chunk sizes at 0.** It
   streams and never finalizes them on teardown, so Python's `wave` (3.14)
   rejects the file (`not a WAVE file`) and some players truncate it. The harness
   rewrites those two size fields (`_riff_header_fix`) into a `.playable.wav`
   copy so the output is actually playable; byte-count checks are unaffected.

## Verification

```
[AUDIO] open: OK  __AUDIO_open_OK__
[AUDIO] wrote 192000 bytes __AUDIO_WRITE_BYTES=192000__
[AUDIO] write: OK  __AUDIO_write_OK__
__AUDIO_DONE__ __AUDIO_PASS__

=== AUDIO-M1 host-side verification ===
  guest wrote : 192000 bytes
  wav file    : 192044 bytes (192000 bytes PCM after 44-byte header)
  received/written ratio: 1.000
  host received: OK
=== AUDIO-M1: PASS (smp=1) ===
```

Run: `bash tools/riscv/nixos/audio/audio-gate.sh [--rebuild-kernel]`.

## 出声 (audible-tone) verification

Byte count alone only proves bytes *left* the guest — a stream of 192 000 zero
bytes would pass. To prove *sound actually came out*, `boot_audio.py` now runs a
second pass (`verify_tone`) that decodes the host WAV and asserts both amplitude
and pitch:

```
=== AUDIO-M1 audible-tone verification ===
  fmt          : 2 ch, 48000 Hz, 16-bit, 48000 frames
  amplitude    : RMS=11584.1  peak=16383 (min RMS 2000)
  pitch        : 439.5 Hz (expect 440 ± 12)
  playable copy: target/nixos/audio/audio-out.wav.playable.wav
  audible tone : OK
```

- **Amplitude** — RMS 11584 / peak 16383, matching the guest's `16383*sin(...)`
  synthesis (0.5 full-scale). The `min RMS 2000` floor rejects silence/zero-fill.
- **Pitch** — dominant frequency estimated via zero crossings (deterministic for
  a pure tone): 439.5 Hz against an expected 440 ± 12 Hz.
- **Playable copy** — `_riff_header_fix` fills in the left-at-zero chunk sizes so
  the WAV opens in `ffprobe`/`aplay`/`ffplay` (`ffprobe` reports
  `pcm_s16le, 48000 Hz, 2 ch, 1.000 s`). `--play` additionally plays it on the
  host via `aplay`/`paplay`.

The gate now requires `audible tone: OK` (not just byte count) to report PASS.

### SMP note

`smp=4` boots but hangs during the first virtio-block read (the known SMP=4
issue — see `nixos-ltp-status`), unrelated to audio. Verification uses `smp=1`.

## Next steps (AUDIO-M2+)

- **Capture (RX queue)**: implement `read()` + `virtio_snd_pcm_xfer` RX framing
  (1 out + 2 in SGs), verify against an input-capable backend.
- **Period/streaming model**: pre-allocate a ring of TX messages and keep them in
  flight (Linux's `virtsnd_pcm_msg_send`), instead of one-shot synchronous sends.
- **Event queue**: `VIRTIO_SND_EVT_PCM_PERIOD_ELAPSED`/`XRUN` for latency/XRUN
  reporting.
- **ALSA**: a `snd-virtio`-style user-space library or minimal ALSA stack over the
  node (out of scope here by design).

## CI follow-up (post-landing)

The initial PR #35 CI was red on `make check`/`make docs` for two reasons, both
now fixed:

1. **My code** — `cargo fmt --check` flagged the `sound/device.rs` import
   ordering and line wrapping, and `make docs` (`cargo doc -D warnings`) reported
   the `transport` field as never read. Fixed by re-running `cargo fmt` and
   marking the keep-for-lifetime `transport` field `#[allow(dead_code)]`
   (commits `a894ccc94` on `track/nixos`, `8b048b72e` on `virtio-sound-driver`).
2. **Pre-existing on `main`** — the `nightly-2026-07-21` toolchain bump
   reformats several files merged under an earlier toolchain (pty, fanotify,
   resolver, keyctl, seccomp, syscall mod), and `fanotify.rs` had an unresolved
   `FsEventPublisher` intra-doc link. Fixed in a separate chore PR (#36,
   branch `chore/fmt-nightly`) so the sound PR stays focused.
3. **Still red, still pre-existing on `main`** (not fixed by #35/#36, tolerated
   by the maintainer — e.g. #34 merged with identical failures):
   - `check-license-lines` — eight `tools/riscv/{systemd,xorg}` build scripts
     lack the MPL-2.0 SPDX header. Fixed in chore PR **#37**
     (`chore/license-headers`).
   - `basic-test (lint)` / clippy — new lints (`is_multiple_of`, field order,
     unnecessary cast, collapsible if) on the loongarch64 target, in code my PRs
     don't touch.
   - `basic-test (ktest)` / `integration-test` (release) — `ostd` inline-asm
     `RISC-V Image header must be exactly 64 bytes` (the known `--release` +
     `riscv_sv39_mode` issue, see `riscv-boot-build-config`).
