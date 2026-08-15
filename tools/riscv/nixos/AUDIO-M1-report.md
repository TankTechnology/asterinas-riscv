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

## Next steps (AUDIO-M2+)

- **Capture (RX queue)**: implement `read()` + `virtio_snd_pcm_xfer` RX framing
  (1 out + 2 in SGs), verify against an input-capable backend.
- **Period/streaming model**: pre-allocate a ring of TX messages and keep them in
  flight (Linux's `virtsnd_pcm_msg_send`), instead of one-shot synchronous sends.
- **Event queue**: `VIRTIO_SND_EVT_PCM_PERIOD_ELAPSED`/`XRUN` for latency/XRUN
  reporting.
- **ALSA**: a `snd-virtio`-style user-space library or minimal ALSA stack over the
  node (out of scope here by design).
- Minor: silence the `transport` field dead-code warning (keep-for-lifetime, like
  the block/console devices).
