# POLISH-M6 — ALSA writei playback works (`aplay` → virtio-sound)

Date: 2026-08-15
Branch: `track/nixos`
Status: **`aplay -D hw:0,0 /sine.wav` plays a 440 Hz tone through virtio-sound
end-to-end.** The unmodified Alpine riscv64-musl `aplay` opens `/dev/snd/pcmC0D0p`,
negotiates params, and streams frames through the ALSA ioctl ABI into the
virtio-sound TX queue; the host WAV backend receives a verifiable tone.

---

## 1. What was blocking (the AUDIO-M2 "API stall")

M5 designed the `/dev/snd` device model and concluded the only real work was the
kernel-side ALSA ioctl ABI. That ABI was implemented (WIP in
`kernel/src/device/snd/` + `misc/sound.rs`), but the first `aplay` boot failed
before any sound came out:

```
aplay: main:850: audio open error: Not a tty
__ALSA_EXIT=1__
__ALSA_DONE__ __ALSA_FAIL__
```

The cause was one missing ioctl — **`SNDRV_PCM_IOCTL_SYNC_PTR` (0xc0884123)** —
returning `ENOTTY`:

```
WARN: PCM ioctl 0xc0884123
WARN: unknown ALSA PCM ioctl command 0xc0884123
```

### 1.1 Why the M5 design doc was wrong about mmap

`docs/porting/snd-device-model.md` §4.1 assumed *"libasound falls back to
`snd_pcm_writei` when `MMAP`/`STATUS_EXT` ioctls return `ENOTTY`"*. That is only
true of the **data** path. The `hw` plugin **always** mmaps the status/control
pages and issues `SYNC_PTR` during `snd_pcm_open` (even in writei mode), to push
its `appl_ptr` and pull back `state`/`hw_ptr`. There is no writei path that
skips it. So `SYNC_PTR` is mandatory for *any* ALSA client, mmap or not.

## 2. Fixes landed this session

### 2.1 `SNDRV_PCM_IOCTL_SYNC_PTR` (the open blocker)

`kernel/src/device/snd/pcm.rs`:
- Transcribed the three `asound.h` structs byte-for-byte from
  `/usr/riscv64-linux-gnu/include/sound/asound.h` (LP64 layout):
  - `snd_pcm_mmap_status64` — 56 B (`state`, `hw_ptr`, `tstamp`, `suspended_state`, `audio_tstamp`)
  - `snd_pcm_mmap_control64` — 16 B (`appl_ptr`, `avail_min`)
  - `snd_pcm_sync_ptr64` — 136 B (`flags` + 64 B status union + 64 B control union; the two
    *separate* unions are what make it 136 B, not 128 B — the size is what `ioc!`
    matches against)
- Added the `SyncPtr` ioctl def (`ioc!(…, b'A', 0x23, InOutData<SndPcmSyncPtr>)`).
- `PcmStream::build_mmap_status()` reports the tracked `state` + `hw_ptr`.

`kernel/src/device/misc/sound.rs` dispatch:
```rust
cmd @ SyncPtr => {
    let mut sync_ptr = cmd.read()?;
    sync_ptr.status = self.pcm.lock().build_mmap_status();
    cmd.write(&sync_ptr)?;
    Ok(0)
}
```
Playback is fully synchronous (hw_ptr advances in `writei`), so the app's
`appl_ptr` is informational and need not be stored.

### 2.2 auto-start on the first `writei` (the second blocker)

`aplay` calls `snd_pcm_prepare` then `snd_pcm_writei` and **never** issues
`START`. Linux's `snd_pcm_lib_write1` auto-starts a PREPARED stream on the first
write; our `WRITEI_FRAMES` path did not, so the first `writei` submitted TX
frames to a prepared-but-not-started stream and blocked forever on
`pop_used`. Fix in `SoundPcmFile::writei`:

```rust
if self.pcm.lock().params().is_some() {
    self.start_device()?;   // idempotent; PREPARE already ran
}
```

### 2.3 build environment: `cargo-osdk` binary baked the wrong repo path

`cargo osdk build` was failing with
`package collision in the lockfile: align_ext … asterinas-riscv-drm/ostd/… vs
asterinas-riscv-nixos/ostd/…`. Root cause: the generated base crate hardcodes
`ostd`/`frame-allocator`/`heap-allocator` paths from `env!("CARGO_MANIFEST_DIR")`
(baked at compile time of the `cargo-osdk` binary, only when `OSDK_LOCAL_DEV=1`),
and `~/.cargo/bin/cargo-osdk` pointed at a binary built in the sibling
`asterinas-riscv-drm` repo (a parallel DRM-M3 task had reinstalled it). Fix:
repointed the symlink to the nixos-built binary and deleted the stale base
crates. See memory `osdk-binary-bakes-repo-path`.

## 3. Verification (QEMU, `aplay -D hw:0,0 /sine.wav`)

Observed full ioctl trace (the complete writei path):
```
CTL: CARD_INFO, PVERSION, PCM_PREFER_SUBDEVICE
PCM open:  INFO, PVERSION, USER_PVERSION, TTSTAMP, SYNC_PTR, INFO
HW:        HW_REFINE ×7, HW_PARAMS
SW/start:  SYNC_PTR, SW_PARAMS, PREPARE, SYNC_PTR, SW_PARAMS
Data:      WRITEI_FRAMES …
```

Result:
```
Playing WAVE '/sine.wav'
__ALSA_EXIT=0__
__ALSA_DONE__ __ALSA_PASS__
fmt          : 2 ch, 48000 Hz, 16-bit, 48128 frames
amplitude    : RMS=11568.7  peak=16383 (min RMS 2000)
pitch        : 438.8 Hz (expect 440 ± 12)
audible tone : OK
=== ALSA: PASS (smp=1) ===
```

The host WAV backend received the tone, and `verify_tone` confirms amplitude
(RMS 11568.7, peak 16383) and pitch (438.8 Hz, within ±12 Hz of 440). This is
the same verification bar as AUDIO-M1's raw-PCM `出声` test, now driven through
the full ALSA ioctl ABI by an unmodified musl `aplay`.

## 4. Files / commits

Kernel (the ABI layer + driver parameterization):
- `kernel/src/device/snd/` — `mod.rs`, `pcm.rs`, `control.rs` (new).
- `kernel/src/device/misc/sound.rs` — PCM/control char nodes + ioctl dispatch.
- `kernel/src/device/mod.rs` — `mod snd;`.
- `kernel/comps/virtio/src/device/sound/device.rs` — parameterize
  `SET_PARAMS`; add `prepare`/`start`/`stop`/`write_bytes`/`write_frames`/
  `submit` for the ALSA path.

Test harness:
- `tools/riscv/nixos/audio/{build_alsa.sh, boot_alsa.py, init_alsa.c, alsa-gate.sh}`.

## 5. Known gaps / next steps

1. **Minor dead-code warnings** (unused ABI constants `SNDRV_PCM_STATE_XRUN`,
   `…_DRAINING`, `SNDRV_PCM_STREAM_CAPTURE`; `PcmParams::{rate,format,period_frames}`
   unread; `PcmStream::state()` getter unused) — these are the capture/drain
   states reserved for later milestones, not bugs.
2. **AUDIO-M3 — mmap + streaming**: real `MMAP`/`STATUS_EXT`/`SYNC_PTR` appl_ptr
   tracking + a DMA ring + IRQ-driven period completion (JACK/PipeWire-class).
3. **AUDIO-M4 — control/mixer**: `ELEM_LIST`/`ELEM_INFO`/`ELEM_READ` on
   `controlC0` (volume), wiring virtio-sound channel/format families.
4. **RX capture** (the RX queue is still dormant).
