// SPDX-License-Identifier: MPL-2.0

//! The CORB/RIRB command rings.
//!
//! Commands go out through the Command Output Ring Buffer and responses come
//! back through the Response Input Ring Buffer; both are DMA memory the
//! controller walks with its own write pointer. QEMU's `intel-hda` answers on
//! these rings (its immediate-command interface stays silent), so this is the
//! path the driver uses for every codec verb.

use alloc::sync::Arc;

use aster_core::prelude::*;
use ostd::info;
use ostd::mm::{HasDaddr, PAGE_SIZE, VmIo, dma::DmaCoherent};

use super::{HdaController, regs::*};

/// Entries per ring: the controller's largest supported size.
const ENTRIES: usize = 256;
/// `CORBSIZE`/`RIRBSIZE` code for 256 entries.
const SIZE_CODE_256: u8 = 0x2;

/// One controller's command and response rings.
#[derive(Debug)]
pub(super) struct CorbRirb {
    corb: Arc<DmaCoherent>,
    rirb: Arc<DmaCoherent>,
    /// The last command slot used; both rings advance before use.
    next_command: u16,
}

impl CorbRirb {
    /// Allocates the rings and starts them.
    pub(super) fn new(controller: &HdaController) -> Result<Self> {
        let corb = Arc::new(DmaCoherent::alloc(1, true)?);
        let rirb = Arc::new(DmaCoherent::alloc(1, true)?);
        assert!(PAGE_SIZE >= ENTRIES * 8, "one page holds both rings");

        // Quiesce both rings before re-programming them.
        controller.write8(REG_CORBCTL, 0);
        controller.write8(REG_RIRBCTL, 0);
        controller.write8(REG_CORBSIZE, SIZE_CODE_256);
        controller.write8(REG_RIRBSIZE, SIZE_CODE_256);

        // Reset the write pointers: set the reset bit, then clear it.
        controller.write16(REG_CORBRP, 0x8000);
        controller.write16(REG_CORBRP, 0);
        controller.write16(REG_RIRBWP, 0x8000);

        let corb_addr = corb.daddr();
        let rirb_addr = rirb.daddr();
        controller.write32(REG_CORBLBASE, corb_addr as u32);
        controller.write32(REG_CORBUBASE, (corb_addr >> 32) as u32);
        controller.write32(REG_RIRBLBASE, rirb_addr as u32);
        controller.write32(REG_RIRBUBASE, (rirb_addr >> 32) as u32);

        // No interrupts: the driver polls the response pointer, and a large
        // response count keeps the controller from stopping after every
        // response to wait for an interrupt acknowledge.
        controller.write16(REG_RINTCNT, 0xFF);
        controller.write8(REG_CORBCTL, CORBCTL_RUN);
        controller.write8(REG_RIRBCTL, RIRBCTL_RUN);
        info!(
            "rings corb={:#x} rirb={:#x} corbctl={:#x} rirbctl={:#x} corbsize={:#x} rirbsize={:#x}",
            corb_addr,
            rirb_addr,
            controller.read8(REG_CORBCTL),
            controller.read8(REG_RIRBCTL),
            controller.read8(REG_CORBSIZE),
            controller.read8(REG_RIRBSIZE)
        );

        Ok(Self {
            corb,
            rirb,
            next_command: 0,
        })
    }

    /// Sends one verb and waits for its response.
    pub(super) fn send(&mut self, controller: &HdaController, verb: u32) -> Result<u32> {
        // Both rings advance *before* use: the first command goes to slot 1,
        // and QEMU (like the hardware) reads `corb_rp + 1`.
        let index = ((self.next_command as usize + 1) % ENTRIES) as u16;
        self.corb.write_val(index as usize * 4, &verb)?;

        self.next_command = index;
        controller.write16(REG_CORBWP, index);

        // The response write pointer is "next": after the command at `index`
        // is answered it equals `index + 1`, and the response is at `index`.
        let mut waited = 0u32;
        while controller.read16(REG_RIRBWP) & 0xFF != self.next_command {
            core::hint::spin_loop();
            waited += 1;
            if waited > 5_000_000 {
                return_errno_with_message!(Errno::ETIMEDOUT, "the codec did not answer");
            }
        }

        // Acknowledge the response so the controller keeps filling the ring.
        controller.write8(REG_RIRBSTS, 1);

        let response: u32 = self.rirb.read_val(index as usize * 8)?;
        Ok(response)
    }
}
