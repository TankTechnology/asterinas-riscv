// SPDX-License-Identifier: MPL-2.0

//! A minimal output stream: one buffer, one descriptor, one converter.
//!
//! This is the bring-up self-test. It programs the codec's DAC and output pin
//! for 48 kHz/16-bit/stereo, points a stream descriptor at a DMA buffer filled
//! with a square wave, runs the stream until the buffer is consumed, and stops
//! it. The host's audio backend receiving the samples (a WAV file under QEMU)
//! proves the whole path: codec verbs, stream descriptor, and DMA transfer.

use alloc::sync::Arc;

use aster_core::prelude::*;
use ostd::{
    info,
    mm::{HasDaddr, VmIo, dma::DmaCoherent},
};

use super::{
    HdaController,
    bus::CorbRirb,
    codec::{self, HdaCodec},
    regs::*,
};

/// Half a second of 48 kHz stereo 16-bit audio.
const BYTES: usize = 48_000 * 2 * 2 / 2;
/// The stream number/tag both the codec and the descriptor use.
const STREAM_TAG: u8 = 1;
/// Stream descriptor index. QEMU's model treats descriptors 0..3 as capture
/// and 4.. as playback, so an output stream must use index 4 or higher.
const STREAM_INDEX: usize = 4;

/// Plays one square-wave tone and returns the number of bytes transferred.
pub(super) fn play_test_tone(
    controller: &HdaController,
    codec: &HdaCodec,
    bus: &mut CorbRirb,
) -> Result<usize> {
    let dac = codec.dac().ok_or_else(|| Error::with_message(Errno::ENODEV, "no DAC"))?;
    let pin = codec.output_pin().ok_or_else(|| Error::with_message(Errno::ENODEV, "no output pin"))?;
    let address = codec.address();

    // Codec side: format, 0 dB gain, stream tag, pin enabled.
    codec::send_command(
        controller,
        bus,
        verb16(address, dac, VERB_SET_STREAM_FORMAT_4BIT, FMT_48K_16BIT_STEREO),
    )?;
    codec::send_command(
        controller,
        bus,
        verb16(address, dac, VERB_SET_AMP_GAIN_MUTE_4BIT, AMP_OUT_0DB),
    )?;
    codec::send_verb(
        controller,
        bus,
        address,
        dac,
        VERB_SET_CONVERTER_STREAM,
        STREAM_TAG << 4,
    )?;
    codec::send_verb(
        controller,
        bus,
        address,
        pin,
        VERB_SET_PIN_WIDGET_CONTROL,
        PIN_CTL_OUT_EN,
    )?;

    // Controller side: one buffer, one descriptor covering all of it.
    let base = REG_SD_BASE + STREAM_INDEX * 0x20;
    // The stream descriptor resets while SRST is set and releases when
    // software clears it; the bit does not self-clear.
    controller.write8(base + SD_CTL, SD_CTL_RESET);
    for _ in 0..1000 {
        core::hint::spin_loop();
    }
    controller.write8(base + SD_CTL, 0);

    let pages = BYTES.div_ceil(ostd::mm::PAGE_SIZE);
    let buffer = Arc::new(DmaCoherent::alloc(pages, true)?);
    fill_square_wave(&buffer)?;
    let bdl = Arc::new(DmaCoherent::alloc(1, true)?);
    bdl.write_val(0, &(buffer.daddr() as u64))?;
    bdl.write_val(8, &(BYTES as u32))?;
    // One descriptor: interrupt on completion, do not continue past it.
    bdl.write_val(12, &1u32)?;

    controller.write32(base + SD_CBL, BYTES as u32);
    controller.write16(base + SD_LVI, 0);
    controller.write32(base + SD_BDPL, bdl.daddr() as u32);
    controller.write32(base + SD_BDPU, (bdl.daddr() >> 32) as u32);
    controller.write16(base + SD_FMT, FMT_48K_16BIT_STEREO);
    controller.write8(base + SD_STS, 0xFF);

    // Stream number in bits 23:20, then run.
    controller.write8(base + 0x02, STREAM_TAG << 4);
    controller.write8(base + SD_CTL, SD_CTL_RUN | SD_CTL_IOCE);
    for _ in 0..100_000 {
        core::hint::spin_loop();
    }
    info!(
        "stream: after start ctl={:#x} sts={:#x} lpib={} cbl={} bdl={:#x}",
        controller.read8(base + SD_CTL),
        controller.read8(base + SD_STS),
        controller.read32(base + SD_LPIB),
        controller.read32(base + SD_CBL),
        controller.read32(base + SD_BDPL)
    );

    // Wait for the buffer-completion status: the descriptor asks for an
    // interrupt, and LPIB alone wraps back to zero at the buffer length.
    let mut waited = 0u64;
    while controller.read8(base + SD_STS) & SD_STS_BCIS == 0
        && (controller.read32(base + SD_LPIB) as usize) < BYTES
    {
        core::hint::spin_loop();
        waited += 1;
        if waited > 50_000_000 {
            info!(
                "stream: timed out ctl={:#x} sts={:#x} lpib={} bdl={:#x}",
                controller.read8(base + SD_CTL),
                controller.read8(base + SD_STS),
                controller.read32(base + SD_LPIB),
                controller.read32(base + SD_BDPL)
            );
            controller.write8(base + SD_CTL, 0);
            return_errno_with_message!(Errno::ETIMEDOUT, "the stream did not complete");
        }
    }

    controller.write8(base + SD_CTL, 0);
    info!(
        "stream: played {} bytes on dac {} pin {} (lpib={})",
        BYTES,
        dac,
        pin,
        controller.read32(base + SD_LPIB)
    );

    Ok(BYTES)
}

/// Fills the buffer with a 440 Hz square wave (period 109 samples at 48 kHz).
fn fill_square_wave(buffer: &Arc<DmaCoherent>) -> Result<()> {
    let samples = BYTES / 2;
    for index in 0..samples {
        let positive = (index / 54) % 2 == 0;
        let sample: i16 = if positive { 8000 } else { -8000 };
        buffer.write_val(index * 2, &sample)?;
    }
    Ok(())
}

/// Encodes a 4-bit verb id with its 16-bit payload.
fn verb16(codec: u8, node: u8, verb4: u8, payload: u16) -> u32 {
    ((codec as u32) << 28)
        | ((node as u32) << 20)
        | ((verb4 as u32) << 16)
        | payload as u32
}
