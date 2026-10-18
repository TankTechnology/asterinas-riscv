// SPDX-License-Identifier: MPL-2.0

mod ext;
mod init;
mod poll;
mod sched;

pub(super) use init::{init, iter_all_ifaces};
pub(super) use poll::init_in_first_kthread;

use crate::prelude::*;

/// One interface's name and packet counters, as sampled for `/proc/net/dev`.
pub(crate) struct IfaceStatus {
    pub(crate) name: String,
    pub(crate) stats: aster_bigtcp::iface::IfaceStatsSnapshot,
}

/// One entry of an interface's neighbour cache, as `/proc/net/arp` needs it.
pub(crate) struct ArpEntry {
    pub(crate) ip: aster_bigtcp::wire::Ipv4Address,
    pub(crate) mac: aster_bigtcp::wire::EthernetAddress,
    pub(crate) iface: String,
}

/// Samples every interface's neighbour cache.
pub(crate) fn arp_entries() -> Vec<ArpEntry> {
    let mut entries = Vec::new();
    for iface in iter_all_ifaces() {
        let name = iface.name().as_str().unwrap_or_default();
        for (ip, mac) in iface.arp_entries() {
            entries.push(ArpEntry {
                ip,
                mac,
                iface: name.into(),
            });
        }
    }
    entries
}

/// Samples every interface, in index order.
///
/// Returns an owned snapshot rather than the interfaces themselves, so callers outside this
/// module do not have to name [`Iface`] or the ext type.
pub(crate) fn iface_statuses() -> Vec<IfaceStatus> {
    iter_all_ifaces()
        .map(|iface| IfaceStatus {
            // A `FixedCStr` stores whatever bytes the driver was handed. The names in use are
            // ASCII; a non-UTF-8 one is not worth failing the whole read over.
            name: iface.name().as_str().unwrap_or_default().into(),
            stats: iface.stats(),
        })
        .collect()
}

pub(super) type Iface = dyn aster_bigtcp::iface::Iface<ext::BigtcpExt>;
pub(super) type BoundTcpPort = aster_bigtcp::iface::BoundTcpPort<ext::BigtcpExt>;
pub(super) type BoundUdpPort = aster_bigtcp::iface::BoundUdpPort<ext::BigtcpExt>;

pub(super) type RawTcpSocketExt = aster_bigtcp::socket::RawTcpSocketExt<ext::BigtcpExt>;

pub(super) type TcpConnection = aster_bigtcp::socket::TcpConnection<ext::BigtcpExt>;
pub(super) type TcpListener = aster_bigtcp::socket::TcpListener<ext::BigtcpExt>;
pub(super) type UdpSocket = aster_bigtcp::socket::UdpSocket<ext::BigtcpExt>;

/// The default transmit queue length.
///
/// On Linux, this value limits the number of SKBs
/// that can be queued in a network device's egress qdisc.
/// This value does not take effect on Asterinas now.
///
/// Reference: <https://elixir.bootlin.com/linux/v7.1/source/include/net/pkt_sched.h#L13>.
pub(super) const DEFAULT_TX_QUEUE_LEN: u32 = 1000;
