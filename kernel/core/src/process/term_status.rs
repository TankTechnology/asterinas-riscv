// SPDX-License-Identifier: MPL-2.0

use super::signal::sig_num::SigNum;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum TermStatus {
    Exited(u8),
    Killed(SigNum),
    /// Killed by a signal whose default action dumps core, while core dumps
    /// were enabled (`RLIMIT_CORE` greater than zero).
    KilledWithCore(SigNum),
}

impl TermStatus {
    /// Return as a 32-bit integer encoded as specified in wait(2) man page.
    pub(crate) fn as_u32(&self) -> u32 {
        match self {
            TermStatus::Exited(status) => (*status as u32) << 8,
            TermStatus::Killed(signum) => signum.as_u8() as u32,
            // Bit 7 of the wait status is `WCOREDUMP`.
            TermStatus::KilledWithCore(signum) => (signum.as_u8() as u32) | 0x80,
        }
    }
}
