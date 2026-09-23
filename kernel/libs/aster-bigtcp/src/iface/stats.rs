// SPDX-License-Identifier: MPL-2.0

//! Packet counters shared by Ethernet and loopback interfaces.

use core::sync::atomic::{AtomicU64, Ordering};

#[derive(Default)]
pub(crate) struct IfaceStats {
    rx_bytes: AtomicU64,
    rx_packets: AtomicU64,
    tx_bytes: AtomicU64,
    tx_packets: AtomicU64,
}

/// A point-in-time sample of packets consumed by one interface.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct IfaceStatsSnapshot {
    pub rx_bytes: u64,
    pub rx_packets: u64,
    pub tx_bytes: u64,
    pub tx_packets: u64,
}

impl IfaceStats {
    pub(crate) fn snapshot(&self) -> IfaceStatsSnapshot {
        IfaceStatsSnapshot {
            rx_bytes: self.rx_bytes.load(Ordering::Relaxed),
            rx_packets: self.rx_packets.load(Ordering::Relaxed),
            tx_bytes: self.tx_bytes.load(Ordering::Relaxed),
            tx_packets: self.tx_packets.load(Ordering::Relaxed),
        }
    }

    pub(crate) fn record_rx(&self, bytes: usize) {
        self.rx_bytes.fetch_add(bytes as u64, Ordering::Relaxed);
        self.rx_packets.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn record_tx(&self, bytes: usize) {
        self.tx_bytes.fetch_add(bytes as u64, Ordering::Relaxed);
        self.tx_packets.fetch_add(1, Ordering::Relaxed);
    }
}
