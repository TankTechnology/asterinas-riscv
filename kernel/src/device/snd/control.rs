// SPDX-License-Identifier: MPL-2.0

//! ALSA control ioctl ABI (minimal: `CARD_INFO` only).
//!
//! `libasound` resolves a numeric card (e.g. `hw:0,0`) by opening
//! `/dev/snd/controlC<N>` and issuing `SNDRV_CTL_IOCTL_CARD_INFO`
//! (`snd_card_load2`), so a playback-only card still needs a control node that
//! answers `CARD_INFO`. This module provides just that.

use crate::prelude::*;

/// `struct snd_ctl_card_info` (376 bytes on LP64).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct SndCtlCardInfo {
    pub card: i32,
    pub pad: i32,
    pub id: [u8; 16],
    pub driver: [u8; 16],
    pub name: [u8; 32],
    pub longname: [u8; 80],
    pub reserved_: [u8; 16],
    pub mixername: [u8; 80],
    pub components: [u8; 128],
}

/// The ALSA control protocol version we report (`SNDRV_PROTOCOL_VERSION(2, 0, 9)`).
pub const SNDRV_CTL_VERSION: i32 = 0x20009;

pub(crate) mod ioctl_defs {
    use super::SndCtlCardInfo;
    use crate::util::ioctl::{InData, OutData, ioc};

    // Reference: <https://elixir.bootlin.com/linux/v6.17/source/include/uapi/sound/asound.h#L1165-L1185>
    pub(crate) type Pversion = ioc!(SNDRV_CTL_IOCTL_PVERSION, b'U', 0x00, OutData<i32>);
    pub(crate) type CardInfo = ioc!(SNDRV_CTL_IOCTL_CARD_INFO, b'U', 0x01, OutData<SndCtlCardInfo>);
    pub(crate) type SubscribeEvents = ioc!(SNDRV_CTL_IOCTL_SUBSCRIBE_EVENTS, b'U', 0x32, InData<i32>);
}

/// Builds the `SNDRV_CTL_IOCTL_CARD_INFO` reply for the (single) virtio-sound
/// card.
pub fn build_card_info() -> SndCtlCardInfo {
    let mut info = SndCtlCardInfo::new_zeroed();
    info.card = 0;
    let id = b"virtio-sound";
    info.id[..id.len()].copy_from_slice(id);
    let driver = b"virtio-sound";
    info.driver[..driver.len()].copy_from_slice(driver);
    let name = b"Asterinas Virtio Sound";
    info.name[..name.len()].copy_from_slice(name);
    let longname = b"Asterinas virtio-sound";
    info.longname[..longname.len()].copy_from_slice(longname);
    info
}
