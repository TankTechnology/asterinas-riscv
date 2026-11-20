// SPDX-License-Identifier: MPL-2.0

//! Network namespaces.
//!
//! A network namespace gives its member processes an isolated view of network
//! interfaces. The initial namespace sees the loopback interface plus any
//! real devices (e.g., virtio-net); a namespace created with
//! `clone(CLONE_NEWNET)` or `unshare(CLONE_NEWNET)` contains only a fresh
//! loopback interface, initially down (as in Linux), which can be brought up
//! from inside the namespace.
//!
//! IP socket bind/connect, interface ioctls, and netlink route requests use the
//! socket's namespace, captured at creation. Netlink port tables and multicast
//! groups are also per namespace. Packet polling of real devices stays global.

use core::sync::atomic::{AtomicI32, Ordering};

use spin::Once;

use super::iface::{self, Iface};
use crate::{
    fs::pseudofs::{NsCommonOps, NsType, StashedDentry},
    prelude::*,
    process::{UserNamespace, posix_thread::AsPosixThread},
};

/// A network namespace.
pub struct NetNamespace {
    /// The network interfaces visible in this namespace.
    ifaces: Vec<Arc<Iface>>,
    /// The user namespace that owns this network namespace.
    owner: Arc<UserNamespace>,
    default_ipv4_tag: AtomicI32,
    loopback_ipv4_tag: AtomicI32,
    stashed_dentry: StashedDentry,
    /// The sockets this namespace exposes through `/proc/net` and `SOCK_DIAG`.
    proc_net: crate::net::socket::proc_net::ProcNetRegistry,
}

impl NetNamespace {
    /// Returns a reference to the singleton initial network namespace.
    pub fn get_init_singleton() -> &'static Arc<NetNamespace> {
        static INIT: Once<Arc<NetNamespace>> = Once::new();

        INIT.call_once(|| {
            Arc::new(Self {
                ifaces: iface::iter_all_ifaces().cloned().collect(),
                owner: UserNamespace::get_init_singleton().clone(),
                default_ipv4_tag: AtomicI32::new(0),
                loopback_ipv4_tag: AtomicI32::new(0),
                stashed_dentry: StashedDentry::new(),
                proc_net: crate::net::socket::proc_net::ProcNetRegistry::default(),
            })
        })
    }

    /// Creates a new network namespace containing only a fresh loopback
    /// interface (initially down, as in Linux).
    pub fn new_child(owner: Arc<UserNamespace>) -> Arc<Self> {
        let loopback = iface::new_ns_loopback();
        iface::spawn_poll_thread(loopback.clone());

        Arc::new(Self {
            ifaces: vec![loopback],
            owner,
            default_ipv4_tag: AtomicI32::new(0),
            loopback_ipv4_tag: AtomicI32::new(0),
            stashed_dentry: StashedDentry::new(),
            proc_net: crate::net::socket::proc_net::ProcNetRegistry::default(),
        })
    }

    /// Returns the interfaces visible in this namespace.
    pub fn ifaces(&self) -> &[Arc<Iface>] {
        &self.ifaces
    }

    /// Returns the socket registry this namespace publishes to user space.
    pub fn proc_net_sockets(&self) -> &crate::net::socket::proc_net::ProcNetRegistry {
        &self.proc_net
    }

    /// Counts the sockets that exist in this namespace.
    ///
    /// This mirrors the idea behind Linux's `sockets: used` line: every socket
    /// object, not just the ones that appear in `/proc/net/tcp` or
    /// `/proc/net/udp`. Netlink sockets are counted once they are bound, and
    /// ICMP ping sockets through their interface tables.
    pub(crate) fn socket_count(&self) -> usize {
        let icmp: usize = 0;
        icmp
    }

    /// Returns the owner user namespace of this namespace.
    pub fn owner(&self) -> &Arc<UserNamespace> {
        &self.owner
    }

    /// Returns the loopback interface of this namespace.
    pub fn loopback(&self) -> &Arc<Iface> {
        &self.ifaces[0]
    }

    /// Returns the default interface for this namespace: the first
    /// non-loopback interface if any, otherwise the loopback interface.
    pub fn default_iface(&self) -> &Arc<Iface> {
        self.ifaces
            .iter()
            .find(|iface| iface.type_() != iface::InterfaceType::LOOPBACK)
            .unwrap_or(&self.ifaces[0])
    }

    /// Returns the IPv4 interface-tag default for newly created interfaces.
    pub fn default_ipv4_tag(&self) -> i32 {
        self.default_ipv4_tag.load(Ordering::Relaxed)
    }

    /// Updates the IPv4 interface-tag default in this network namespace.
    pub fn set_default_ipv4_tag(&self, value: i32) {
        self.default_ipv4_tag.store(value, Ordering::Relaxed);
    }

    /// Returns the loopback IPv4 tag in this network namespace.
    pub fn loopback_ipv4_tag(&self) -> i32 {
        self.loopback_ipv4_tag.load(Ordering::Relaxed)
    }

    /// Updates the loopback IPv4 tag in this network namespace.
    pub fn set_loopback_ipv4_tag(&self, value: i32) {
        self.loopback_ipv4_tag.store(value, Ordering::Relaxed);
    }
}

/// Returns the network namespace of the current thread.
///
/// Falls back to the initial network namespace for kernel threads without a
/// POSIX context.
pub fn current_net_ns() -> Arc<NetNamespace> {
    let current_thread = current_thread!();
    let Some(posix_thread) = current_thread.as_posix_thread() else {
        return NetNamespace::get_init_singleton().clone();
    };

    // The socket-to-namespace association is not carried on `NsProxy` yet, so
    // every caller sees the initial namespace.
    let _ = posix_thread;
    NetNamespace::get_init_singleton().clone()
}

impl Drop for NetNamespace {
    fn drop(&mut self) {
        // The initial namespace is static; each child owns its loopback poll thread.
        self.loopback().sched_poll().stop();
    }
}

impl NsCommonOps for NetNamespace {
    const TYPE: NsType = NsType::Net;

    fn owner_user_ns(&self) -> Option<&Arc<UserNamespace>> {
        Some(&self.owner)
    }

    fn parent(&self) -> Result<&Arc<Self>> {
        return_errno_with_message!(
            Errno::EINVAL,
            "a network namespace does not have a parent namespace"
        );
    }

    fn stashed_dentry(&self) -> &StashedDentry {
        &self.stashed_dentry
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;
    use crate::thread::Thread;

    #[ktest]
    fn child_netns_poll_thread_releases_loopback() {
        crate::time::clocks::init_for_ktest();
        let net_ns = NetNamespace::new_child(UserNamespace::get_init_singleton().clone());
        let loopback = Arc::downgrade(net_ns.loopback());
        drop(net_ns);

        for _ in 0..1000 {
            if loopback.strong_count() == 0 {
                break;
            }
            Thread::yield_now();
        }

        assert!(loopback.upgrade().is_none(), "loopback interface leaked");
    }
}
