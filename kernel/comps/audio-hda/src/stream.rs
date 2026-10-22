// SPDX-License-Identifier: MPL-2.0

//! The output stream: codec programming, one DMA buffer, one descriptor.
//!
//! Every playback call is bounded by the data it is given: the buffer is
//! programmed, the stream runs until the buffer-completion status is set, and
//! the stream stops. That is enough for `aplay`'s blocking writes and for the
//! bring-up self-test.

use alloc::{sync::Arc, vec::Vec};

use aster_core::prelude::*;
use ostd::{
    info, warn,
    mm::{HasDaddr, VmIo, dma::DmaCoherent},
};

use super::{HdaController, PlaybackCodec, bus::CorbRirb, codec, regs::*};

/// Stream descriptor index. QEMU's model treats descriptors 0..=3 as capture
/// and 4..=7 as playback, so an output stream must use index 4 or higher.
pub(super) const STREAM_INDEX: usize = 4;
/// The stream number/tag both the codec and the descriptor use.
pub(super) const STREAM_TAG: u8 = 1;

/// Plays `data` (48 kHz, 16-bit, two channels, interleaved) and returns once
/// the stream has consumed it.
pub(super) fn play_bytes(
    controller: &HdaController,
    codec: PlaybackCodec,
    bus: &mut CorbRirb,
    data: &[u8],
) -> Result<()> {
    if data.is_empty() {
        return Ok(());
    }

    program_codec(controller, codec, bus)?;

    let base = REG_SD_BASE + STREAM_INDEX * 0x20;
    // The stream descriptor resets while SRST is set and releases when
    // software clears it; the bit does not self-clear.
    controller.write8(base + SD_CTL, SD_CTL_RESET);
    for _ in 0..1000 {
        core::hint::spin_loop();
    }
    controller.write8(base + SD_CTL, 0);

    let pages = data.len().div_ceil(ostd::mm::PAGE_SIZE);
    let buffer = Arc::new(DmaCoherent::alloc(pages, true)?);
    copy_into_buffer(&buffer, data)?;

    let bdl = Arc::new(DmaCoherent::alloc(1, true)?);
    bdl.write_val(0, &(buffer.daddr() as u64))?;
    bdl.write_val(8, &(data.len() as u32))?;
    // One descriptor: interrupt on completion, do not continue past it.
    bdl.write_val(12, &1u32)?;

    controller.write32(base + SD_CBL, data.len() as u32);
    controller.write16(base + SD_LVI, 0);
    controller.write32(base + SD_BDPL, bdl.daddr() as u32);
    controller.write32(base + SD_BDPU, (bdl.daddr() >> 32) as u32);
    controller.write16(base + SD_FMT, FMT_48K_16BIT_STEREO);
    controller.write8(base + SD_STS, 0xFF);

    // Stream number in bits 23:20, then run.
    controller.write8(base + 0x02, STREAM_TAG << 4);
    controller.write8(base + SD_CTL, SD_CTL_RUN | SD_CTL_IOCE);

    let mut waited = 0u64;
    while controller.read8(base + SD_STS) & SD_STS_BCIS == 0
        && (controller.read32(base + SD_LPIB) as usize) < data.len()
    {
        core::hint::spin_loop();
        waited += 1;
        if waited > 200_000_000 {
            warn!(
                "stream: timeout len={} lpib={} ctl={:#x} sts={:#x}",
                data.len(),
                controller.read32(base + SD_LPIB),
                controller.read8(base + SD_CTL),
                controller.read8(base + SD_STS)
            );
            controller.write8(base + SD_CTL, 0);
            return_errno_with_message!(Errno::ETIMEDOUT, "the stream did not complete");
        }
    }

    controller.write8(base + SD_CTL, 0);
    Ok(())
}

/// Programs the DAC and pin for the stream the descriptor uses.
fn program_codec(
    controller: &HdaController,
    codec: PlaybackCodec,
    bus: &mut CorbRirb,
) -> Result<()> {
    let address = codec.address;
    // Format, 0 dB gain, stream tag, pin enabled.
    codec::send_command(
        controller,
        bus,
        verb16(
            address,
            codec.dac,
            VERB_SET_STREAM_FORMAT_4BIT,
            FMT_48K_16BIT_STEREO,
        ),
    )?;
    codec::send_command(
        controller,
        bus,
        verb16(address, codec.dac, VERB_SET_AMP_GAIN_MUTE_4BIT, AMP_OUT_0DB),
    )?;
    codec::send_verb(
        controller,
        bus,
        address,
        codec.dac,
        VERB_SET_CONVERTER_STREAM,
        STREAM_TAG << 4,
    )?;
    codec::send_verb(
        controller,
        bus,
        address,
        codec.pin,
        VERB_SET_PIN_WIDGET_CONTROL,
        PIN_CTL_OUT_EN,
    )?;
    Ok(())
}

fn copy_into_buffer(buffer: &Arc<DmaCoherent>, data: &[u8]) -> Result<()> {
    // `DmaCoherent` is word-addressable through `VmIo`; copy in 4-byte chunks
    // with a zero-padded tail.
    let mut offset = 0;
    while offset + 4 <= data.len() {
        let word = u32::from_le_bytes([
            data[offset],
            data[offset + 1],
            data[offset + 2],
            data[offset + 3],
        ]);
        buffer.write_val(offset, &word)?;
        offset += 4;
    }
    if offset < data.len() {
        let mut tail = [0u8; 4];
        tail[..data.len() - offset].copy_from_slice(&data[offset..]);
        buffer.write_val(offset, &u32::from_le_bytes(tail))?;
    }
    Ok(())
}

/// A half-second 440 Hz square wave at 48 kHz/16-bit/stereo.
pub(super) fn test_tone() -> Vec<u8> {
    let frames = 24_000usize;
    let mut data = Vec::with_capacity(frames * 4);
    for index in 0..frames {
        let positive = (index / 54) % 2 == 0;
        let sample: i16 = if positive { 8000 } else { -8000 };
        data.extend_from_slice(&sample.to_le_bytes());
        data.extend_from_slice(&sample.to_le_bytes());
    }
    data
}

/// Plays the bring-up tone; used by the `aster_audio.selftest` path.
pub(super) fn play_test_tone(
    controller: &HdaController,
    codec: PlaybackCodec,
    bus: &mut CorbRirb,
) -> Result<usize> {
    let data = test_tone();
    play_bytes(controller, codec, bus, &data)?;
    info!(
        "stream: played {} bytes on dac {} pin {}",
        data.len(),
        codec.dac,
        codec.pin
    );
    Ok(data.len())
}

/// Encodes a 4-bit verb id with its 16-bit payload.
fn verb16(codec: u8, node: u8, verb4: u8, payload: u16) -> u32 {
    ((codec as u32) << 28)
        | ((node as u32) << 20)
        | ((verb4 as u32) << 16)
        | payload as u32
}
