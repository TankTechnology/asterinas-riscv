// SPDX-License-Identifier: MPL-2.0

//! Reusable ALSA `/dev/snd` device model.
//!
//! Asterinas has no ALSA "subsystem" to plug a backend into, so the ALSA PCM
//! ioctl ABI (the `sound/asound.h` structs and ioctl command encodings) lives
//! here, on the device node. This mirrors Linux's `sound/core` split without
//! importing its complexity: a PCM device node speaks the ALSA PCM protocol
//! (magic `'A'`) and delegates the actual transport to a driver (currently the
//! virtio-sound device). The first consumer is `/dev/snd/pcmC0D0p` (see
//! [`crate::device::misc::sound`]).
//!
//! The struct layouts in [`pcm`] are transcribed verbatim from
//! `include/uapi/sound/asound.h` (LP64) so unmodified musl ALSA clients such as
//! `aplay` and `speaker-test` run against Asterinas. See
//! `docs/porting/snd-device-model.md` for the design.

pub mod control;
pub mod pcm;
