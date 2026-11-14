// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/interrupts`, the per-IRQ delivery counters that
//! `htop`, `irqtop`, `mpstat -I` and most "why is the board busy" one-liners
//! read.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_interrupts.5.html>
//!
//! The format is Linux's, with a single CPU column: the kernel counts
//! deliveries per IRQ line rather than per line and hart (see
//! `ostd::irq::IRQ_COUNTS`), because none of the tools this image targets
//! depends on the split and a per-CPU table would need a CPU id in the IRQ
//! dispatch path that is not portable across the architectures OSTD builds for.
//! Lines that have never fired are omitted, as on Linux.

use alloc::{format, string::String};

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// The inode at `/proc/interrupts`.
pub struct InterruptsFileOps;

impl InterruptsFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for InterruptsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        // Linux pads the header to the width of the counter columns so the
        // numbers line up under it.
        writeln!(
            printer,
            "{:>11} {:>10}  {:<10} {}",
            "", "CPU0", "Type", "Device"
        )?;

        for (irq_num, counter) in ostd::irq::irq_counts().iter().enumerate() {
            let count = counter.load(core::sync::atomic::Ordering::Relaxed);
            if count == 0 {
                continue;
            }
            let (kind, device) = describe(irq_num as u8);
            writeln!(
                printer,
                "{:>3}: {:>10}  {:<10} {}",
                irq_num, count, kind, device
            )?;
        }

        // Linux ends the file with the architecture-specific error counters;
        // the board's PLIC reports none of them separately, so the section is
        // present but empty rather than missing (a missing section makes some
        // parsers stop reading).
        writeln!(printer, "ERR:")?;
        writeln!(printer, "MIS:")?;

        Ok(printer.bytes_written())
    }
}

/// Names the interrupt line the way an operator would recognize it.
///
/// The kernel does not keep a device name per line, so this is derived from
/// the line number: the RISC-V hart-level lines are fixed by the ISA, and the
/// external lines are the PLIC numbers the device tree and the drivers use.
fn describe(irq_num: u8) -> (String, String) {
    let (kind, device): (&str, String) = match irq_num {
        0 => ("RISC-V", String::from("software")),
        1 => ("RISC-V", String::from("timer")),
        2 => ("RISC-V", String::from("external")),
        n => ("PLIC", format!("line {}", n)),
    };
    (String::from(kind), device)
}
