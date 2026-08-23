// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use crate::{
    net::net_ns::current_net_ns,
    prelude::*,
    process::{credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks as lsm_hooks,
};

// Port 0 means an ephemeral port is requested; it is never privileged.
const PRIVILEGED_PORTS: Range<u16> = 1..1024;

/// Checks if the port is privileged and, if so, whether the thread is allowed to bind to it.
pub fn check_port_privilege(port: u16) -> Result<()> {
    if !PRIVILEGED_PORTS.contains(&port) {
        return Ok(());
    }

    // The capability is checked against the owner user namespace of the
    // current network namespace, so a process with capabilities in its own
    // user namespace can bind privileged ports inside its sandbox (as in
    // Linux).
    let thread = current_thread!();
    let posix_thread = thread.as_posix_thread().unwrap();
    let owner_user_ns = current_net_ns().owner().clone();
    if lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
        owner_user_ns.as_ref(),
        posix_thread,
        CapSet::NET_BIND_SERVICE,
    ))
    .is_ok()
    {
        return Ok(());
    }

    return_errno_with_message!(
        Errno::EACCES,
        "only threads with CAP_NET_BIND_SERVICE can bind to privileged ports"
    );
}
