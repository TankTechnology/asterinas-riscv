// SPDX-License-Identifier: MPL-2.0

//! The console I/O.

use core::fmt;

use spin::Once;

use crate::{
    boot::EarlyCmdline,
    sync::{LocalIrqDisabled, SpinLock},
};

/// The primary serial port, which serves as an early console.
pub(crate) static SERIAL_PORT: Once<SpinLock<SbiSerial, LocalIrqDisabled>> =
    Once::initialized(SpinLock::new(SbiSerial::new()));

/// A serial port that is implemented via RISC-V SBI.
pub(crate) struct SbiSerial {
    _private: (),
}

impl SbiSerial {
    const fn new() -> Self {
        Self { _private: () }
    }
}

impl fmt::Write for SbiSerial {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        // Marked like the logger's own print path: a hart inside one of these
        // byte-by-byte firmware writes takes no traps at all, and that is
        // otherwise reported as a plain `idle`.
        crate::smp::note_activity(crate::smp::ACT_EARLY_PRINT);
        let start = crate::arch::read_tsc();
        for c in s.as_bytes() {
            sbi_rt::console_write_byte(*c);
        }
        let elapsed = crate::arch::read_tsc().saturating_sub(start);
        let frequency = crate::arch::tsc_freq();
        // Before the timebase is known every duration would be nonsense; the
        // early-boot lines are also not the ones that matter here.
        if frequency > 1000 {
            let elapsed_us =
                (elapsed.saturating_mul(1_000_000) / frequency).min(u64::from(u32::MAX)) as u32;
            crate::uart_stats::note_fw_write(elapsed_us);
        }
        crate::smp::note_activity(0);
        Ok(())
    }
}

/// Initializes the serial port.
pub(crate) fn init(_early_cmdline: &EarlyCmdline) {}
