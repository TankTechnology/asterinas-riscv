// SPDX-License-Identifier: MPL-2.0

//! Intel HDA controller registers, codec verbs and parameters.
//!
//! Reference: Intel High Definition Audio Specification, and the register
//! layout Linux's `sound/pci/hda/hda_intel.c` uses.

/// Codec slots the controller samples in `STATESTS`.
pub(super) const MAX_CODECS: usize = 4;

// Controller registers (BAR0).
pub(super) const REG_GCAP: usize = 0x00;
pub(super) const REG_VMIN: usize = 0x02;
pub(super) const REG_VMAJ: usize = 0x03;
pub(super) const REG_GCTL: usize = 0x08;
pub(super) const REG_STATESTS: usize = 0x0E;
pub(super) const REG_INTCTL: usize = 0x20;
pub(super) const REG_INTSTS: usize = 0x24;
pub(super) const REG_CORBLBASE: usize = 0x40;
pub(super) const REG_CORBUBASE: usize = 0x44;
pub(super) const REG_CORBWP: usize = 0x48;
pub(super) const REG_CORBRP: usize = 0x4A;
pub(super) const REG_CORBCTL: usize = 0x4C;
pub(super) const REG_CORBSIZE: usize = 0x4E;
pub(super) const REG_RIRBLBASE: usize = 0x50;
pub(super) const REG_RIRBUBASE: usize = 0x54;
pub(super) const REG_RIRBWP: usize = 0x58;
pub(super) const REG_RINTCNT: usize = 0x5A;
pub(super) const REG_RIRBCTL: usize = 0x5C;
pub(super) const REG_RIRBSTS: usize = 0x5D;
pub(super) const REG_RIRBSIZE: usize = 0x5E;
pub(super) const REG_ICOI: usize = 0x60;
pub(super) const REG_ICII: usize = 0x64;
pub(super) const REG_ICIS: usize = 0x68;

/// `GCTL` bits.
pub(super) const GCTL_CRST: u32 = 1 << 0;

/// `CORBCTL`/`RIRBCTL` bits.
pub(super) const CORBCTL_RUN: u8 = 1 << 1;
pub(super) const RIRBCTL_RUN: u8 = 1 << 1;

/// `ICIS` bits.
pub(super) const ICIS_ICB: u32 = 1 << 0;
pub(super) const ICIS_IRV: u32 = 1 << 1;

/// Stream descriptor registers, relative to `0x80 + stream_index * 0x20`.
pub(super) const REG_SD_BASE: usize = 0x80;
pub(super) const SD_CTL: usize = 0x00;
pub(super) const SD_STS: usize = 0x03;
pub(super) const SD_LPIB: usize = 0x04;
pub(super) const SD_CBL: usize = 0x08;
pub(super) const SD_LVI: usize = 0x0C;
pub(super) const SD_FMT: usize = 0x12;
pub(super) const SD_BDPL: usize = 0x18;
pub(super) const SD_BDPU: usize = 0x1C;

/// `SD_CTL` bits.
pub(super) const SD_CTL_RESET: u8 = 1 << 0;
pub(super) const SD_CTL_RUN: u8 = 1 << 1;
pub(super) const SD_CTL_IOCE: u8 = 1 << 2;
pub(super) const SD_CTL_STRIPE_MASK: u8 = 0x3 << 4;

/// `SD_STS` bits.
pub(super) const SD_STS_BCIS: u8 = 1 << 2;
pub(super) const SD_STS_FIFOE: u8 = 1 << 3;

/// PCM stream format for 48 kHz, 16-bit, two channels.
///
/// Bits: 14 = 48 kHz base rate, 10:8 = rate divisor (0 = /1), 6:4 = 16-bit,
/// 3:0 = 2 channels.
pub(super) const FMT_48K_16BIT_STEREO: u16 = 0x4011;

/// Codec parameters (`GET_PARAMETER`).
pub(super) const PARAM_VENDOR_ID: u8 = 0x00;
pub(super) const PARAM_DEVICE_ID: u8 = 0x01;
pub(super) const PARAM_REVISION_ID: u8 = 0x02;
pub(super) const PARAM_NODE_COUNT: u8 = 0x04;
pub(super) const PARAM_FUNCTION_TYPE: u8 = 0x05;
pub(super) const PARAM_WIDGET_CAPS: u8 = 0x09;
pub(super) const PARAM_PIN_CAPS: u8 = 0x0C;
pub(super) const PARAM_CONNECTION_COUNT: u8 = 0x0E;

/// Codec verbs (12-bit ids).
pub(super) const VERB_GET_PARAMETER: u16 = 0xF00;
pub(super) const VERB_GET_CONNECTION_LIST: u16 = 0xF02;
/// The two 4-bit-payload verbs carry their value in the low 16 bits.
pub(super) const VERB_SET_STREAM_FORMAT_4BIT: u8 = 0x2;
pub(super) const VERB_SET_AMP_GAIN_MUTE_4BIT: u8 = 0x3;
pub(super) const VERB_SET_CONVERTER_STREAM: u16 = 0x706;
pub(super) const VERB_SET_PIN_WIDGET_CONTROL: u16 = 0x707;
pub(super) const VERB_SET_EAPD_BTLENABLE: u16 = 0x70C;

/// Widget types reported in bits 23:20 of `WIDGET_CAPS`.
pub(super) const WIDGET_TYPE_AUDIO_OUTPUT: u32 = 0x0;
pub(super) const WIDGET_TYPE_AUDIO_INPUT: u32 = 0x1;
pub(super) const WIDGET_TYPE_AUDIO_MIXER: u32 = 0x2;
pub(super) const WIDGET_TYPE_AUDIO_SELECTOR: u32 = 0x3;
pub(super) const WIDGET_TYPE_PIN_COMPLEX: u32 = 0x4;

/// `PIN_CAPS` bit: the pin can drive output.
pub(super) const PIN_CAP_OUTPUT: u32 = 1 << 2;

/// `SET_PIN_WIDGET_CONTROL` payload bits.
pub(super) const PIN_CTL_OUT_EN: u8 = 1 << 6;
pub(super) const PIN_CTL_HP_EN: u8 = 1 << 7;

/// `SET_AMP_GAIN_MUTE` payload: output amplifier, index 0, unmuted, 0 dB.
pub(super) const AMP_OUT_0DB: u16 = 0x8000;
