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
//! Socket bind/connect and netlink route dumps operate on the interface view
//! of the *current* namespace; packet polling of real devices stays global.

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
        })
    }

    /// Returns the interfaces visible in this namespace.
    pub fn ifaces(&self) -> &[Arc<Iface>] {
        &self.ifaces
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

    let ns_proxy = posix_thread.ns_proxy().lock();
    ns_proxy
        .as_ref()
        .map(|proxy| proxy.net_ns().clone())
        .unwrap_or_else(|| NetNamespace::get_init_singleton().clone())
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
