# `/dev/snd` device model — ALSA ABI over virtio-sound

Status: design proposal (2026-08-15). Not yet implemented.

## 1. Goal

Turn the current "raw PCM device node" (`/dev/snd/pcmC0D0p`, `write()` of raw
frames) into a **real ALSA card** (`hw:0,0`) so unmodified userspace ALSA
clients — `aplay`, `speaker-test`, `amixer`, `mpv`/`ffmpeg` via the writei
fallback — run against Asterinas. The deliverable is a **reusable `/dev/snd`
device model** in the kernel that speaks the ALSA **ioctl ABI** and drives the
existing virtio-sound transport.

Key conclusion (see `tools/riscv/nixos/POLISH-M5-report.md`): the userspace half
is *free* — Alpine ships prebuilt riscv64-musl `alsa-lib` + `alsa-utils`, no
cross-compilation needed. The entire real work is the kernel-side ABI layer
described here.

## 2. Current state

| piece | file | today |
|---|---|---|
| virtio-sound driver | `kernel/comps/virtio/src/device/sound/device.rs` | hardcoded `SET_PARAMS` (S16LE / 48 kHz / 2ch / 8192 B buffer / 2048 B period); `prepare_playback()` does SET_PARAMS→PREPARE→START once; `play()` streams frames synchronously |
| char node | `kernel/src/device/misc/sound.rs` | `/dev/snd/pcmC0D0p` (misc major 10, minor 116); `write_at()` hands bytes to `SoundDevice::play()`; no `ioctl` |
| stream caps | `StreamInfo` in `device.rs` | `formats`/`rates`/`channels_min`/`channels_max` already read from `PCM_INFO` but unused |

The driver already discovers real stream capabilities (`query_streams` →
`R_PCM_INFO`); it just throws them away and hardcodes S16/48k/2ch.

## 3. The device model

Asterinas has no ALSA "subsystem" to write a backend for, so the ALSA ABI must
live **on the sound device node**. The model mirrors Linux's `sound/core` split
without importing its complexity:

```
/dev/snd/pcmC0D0p   — playback stream  (ioctl = ALSA PCM ABI, magic 'A')
/dev/snd/controlC0  — control/mixer    (ioctl = ALSA control ABI, magic 'U')
```

Both are registered as misc char devices (major 10). `pcmC0D0p` already exists;
`controlC0` is a new, initially-empty node (many clients probe it and bail on
`ENOENT`/`ENOTTY` if absent). A third node `/dev/snd/timer` is **not** needed for
playback (`snd_pcm_writei` does not use the timer API).

### 3.1 Layering

```
userspace (aplay/speaker-test)          stock musl riscv64 binaries
   │  ALSA ioctl ABI (asound.h structs)
   ▼
kernel/src/device/snd/                   NEW — reusable /dev/snd layer
   ├─ pcm.rs      snd_pcm_* structs + PCM ioctl dispatch (magic 'A')
   ├─ control.rs  snd_ctl_* structs + control ioctl dispatch (magic 'U')
   └─ mod.rs      node registration, minor numbering (116=pcmC0D0p, 0=controlC0)
   │  driver trait: get caps / set_params / prepare / start / stop / writei
   ▼
kernel/comps/virtio/device/sound/        EXISTING — parameterized, not rewritten
```

The `/dev/snd` layer depends on a small **driver trait** (`SoundBackend`), so a
future non-virtio sound source (e.g. `hd-audio`) plugs in without touching the
ABI layer. This is the "Option A vs B" question from the M4 report resolved in
favour of a clean split: the ABI lives in a reusable layer, the virtio-sound
driver just implements the trait.

```rust
trait SoundBackend {
    fn streams(&self) -> &[StreamInfo];               // caps from PCM_INFO
    fn set_params(&self, s: u32, p: &PcmParams) -> Result<()>;  // parameterized
    fn prepare(&self, s: u32) -> Result<()>;
    fn start(&self, s: u32) -> Result<()>;
    fn stop(&self, s: u32) -> Result<()>;              // DROP/DRAIN
    fn writei(&self, s: u32, buf: &[u8]) -> Result<usize>;   // frame-aligned write
}
```

## 4. The ALSA PCM ioctl ABI (minimal playback set)

Structs are copied verbatim from `include/uapi/sound/asound.h` (fixed-size,
reserved padding — the ABI is stable and versioned by `SNDRV_PCM_VERSION
(0x10f)`). The ioctl numbers below are the canonical `asound.h` values (magic
`'A'` = 0x41 for PCM, `'U'` = 0x55 for control).

| ioctl | enc | data | needed for `aplay`? | notes |
|---|---|---|---|---|
| `PVERSION` (0x00) | `_IOR` | `int` | yes | protocol handshake; return `0x010f` |
| `INFO` (0x01) | `_IOR` | `snd_pcm_info` | yes | card/device/subdevice/stream fields |
| `HW_REFINE` (0x10) | `_IOWR` | `snd_pcm_hw_params` | yes | constrain to device caps |
| `HW_PARAMS` (0x11) | `_IOWR` | `snd_pcm_hw_params` | yes | **this is what feeds `set_params`** |
| `HW_FREE` (0x12) | `_IO` | — | yes | reset |
| `SW_PARAMS` (0x13) | `_IOWR` | `snd_pcm_sw_params` | yes | start/stop threshold; can be no-op-accept |
| `STATUS` (0x20) | `_IOR` | `snd_pcm_status` | yes | state/RUNNING, hw_ptr |
| `PREPARE` (0x40) | `_IO` | — | yes | driver `prepare` |
| `START` (0x42) | `_IO` | — | yes | driver `start` |
| `DROP` (0x43) | `_IO` | — | yes | driver `stop` (abort) |
| `DRAIN` (0x44) | `_IO` | — | yes | driver `stop` (flush) |
| `WRITEI_FRAMES` (0x50) | `_IOW` | `snd_xferi` | yes | `snd_pcm_writei` path — the core |
| `WRITEN_FRAMES` (0x52) | `_IOW` | `snd_xfern` | nice-to-have | `snd_pcm_writen` (interleaved multi-channel, non-atomic) |
| `CHANNEL_INFO` (0x32) | `_IOR` | `snd_pcm_channel_info` | optional | `snd_pcm_channel_info()` probes |
| `DELAY` (0x21) | `_IOR` | `snd_pcm_sframes_t` | optional | latency query |

`WRITEI_FRAMES` (`snd_xferi` = `{ result: snd_pcm_sframes_t, buf: *const void,
frames: snd_pcm_uframes_t }`) is the whole playback data path: copy `frames`
interleaved frames from the user pointer, convert to bytes, `SoundBackend::writei`.
This is exactly what the current `write()` does, re-framed as an ioctl.

### 4.1 mmap access model (later phase)

`libasound` prefers `snd_pcm_mmap_*` (via `MMAP`/`SYNC_PTR`/`STATUS_EXT`) for
low latency, but **falls back to `snd_pcm_writei`** when `MMAP`/`STATUS_EXT`
ioctls return `ENOTTY`. So the writei-only first cut unlocks `aplay`,
`speaker-test`, `ffmpeg`, `mpv` (fallback path). mmap (`STATUS_EXT` + a
DMA-mapped ring) is a later AUDIO-M3 phase for JACK/PipeWire-class clients.

## 5. Driver change: parameterize `SET_PARAMS`

The only *driver* edit is making the hardcoded `VirtioSndPcmSetParams` fields
come from the negotiated `snd_pcm_hw_params` (via the `SoundBackend::set_params`
trait method):

| field | today | becomes |
|---|---|---|
| `format` | `FMT_S16` | `snd_pcm_format` → virtio-sound `FMT_S16/S24_3LE/S32/…` (start S16LE only) |
| `rate` | `RATE_48000` | `snd_pcm_hw_params` rate (validate against `StreamInfo.rates`) |
| `channels` | `2` | `snd_pcm_hw_params` channels (validate `channels_min..=max`) |
| `buffer_bytes`/`period_bytes` | const 8192/2048 | derive from period/buffer frames × frame size |

The `prepare_playback()` once-only guard is replaced by explicit `prepare`/`start`
calls driven by the ioctls, so a client can re-negotiate params between streams.

## 6. Asterinas ioctl machinery (what to reuse)

The `FileOps::ioctl(&self, path, raw_ioctl)` hook already exists on every file;
the sound file adds one. The existing `ioc!` macro + `dispatch_ioctl!` (used by
`kernel/src/device/fb.rs`, `evdev/file.rs`) express the ABI declaratively:

```rust
use ioctl_defs::*;   // generated from asound.h constants
dispatch_ioctl!(match raw_ioctl {
    cmd @ PcmPversion => { cmd.write(&(SNDRV_PCM_VERSION as i32))?; Ok(0) }
    cmd @ PcmInfo      => { cmd.write(&self.build_pcm_info())?; Ok(0) }
    cmd @ PcmHwParams  => { let p = cmd.read()?; self.apply_hw_params(&p)?; cmd.write(&p)?; Ok(0) }
    cmd @ PcmWritei     => { let x = cmd.read()?; self.writei(&x)?; Ok(0) }
    // PREPARE/START/DROP/DRAIN are _IO (no data): match on the command directly
    _ => return_errno_with_message!(Errno::ENOTTY, "unknown sound ioctl"),
});
```

The `ioc!` macro handles both legacy (`_IOR('A', 0x00, int)`-style raw command)
and modern (magic+nr+data) encodings, so the `asound.h` constants map directly.
Struct layouts are `#[repr(C)]` `Pod` types (`snd_pcm_info`, `snd_pcm_hw_params`,
`…`) — the same pattern as `FbVarScreenInfo`/`FbFixScreenInfo` in `fb.rs`.

One caveat: `snd_pcm_hw_params` is a *large* variable-ish struct (~612 B on
riscv64, dominated by the 120-element `intervals` array). The pointer-based
ioctl path (`SafePtr<_, CurrentUserSpace>`) already used for framebuffer cmaps is
the template for copying it in/out without stack bloat.

## 7. Userspace (verified)

Alpine v3.22 ships riscv64-musl builds — **no cross-compile needed**:

| package | provides | size |
|---|---|---|
| `musl` | `/lib/ld-musl-riscv64.so.1` + `libc.musl-riscv64.so.1` | 613 KB |
| `alsa-lib` 1.2.14 | `libasound.so.2` (932 KB) + `/usr/share/alsa/` config (178 KB) | 1.1 MB |
| `alsa-utils` 1.2.14 | `aplay` (63 KB), `speaker-test` (30 KB), `amixer` (47 KB) | 140 KB |

Total ~1.8 MB added to the initramfs (currently 2.5 MB cpio.gz) — negligible.
The LTP initramfs already ships the musl loader+libc (as `ld-musl-riscv64.so.1`),
so only the soname-name reconciliation (`libc.musl-riscv64.so.1`) and the two
alsa packages need adding. `aplay -D hw:0,0` then exercises the ioctl set in §4.

## 8. Phasing

1. **AUDIO-M2 — writei playback.** Parameterize `SET_PARAMS`; add `kernel/src/device/snd/`
   with the §4 minimal ioctl set on `pcmC0D0p`; a no-op `controlC0`. Verify
   `aplay`/`speaker-test -c2 -fS16_LE -r48000` in QEMU (reuse `boot_audio.py`
   `verify_tone` on the host WAV backend).
2. **AUDIO-M3 — mmap + streaming.** `STATUS_EXT`/`SYNC_PTR`/`MMAP`, DMA ring,
   IRQ-driven period completion instead of sync poll. Unlocks JACK/PipeWire.
3. **AUDIO-M4 — control/mixer.** Real `ELEM_LIST`/`ELEM_INFO`/`ELEM_READ` on
   `controlC0` (volume), wiring `VIRTIO_SND_R_PCM_SET_PARAMS` channel/format
   families.

## 9. Risks / open questions

- **`snd_pcm_hw_params` interval negotiation** is fiddly (`HW_REFINE` vs
  `HW_PARAMS`; `interval` vs `mask`). Mitigation: accept any single-valued
  params in the device's supported set, reject the rest with `EINVAL`; let
  `HW_REFINE` be a pass-through that clamps to caps.
- **Synchronous `play()` blocks** the calling thread on `pop_used`; a real
  streaming client expects non-blocking/period-driven behaviour. Acceptable for
  AUDIO-M2, addressed in AUDIO-M3.
- **`snd_pcm_status` `hw_ptr`/`state`** must be plausibly correct or some clients
  spin/underrun. Track a monotonic `hw_ptr` in `writei` for the first cut.
- **No `TSTAMP`/`HWSYNC`/`LINK`** — return `ENOTTY`; libasound tolerates this.
