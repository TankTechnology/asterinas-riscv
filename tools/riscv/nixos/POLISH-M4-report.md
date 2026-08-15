# POLISH-M4 — CI blockers, ALSA evaluation, LTP status

Date: 2026-08-15
Branch: `track/nixos`
Status: **CI blockers fixed** (clippy ×3 targets + riscv `--release` build) and
flowed to `main` as **PR #38**; sound-device lint fix pushed to **PR #35**;
**ALSA evaluation** delivered below (conclusion: implement the kernel-side ALSA
PCM ioctl ABI over the existing `/dev/snd/pcmC0D0p` node; alsa-lib cross-compile
is the cheap half).

---

## 1. CI blockers — fixed

The `nightly-2026-07-21` toolchain bump turned several warnings into `-D warnings`
errors and broke the RISC-V `--release` build. Two classes, both now fixed.

### 1.1 Clippy lints (all three targets)

| lint | file | fix |
|---|---|---|
| `clippy::allow_attributes` | `kernel/comps/virtio/src/device/sound/device.rs` | `#[allow(dead_code)]` → `#[expect(dead_code)]` |
| `clippy::manual_is_multiple_of` | `kernel/src/device/evdev/file.rs` | `total % EVENT_SIZE != 0` → `!total.is_multiple_of(EVENT_SIZE)` |
| `clippy::inconsistent_struct_constructor` | `kernel/src/device/fb.rs` | reorder `FbFixScreenInfo` literal to match struct field order |
| `clippy::unnecessary_cast` | `kernel/src/syscall/mod.rs` | `-(errno as i32) as usize` → `(-errno) as usize` |
| `clippy::collapsible_if` | `ostd/src/arch/riscv/boot/simple_framebuffer.rs` | let-chain collapse |
| `dead_code` (57×) | `kernel/src/syscall/mod.rs` | extend module-level `expect(dead_code)` to `x86_64`; drop now-redundant item-level `expect` in `getdents64` |

The `dead_code` batch is the interesting one: `fanotify`/`keyctl`/`seccomp`
(the "security trio", PR #33) are wired into the **generic** syscall table
(`arch/generic.rs`, numbers 217–219/262/263/277) which serves riscv64 and
loongarch64. x86_64 has its own explicit table (`arch/x86.rs`) that does not
yet import them, so on x86 those 57 items are genuinely unreachable. The
existing `#![cfg_attr(any(riscv64, loongarch64), expect(dead_code))]` covered
the two generic-table targets; extending it to `x86_64` is the minimal,
semantically-correct fix (these syscalls are *intentionally* not on x86 yet).

### 1.2 RISC-V `--release` build failure

`ostd/src/arch/riscv/boot/bsp_boot.S` guarded the 64-byte Image header with a
GAS `.if`/`.error`. Under `--release`, LLVM's integrated assembler rejects the
symbol difference `(bsp_riscv_image_header_end - _start)` with
`error: expected absolute expression`. The linker script already asserts the
same invariant (`osdk/src/base_crate/riscv64.ld.template`
`ASSERT(bsp_boot_entry - _start == RISCV_IMAGE_HEADER_SIZE_BYTES)`), so the
assembler-time guard is removed. Verified: `cargo osdk build --scheme riscv
--release` now compiles (previously failed at `Compiling ostd`).

### 1.3 Verification

```
$ cargo osdk clippy -- --no-deps   # RUSTFLAGS=-Dwarnings
  x86_64      EXIT=0
  riscv64     EXIT=0
  loongarch64 EXIT=0
$ cargo osdk build --scheme riscv --release   # EXIT=0
```

### 1.4 PR state

| PR | branch | content | state |
|---|---|---|---|
| #36 | `chore/fmt-nightly` | nightly fmt + `FsEventPublisher` intra-doc link | OPEN (pre-existing) |
| #37 | `chore/license-headers` | SPDX headers on 8 `tools/riscv/{systemd,xorg}` files | OPEN (pre-existing) |
| **#38** | `chore/ci-lint-ostd-release` | **this work** — clippy lints + ostd `--release` | **NEW** |
| #35 | `virtio-sound-driver` | sound driver + `#[expect(dead_code)]` fix | OPEN (updated) |

Together #36 (fmt) + #37 (license) + #38 (clippy/release) clear the
compile-time CI blockers; the remaining red is the pre-existing x86_64
runtime failures (`conformance-test (gvisor, *)`, `regression-test`, `basic-test
(ktest)`) which are not touched here.

---

## 2. ALSA evaluation — how to get real apps working

**Conclusion first:** the tractable path is **stock `alsa-lib` (cross-compiled for
riscv64) + the ALSA PCM ioctl ABI implemented in the kernel on the existing
`/dev/snd/pcmC0D0p` node**. The "virtio-sound ALSA backend" option collapses into
the same kernel-side work — Asterinas has no separate ALSA subsystem to write a
"backend" for, so the ALSA ABI must live on the sound device node either way.

### 2.1 Current state

- `kernel/comps/virtio/src/device/sound/device.rs` — virtio-sound driver,
  **hardcoded** `SET_PARAMS` (S16LE / 48000 Hz / 2 ch, fixed buffer/period).
- `kernel/src/device/misc/sound.rs` — `/dev/snd/pcmC0D0p` (misc major 10, minor
  116), `write()` hands raw PCM frames to `SoundDevice::play()`. No `ioctl`,
  no ALSA ABI, no `/dev/snd/controlC0`.

### 2.2 What stock alsa-lib needs

`libasound` does not magically stream bytes — it speaks the ALSA **ioctl ABI**
(`include/uapi/sound/asound.h`). For a minimal `aplay -D hw:0,0` playback path,
libasound performs roughly:

1. `open("/dev/snd/pcmC0D0p")`
2. `SNDRV_PCM_IOCTL_PVERSION` (protocol handshake)
3. `SNDRV_PCM_IOCTL_HW_REFINE` / `SNDRV_PCM_IOCTL_HW_PARAMS` (negotiate access,
   format, rate, channels, period/buffer size)
4. `SNDRV_PCM_IOCTL_SW_PARAMS` (start threshold etc.)
5. `SNDRV_PCM_IOCTL_PREPARE` → `START`
6. `SNDRV_PCM_IOCTL_WRITEI_FRAMES` (or mmap-based `MMAP`/`SYNC_PTR`)
7. `SNDRV_PCM_IOCTL_DROP` / `DRAIN` / `STATUS`

Asterinas already has the ioctl machinery (`kernel/src/syscall/ioctl.rs`, the
`ioc!` macro and `OutData` used by the framebuffer/evdev devices), so this is an
incremental ABI layer, not a new subsystem.

### 2.3 The two options

| | Option A — generic `/dev/snd` model | Option B — virtio-sound "ALSA backend" |
|---|---|---|
| User space | stock `alsa-lib` (cross-compiled) | stock `alsa-lib` (same) |
| Kernel work | implement ALSA PCM ioctl ABI as a reusable `/dev/snd` layer | implement the same ABI directly on the sound device |
| Result | apps see a normal ALSA card `hw:0,0` | identical (the ABI *is* what makes it an ALSA card) |
| Extra | add `/dev/snd/controlC0` (control/mixer — many apps probe it) | same |

Both options converge: the ABI surface is defined by `asound.h`, not by our
choice of where it lives. Option B is the pragmatic starting point (it reuses the
existing node), Option A is a refactor of B once a second sound source exists.

### 2.4 Required kernel changes (in order)

1. **Parameterize `SET_PARAMS`.** Replace the hardcoded format/rate/channels with
   values negotiated from `HW_PARAMS`. This is the only *driver* change; the rest
   is ABI plumbing. `PCM_INFO` already reports the device's real capabilities.
2. **Implement the ALSA PCM ioctl set** on `/dev/snd/pcmC0D0p`:
   `PVERSION`, `INFO`, `HW_REFINE`/`HW_PARAMS`/`HW_FREE`, `SW_PARAMS`, `STATUS`,
   `PREPARE`, `START`, `DROP`, `DRAIN`, `WRITEI_FRAMES` (mmap can be a later
   phase — libasound falls back to `snd_pcm_writei` → `WRITEI_FRAMES`).
   Struct layouts copied from `asound.h` (`snd_pcm_info`, `snd_pcm_hw_params`,
   `snd_pcm_sw_params`, `snd_pcm_status`, `snd_xferi`/`snd_xfern`).
3. **Add `/dev/snd/controlC0`** with a minimal `SNDRV_CTL_IOCTL_CARD_INFO` /
   `ELEM_LIST`/`ELEM_INFO`/`ELEM_READ` so `aplay`/mixers don't bail. Can be a
   no-op control with zero elements initially.
4. **Cross-compile `alsa-lib`** for riscv64 (`--host=riscv64-linux-gnu`, musl or
   glibc) and run `aplay`/`speaker-test` against `hw:0,0` in the QEMU harness.

### 2.5 Effort & recommendation

- **alsa-lib cross-compile: hours** (autotools, no surprises).
- **Kernel ALSA PCM ioctl ABI: the real work** — ~20 ioctls + struct
  definitions; a focused few days. The synchronous-polling `play()` is fine for a
  first cut; the period/streaming ring (AUDIO-M2) can come later.
- **Control device: small** (a handful of ioctls).

**Recommendation:** do Option B (ABI on the existing node) as the immediate next
milestone — it is the minimum delta that turns "raw device node" into "real ALSA
apps run". Option A is a later refactor, not a prerequisite.

Out of scope for ALSA, but a prerequisite for *many* real apps: the `mmap`-based
access model (libasound's `snd_pcm_mmap_*`), which some apps (JACK/PipeWire)
prefer. `snd_pcm_writei` coverage (via `WRITEI_FRAMES`) is enough to unlock
`aplay`/`ffmpeg`/`mpv`-with-fallback first.

---

## 3. LTP status

Not re-run this session (CI + ALSA evaluation took priority). Baseline from
`nixos-ltp-status`: **462 pass / 42 fail / 29 conf / 1 timeout**, with the
remaining failures dominated by loop-device env gaps (`fsopen`/`fsconfig`/…,
no `/dev/loop*`), a handful of userland env gaps, and open kernel bugs
(`fcntl14`, `sendfile07`, `sbrk01`, `readlink03`, `readlinkat02`, `gethostname02`,
`epoll_wait04`, `fork06`, `symlink03`, `epoll01`). Next step is a fresh
`tools/riscv/ltp-gate.sh --skip-build --smp 1` to re-baseline before resuming the
kernel-bug fixes.

---

## Next steps

1. Wait on #36/#37/#38 CI; the three together should clear `make check` +
   riscv `--release` integration tests (the x86 gvisor/regression/ktest runtime
   reds remain pre-existing and out of scope).
2. **AUDIO-M2**: parameterize `SET_PARAMS` + implement the ALSA PCM ioctl ABI on
   `/dev/snd/pcmC0D0p` (section 2.4), then cross-compile `alsa-lib` and verify
   `aplay` in the QEMU harness.
3. Re-baseline LTP and resume the open kernel-bug fixes.
