// SPDX-License-Identifier: MPL-2.0

//! Monotonic TCP SYN-ACK ingress diagnostics.

use core::sync::atomic::{AtomicU8, Ordering};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub(super) enum SynAckStage {
    Parsed = 1,
    ConnectionFound = 2,
    SocketAccepted = 3,
}

pub(super) struct SynAckTrace(AtomicU8);

impl SynAckTrace {
    pub(super) const fn new() -> Self {
        Self(AtomicU8::new(0))
    }

    /// Records a stage and returns whether it advanced the global observation.
    pub(super) fn record(&self, stage: SynAckStage) -> bool {
        self.0.fetch_max(stage as u8, Ordering::Relaxed) < stage as u8
    }
}

impl SynAckStage {
    pub(super) const fn as_str(self) -> &'static str {
        match self {
            Self::Parsed => "parsed",
            Self::ConnectionFound => "connection-found",
            Self::SocketAccepted => "socket-accepted",
        }
    }
}
