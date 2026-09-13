// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicU32, Ordering};

pub mod iface;
pub mod net_ns;
pub mod socket;
pub mod uts_ns;

static TCP_DIAGNOSTIC_PORT: AtomicU32 = AtomicU32::new(0);

aster_cmdline::define_kv_param!("asterinas.tcp_diagnostic_port", TCP_DIAGNOSTIC_PORT);

pub fn init() {
    let diagnostic_port = TCP_DIAGNOSTIC_PORT.load(Ordering::Relaxed);
    if let Ok(diagnostic_port) = u16::try_from(diagnostic_port)
        && diagnostic_port != 0
    {
        aster_bigtcp::iface::configure_tcp_diagnostics(diagnostic_port);
    }
    iface::init();
    socket::netlink::init();
    socket::vsock::init();
}

/// Lazy init should be called after spawning init thread.
pub fn init_in_first_kthread() {
    iface::init_in_first_kthread();
}
