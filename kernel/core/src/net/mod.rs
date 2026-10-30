// SPDX-License-Identifier: MPL-2.0

use alloc::string::{String, ToString};

use aster_bigtcp::iface::IfaceStatsSnapshot;

mod iface;
mod route;

pub(crate) use iface::{Iface, iter_all_ifaces};
pub(crate) use route::{
    Ipv4RouteSnapshot, add_ipv4_route, ipv4_route_snapshots, lookup_ipv4_route,
};
pub(crate) mod socket;
pub(crate) mod uts_ns;

pub(crate) fn init() {
    iface::init();
    route::init();
    socket::netlink::init();
    socket::vsock::init();
}

/// Returns the current packet counters of every network interface.
pub(crate) fn iface_stats() -> impl Iterator<Item = (String, IfaceStatsSnapshot)> {
    iter_all_ifaces().map(|iface| {
        let name = iface.name().as_str().unwrap_or("?").to_string();
        (name, iface.stats())
    })
}

/// Lazy init should be called after spawning init thread.
pub(crate) fn init_in_first_kthread() {
    iface::init_in_first_kthread();
}
