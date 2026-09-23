// SPDX-License-Identifier: MPL-2.0

use alloc::{
    collections::{
        BTreeSet,
        btree_map::{BTreeMap, Entry},
    },
    ffi::CString,
    sync::Arc,
    vec::Vec,
};
use core::{
    ffi::CStr,
    ops::Deref,
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
};

use aster_softirq::BottomHalfDisabled;
use bitflags::bitflags;
use int_to_c_enum::TryFromInt;
use ostd::sync::{SpinLock, SpinLockGuard};
use smoltcp::{
    iface::{Context, packet::Packet},
    phy::Device,
    wire::{IpAddress, IpEndpoint, Ipv4Cidr, Ipv4Packet, Ipv6Address, Ipv6Cidr, Ipv6Packet},
};

use super::{
    Iface, IfaceStats, IfaceStatsSnapshot,
    poll::{FnHelper, PollContext, SocketTableAction},
    poll_iface::PollableIface,
    port::BindPortConfig,
    time::get_network_timestamp,
};
use crate::{
    errors::BindError,
    ext::Ext,
    socket::{TcpListenerBg, UdpSocketBg},
    socket_table::{SocketRegistries, SocketTable, TcpSocketRegistry, UdpSocketRegistry},
    wire::PortNum,
};

/// Configuration shared by all concrete interface constructors.
pub struct IfaceConfig<E: Ext> {
    name: CString,
    type_: InterfaceType,
    flags: InterfaceFlags,
    sched_poll: E::ScheduleNextPoll,
    socket_registries: Arc<SocketRegistries<E>>,
}

impl<E: Ext> IfaceConfig<E> {
    pub fn new(
        name: CString,
        type_: InterfaceType,
        flags: InterfaceFlags,
        sched_poll: E::ScheduleNextPoll,
        socket_registries: Arc<SocketRegistries<E>>,
    ) -> Self {
        Self {
            name,
            type_,
            flags,
            sched_poll,
            socket_registries,
        }
    }
}

pub struct IfaceCommon<E: Ext> {
    index: u32,
    name: CString,
    type_: InterfaceType,
    flags: AtomicU32,
    stats: IfaceStats,

    interface: SpinLock<PollableIface<E>, BottomHalfDisabled>,
    used_ports: SpinLock<PortTable, BottomHalfDisabled>,
    sockets: SpinLock<SocketTable<E>, BottomHalfDisabled>,
    socket_registries: Arc<SocketRegistries<E>>,
    sched_poll: E::ScheduleNextPoll,
}

/// An enum representing either an IPv4 or IPv6 packet.
pub(super) enum IpPacket<'a> {
    Ipv4(Ipv4Packet<&'a [u8]>),
    Ipv6(Ipv6Packet<&'a [u8]>),
}

/// A normalized IP address for binding purposes.
///
/// IPv4 addresses are normalized to IPv4-mapped IPv6 addresses. IPv6 addresses
/// remain unchanged.
///
/// IPv4-mapped IPv6 addresses (`::ffff:x.x.x.x`) should be treated as equivalent
/// to their IPv4 counterparts for binding purposes. This ensures that binding
/// to `192.0.2.1:80` and `::ffff:192.0.2.1:80` are treated as the same.
//
// TCP dual-stack listeners use this namespace to reserve the IPv4-mapped port
// alongside their IPv6 wildcard port. Packet translation and listener matching
// are handled by the TCP poll path.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct NormalizedAddress(Ipv6Address);

impl From<IpAddress> for NormalizedAddress {
    fn from(value: IpAddress) -> Self {
        match value {
            IpAddress::Ipv4(ipv4) => Self(ipv4.to_ipv6_mapped()),
            IpAddress::Ipv6(ipv6) => Self(ipv6),
        }
    }
}

impl<E: Ext> IfaceCommon<E> {
    pub(super) fn new(interface: smoltcp::iface::Interface, config: IfaceConfig<E>) -> Self {
        let IfaceConfig {
            name,
            type_,
            flags,
            sched_poll,
            socket_registries,
        } = config;

        // Linux reserves interface index 1 for the loopback device in every
        // network namespace.  In particular, systemd configures a freshly
        // created namespace by addressing `lo` through the fixed
        // `LOOPBACK_IFINDEX` value instead of resolving its name first.
        //
        // Non-loopback interfaces are still allocated globally for now.  A
        // fresh namespace contains only its own loopback interface, so using
        // index 1 for every loopback is namespace-correct and cannot collide
        // with a visible non-loopback interface.
        let index = if type_ == InterfaceType::LOOPBACK {
            1
        } else {
            INTERFACE_INDEX_ALLOCATOR.fetch_add(1, Ordering::Relaxed)
        };

        Self {
            index,
            name,
            type_,
            flags: AtomicU32::new(flags.bits()),
            stats: IfaceStats::default(),
            interface: SpinLock::new(PollableIface::new(interface)),
            used_ports: SpinLock::new(PortTable::new()),
            sockets: SpinLock::new(SocketTable::new()),
            socket_registries,
            sched_poll,
        }
    }

    pub(super) fn index(&self) -> u32 {
        self.index
    }

    pub(super) fn name(&self) -> &CStr {
        &self.name
    }

    pub(super) fn stats(&self) -> IfaceStatsSnapshot {
        self.stats.snapshot()
    }

    pub(super) fn record_rx(&self, bytes: usize) {
        self.stats.record_rx(bytes);
    }

    pub(super) fn record_tx(&self, bytes: usize) {
        self.stats.record_tx(bytes);
    }

    pub(super) fn type_(&self) -> InterfaceType {
        self.type_
    }

    pub(super) fn flags(&self) -> InterfaceFlags {
        InterfaceFlags::from_bits_truncate(self.flags.load(Ordering::Relaxed))
    }

    pub(super) fn set_flags(&self, flags: InterfaceFlags) {
        self.flags.store(flags.bits(), Ordering::Relaxed);
    }

    pub(super) fn ipv4_cidr(&self) -> Option<Ipv4Cidr> {
        self.interface.lock().ipv4_cidr()
    }

    pub(super) fn ipv6_cidr(&self) -> Option<Ipv6Cidr> {
        self.interface.lock().ipv6_cidr()
    }

    pub(super) fn sched_poll(&self) -> &E::ScheduleNextPoll {
        &self.sched_poll
    }
}

/// An allocator for non-loopback interfaces.
//
// FIXME: This allocator should be specific to each network namespace once
// namespaces can contain non-loopback interfaces.  Index 1 is reserved for
// the namespace-local loopback interface.
static INTERFACE_INDEX_ALLOCATOR: AtomicU32 = AtomicU32::new(2);

// Lock order: `interface` -> `sockets`
impl<E: Ext> IfaceCommon<E> {
    /// Acquires the lock to the interface.
    pub(crate) fn interface(&self) -> SpinLockGuard<'_, PollableIface<E>, BottomHalfDisabled> {
        self.interface.lock()
    }

    /// Acquires the lock to the socket table.
    pub(crate) fn sockets(&self) -> SpinLockGuard<'_, SocketTable<E>, BottomHalfDisabled> {
        self.sockets.lock()
    }

    pub(crate) fn udp_registry(&self) -> &UdpSocketRegistry<E> {
        self.socket_registries.udp()
    }

    pub(crate) fn tcp_registry(&self) -> &TcpSocketRegistry<E> {
        self.socket_registries.tcp()
    }
}

const IP_LOCAL_PORT_START: u16 = 32768;
const IP_LOCAL_PORT_END: u16 = 60999;

impl<E: Ext> IfaceCommon<E> {
    pub(super) fn bind_tcp(
        &self,
        iface: Arc<dyn Iface<E>>,
        config: BindPortConfig,
    ) -> Result<BoundTcpPort<E>, BindError> {
        self.bind(iface, config, PortProtocol::Tcp)
            .map(BoundTcpPort)
    }

    pub(super) fn bind_udp(
        &self,
        iface: Arc<dyn Iface<E>>,
        config: BindPortConfig,
    ) -> Result<BoundUdpPort<E>, BindError> {
        self.bind(iface, config, PortProtocol::Udp)
            .map(BoundUdpPort)
    }

    fn bind(
        &self,
        iface: Arc<dyn Iface<E>>,
        config: BindPortConfig,
        protocol: PortProtocol,
    ) -> Result<BoundPort<E>, BindError> {
        let addr = config.addr();
        let socket_registries = self.socket_registries.clone();
        let (port, can_reuse, registration) = if config.is_backlog() {
            let (port, can_reuse) = self.used_ports.lock().bind(config, protocol, |_| false)?;
            (port, can_reuse, PortRegistration::None)
        } else if config.is_dual_stack() {
            debug_assert!(matches!(addr, IpAddress::Ipv6(addr) if addr.is_unspecified()));
            let bind = |ipv4_ports: &BTreeMap<PortNum, usize>, dual_ports: &[PortNum]| {
                self.used_ports.lock().bind(config, protocol, |port| {
                    ipv4_ports.contains_key(&port) || dual_ports.contains(&port)
                })
            };
            let (port, can_reuse) = match protocol {
                PortProtocol::Tcp => socket_registries.tcp().bind_dual_stack_port(bind),
                PortProtocol::Udp => socket_registries.udp().bind_dual_stack_port(bind),
            }?;
            (port, can_reuse, PortRegistration::DualStack)
        } else if matches!(addr, IpAddress::Ipv4(_)) {
            let bind = |dual_ports: &[PortNum]| {
                self.used_ports
                    .lock()
                    .bind(config, protocol, |port| dual_ports.contains(&port))
            };
            let (port, can_reuse) = match protocol {
                PortProtocol::Tcp => socket_registries.tcp().bind_ipv4_port(bind),
                PortProtocol::Udp => socket_registries.udp().bind_ipv4_port(bind),
            }?;
            (port, can_reuse, PortRegistration::Ipv4)
        } else {
            let (port, can_reuse) = self.used_ports.lock().bind(config, protocol, |_| false)?;
            (port, can_reuse, PortRegistration::None)
        };
        Ok(BoundPort {
            iface,
            addr,
            port,
            protocol,
            registration,
            can_reuse: AtomicBool::new(can_reuse),
        })
    }

    /// Releases the port so that it can be used again.
    fn release_port(&self, addr: IpAddress, port: u16, can_reuse: bool, protocol: PortProtocol) {
        self.used_ports
            .lock()
            .release(addr, port, can_reuse, protocol);
    }
}

impl<E: Ext> IfaceCommon<E> {
    pub(crate) fn register_udp_socket(&self, socket: Arc<UdpSocketBg<E>>) {
        let mut sockets = self.sockets.lock();
        sockets.insert_udp_socket(socket);
    }

    pub(crate) fn remove_tcp_listener(&self, socket: &Arc<TcpListenerBg<E>>) {
        let mut sockets = self.sockets.lock();
        let removed = sockets.remove_listener(socket.listener_key());
        debug_assert!(removed.is_some());
    }

    pub(crate) fn remove_udp_socket(&self, socket: &Arc<UdpSocketBg<E>>) {
        let mut sockets = self.sockets.lock();
        let removed = sockets.remove_udp_socket(socket);
        debug_assert!(removed.is_some());
    }
}

impl<E: Ext> IfaceCommon<E> {
    pub(super) fn poll<D, P, Q>(
        &self,
        device: &mut D,
        mut process_phy: P,
        mut dispatch_phy: Q,
    ) -> Option<u64>
    where
        D: Device + ?Sized,
        P: for<'pkt, 'cx, 'tx> FnHelper<
                &'pkt [u8],
                &'cx mut Context,
                D::TxToken<'tx>,
                Option<(IpPacket<'pkt>, D::TxToken<'tx>)>,
            >,
        Q: FnMut(&Packet, &mut Context, D::TxToken<'_>),
    {
        let mut interface = self.interface();
        interface.context_mut().now = get_network_timestamp();

        let mut sockets = self.sockets.lock();
        let mut socket_actions = Vec::new();

        let mut context = PollContext::new(
            interface.as_mut(),
            &sockets,
            self.socket_registries.udp(),
            self.socket_registries.tcp(),
            self.index,
            (self.type_ == InterfaceType::LOOPBACK).then_some(&self.stats),
            &mut socket_actions,
        );
        context.poll_ingress(device, &mut process_phy, &mut dispatch_phy);
        context.poll_egress(device, &mut dispatch_phy);

        // Insert new connections and remove dead connections.
        for action in socket_actions.into_iter() {
            match action {
                SocketTableAction::AddTcpConn(new_tcp_conn) => {
                    let res = sockets.insert_connection(new_tcp_conn);
                    debug_assert!(res.is_ok());
                }
                SocketTableAction::DelTcpConn(dead_conn_key) => {
                    sockets.remove_dead_tcp_connection(&dead_conn_key);
                }
            }
        }

        // Note that only TCP connections can have timers set, so as far as the time to poll is
        // concerned, we only need to consider TCP connections.
        interface.next_poll_at_ms()
    }
}

/// A port bound to an iface.
///
/// When dropped, the port is automatically released.
pub struct BoundPort<E: Ext> {
    iface: Arc<dyn Iface<E>>,
    addr: IpAddress,
    port: u16,
    protocol: PortProtocol,
    registration: PortRegistration,
    can_reuse: AtomicBool,
}

#[derive(Clone, Copy)]
enum PortRegistration {
    None,
    Ipv4,
    DualStack,
}

impl<E: Ext> BoundPort<E> {
    /// Returns a reference to the iface.
    pub fn iface(&self) -> &Arc<dyn Iface<E>> {
        &self.iface
    }

    /// Returns the port number.
    pub fn port(&self) -> u16 {
        self.port
    }

    /// Returns the bound IP address.
    pub fn addr(&self) -> &IpAddress {
        &self.addr
    }

    /// Returns the bound endpoint.
    pub fn endpoint(&self) -> IpEndpoint {
        IpEndpoint::new(self.addr, self.port)
    }

    /// Sets whether the port can be reused.
    pub fn set_can_reuse(&self, can_reuse: bool) {
        let iface_common = self.iface.common();
        let mut used_ports = iface_common.used_ports.lock();

        // Check after locking `used_ports` to avoid race conditions.
        if self.can_reuse.load(Ordering::Relaxed) == can_reuse {
            return;
        }

        let key = PortKey {
            addr: NormalizedAddress::from(self.addr),
            port: self.port,
            protocol: self.protocol,
        };
        used_ports.set_can_reuse(key, can_reuse);

        self.can_reuse.store(can_reuse, Ordering::Relaxed);
    }
}

impl<E: Ext> Drop for BoundPort<E> {
    fn drop(&mut self) {
        let common = self.iface.common();
        match (self.protocol, self.registration) {
            (_, PortRegistration::None) => {}
            (PortProtocol::Tcp, PortRegistration::Ipv4) => {
                common.tcp_registry().unregister_ipv4_port(self.port)
            }
            (PortProtocol::Tcp, PortRegistration::DualStack) => {
                common.tcp_registry().unregister_dual_stack_port(self.port)
            }
            (PortProtocol::Udp, PortRegistration::Ipv4) => {
                common.udp_registry().unregister_ipv4_port(self.port)
            }
            (PortProtocol::Udp, PortRegistration::DualStack) => {
                common.udp_registry().unregister_dual_stack_port(self.port)
            }
        }

        // Keep the namespace registry -> interface port table lock order used
        // during bind.
        self.iface.common().release_port(
            self.addr,
            self.port,
            *self.can_reuse.get_mut(),
            self.protocol,
        );
    }
}

/// A TCP port bound to an iface.
pub struct BoundTcpPort<E: Ext>(BoundPort<E>);
/// A UDP port bound to an iface.
pub struct BoundUdpPort<E: Ext>(BoundPort<E>);

impl<E: Ext> Deref for BoundTcpPort<E> {
    type Target = BoundPort<E>;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl<E: Ext> Deref for BoundUdpPort<E> {
    type Target = BoundPort<E>;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct PortKey {
    addr: NormalizedAddress,
    port: u16,
    protocol: PortProtocol,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum PortProtocol {
    Tcp,
    Udp,
}

struct PortState {
    nsocket: usize,
    /// The number of sockets that have enabled address reuse on this port.
    nreuse: usize,
}

impl PortState {
    pub(self) fn new(can_reuse: bool) -> Self {
        let nreuse = if can_reuse { 1 } else { 0 };
        Self { nsocket: 1, nreuse }
    }

    pub(self) fn can_reuse(&self) -> bool {
        self.nsocket == self.nreuse
    }
}

struct PortTable {
    used_ports: BTreeMap<PortKey, PortState>,
    /// Addresses indexed by port and protocol for bounded conflict checks.
    ///
    /// Each address occurs exactly when the corresponding key occurs in
    /// `used_ports`; multiple reusable owners still occupy one address entry.
    port_addresses: BTreeMap<(u16, PortProtocol), BTreeSet<NormalizedAddress>>,
    next_ephemeral_port: u16,
}

impl PortTable {
    fn new() -> Self {
        Self {
            used_ports: BTreeMap::new(),
            port_addresses: BTreeMap::new(),
            next_ephemeral_port: IP_LOCAL_PORT_START,
        }
    }

    fn bind(
        &mut self,
        config: BindPortConfig,
        protocol: PortProtocol,
        external_conflict: impl Fn(u16) -> bool,
    ) -> Result<(u16, bool), BindError> {
        let config_can_reuse = config.can_reuse();
        let addr = NormalizedAddress::from(config.addr());

        let port = if let Some(port) = config.port() {
            if !config.is_backlog() && external_conflict(port) {
                return Err(BindError::InUse);
            }
            port
        } else {
            match self.alloc_ephemeral_port(addr, protocol, config_can_reuse, external_conflict) {
                Some(port) => port,
                None => return Err(BindError::Exhausted),
            }
        };

        if !config.is_backlog() {
            if let Some(addresses) = self.port_addresses.get(&(port, protocol)) {
                for existing_addr in addresses {
                    if !port_addresses_conflict(*existing_addr, addr) {
                        continue;
                    }
                    let existing = PortKey {
                        addr: *existing_addr,
                        port,
                        protocol,
                    };
                    let Some(state) = self.used_ports.get(&existing) else {
                        debug_assert!(false, "port address index must mirror the port table");
                        return Err(BindError::InUse);
                    };
                    if !config_can_reuse || !state.can_reuse() {
                        return Err(BindError::InUse);
                    }
                }
            }
        }

        let key = PortKey {
            addr,
            port,
            protocol,
        };
        let entry = self.used_ports.entry(key);
        match entry {
            Entry::Occupied(mut occupied) => {
                let port_state = occupied.get_mut();
                // FIXME: If the socket is not a backlog socket,
                // we should check whether there is a listening socket on the port.
                // If there is, the socket cannot be bound to that port.
                let can_reuse = config.is_backlog() || (port_state.can_reuse() & config_can_reuse);
                if can_reuse {
                    port_state.nsocket += 1;
                    if config_can_reuse {
                        port_state.nreuse += 1;
                    }
                } else {
                    return Err(BindError::InUse);
                }
            }
            Entry::Vacant(vacant) => {
                let port_state = PortState::new(config_can_reuse);
                vacant.insert(port_state);
            }
        };

        self.port_addresses
            .entry((port, protocol))
            .or_default()
            .insert(addr);

        Ok((port, config_can_reuse))
    }

    /// Allocates an ephemeral port.
    ///
    /// We follow the port range that many Linux kernels use by default, which is 32768-60999.
    ///
    /// Each allocation starts scanning from the port immediately
    /// after the last successfully allocated ephemeral port.
    /// This ensures that a recently released port is not immediately
    /// reused by a new socket, which avoids potential port conflicts.
    ///
    /// See <https://en.wikipedia.org/wiki/Ephemeral_port>.
    fn alloc_ephemeral_port(
        &mut self,
        addr: NormalizedAddress,
        protocol: PortProtocol,
        _can_reuse: bool,
        external_conflict: impl Fn(u16) -> bool,
    ) -> Option<u16> {
        const fn next_ephemeral_port_after(port: u16) -> u16 {
            if port >= IP_LOCAL_PORT_END {
                IP_LOCAL_PORT_START
            } else {
                port + 1
            }
        }

        let start_port = self.next_ephemeral_port;
        let mut port = start_port;
        loop {
            let address_conflict =
                self.port_addresses
                    .get(&(port, protocol))
                    .is_some_and(|addresses| {
                        addresses
                            .iter()
                            .any(|existing| port_addresses_conflict(*existing, addr))
                    });
            if !external_conflict(port) && !address_conflict {
                self.next_ephemeral_port = next_ephemeral_port_after(port);
                return Some(port);
            }

            port = next_ephemeral_port_after(port);
            if port == start_port {
                break;
            }
        }

        // FIXME: If `can_reuse` is `true`, we should also check all in-use ephemeral ports
        // to see if any can be reused instead of directly returning `None`.

        None
    }

    fn release(&mut self, addr: IpAddress, port: u16, can_reuse: bool, protocol: PortProtocol) {
        let key = PortKey {
            addr: NormalizedAddress::from(addr),
            port,
            protocol,
        };
        let Entry::Occupied(mut occupied) = self.used_ports.entry(key) else {
            return;
        };

        let port_state = occupied.get_mut();
        port_state.nsocket -= 1;
        if can_reuse {
            port_state.nreuse -= 1;
        }
        if port_state.nsocket == 0 {
            occupied.remove();
            if let Some(addresses) = self.port_addresses.get_mut(&(port, protocol)) {
                let removed = addresses.remove(&key.addr);
                debug_assert!(removed, "port address index must mirror the port table");
                if addresses.is_empty() {
                    self.port_addresses.remove(&(port, protocol));
                }
            } else {
                debug_assert!(false, "port address index must mirror the port table");
            }
        }
    }

    fn set_can_reuse(&mut self, key: PortKey, can_reuse: bool) {
        let Some(port_state) = self.used_ports.get_mut(&key) else {
            return;
        };

        if can_reuse {
            port_state.nreuse += 1;
        } else {
            port_state.nreuse -= 1;
        }
    }
}

/// Returns whether two normalized addresses share the same bind namespace.
///
/// IPv4 addresses are stored as IPv4-mapped IPv6 values, so the mapped family
/// must be kept separate from native IPv6 when applying wildcard rules.
fn port_addresses_conflict(left: NormalizedAddress, right: NormalizedAddress) -> bool {
    let left_bits = left.0.to_bits();
    let right_bits = right.0.to_bits();
    let left_is_v4 = left_bits >> 32 == 0xffff;
    let right_is_v4 = right_bits >> 32 == 0xffff;
    if left_is_v4 != right_is_v4 {
        return false;
    }
    if left_is_v4 {
        left_bits == right_bits || left_bits & 0xffff_ffff == 0 || right_bits & 0xffff_ffff == 0
    } else {
        left_bits == right_bits || left_bits == 0 || right_bits == 0
    }
}

#[cfg(ktest)]
mod port_table_tests {
    use ostd::prelude::*;
    use smoltcp::wire::{IpAddress, IpEndpoint, Ipv4Address};

    use super::{BindError, BindPortConfig, NormalizedAddress, PortProtocol, PortTable};

    const PORT: u16 = 40_000;

    fn ipv4(last_octet: u8) -> IpAddress {
        IpAddress::Ipv4(Ipv4Address::new(192, 0, 2, last_octet))
    }

    fn bind_config(address: IpAddress, port: u16, can_reuse: bool) -> BindPortConfig {
        BindPortConfig::new(IpEndpoint::new(address, port), can_reuse)
    }

    fn no_external_conflict(_: u16) -> bool {
        false
    }

    #[ktest]
    fn explicit_bind_requires_reuse_from_every_conflicting_owner() {
        for (first_reuse, second_reuse, expected) in [
            (false, false, Err(BindError::InUse)),
            (false, true, Err(BindError::InUse)),
            (true, false, Err(BindError::InUse)),
            (true, true, Ok((PORT, true))),
        ] {
            let mut table = PortTable::new();
            table
                .bind(
                    bind_config(ipv4(1), PORT, first_reuse),
                    PortProtocol::Tcp,
                    no_external_conflict,
                )
                .unwrap();

            assert_eq!(
                table.bind(
                    bind_config(ipv4(1), PORT, second_reuse),
                    PortProtocol::Tcp,
                    no_external_conflict,
                ),
                expected,
            );
        }
    }

    #[ktest]
    fn address_index_tracks_first_bind_and_last_release() {
        let address = ipv4(1);
        let normalized = NormalizedAddress::from(address);
        let mut table = PortTable::new();

        table
            .bind(
                bind_config(address, PORT, true),
                PortProtocol::Udp,
                no_external_conflict,
            )
            .unwrap();
        table
            .bind(
                bind_config(address, PORT, true),
                PortProtocol::Udp,
                no_external_conflict,
            )
            .unwrap();
        assert_eq!(
            table.port_addresses.get(&(PORT, PortProtocol::Udp)),
            Some(&[normalized].into_iter().collect()),
        );

        table.release(address, PORT, true, PortProtocol::Udp);
        assert!(
            table
                .port_addresses
                .contains_key(&(PORT, PortProtocol::Udp))
        );
        table.release(address, PORT, true, PortProtocol::Udp);
        assert!(
            !table
                .port_addresses
                .contains_key(&(PORT, PortProtocol::Udp))
        );
    }

    #[ktest]
    fn ephemeral_bind_uses_address_index_to_skip_wildcard_conflict() {
        let mut table = PortTable::new();
        table
            .bind(
                bind_config(IpAddress::Ipv4(Ipv4Address::UNSPECIFIED), PORT, false),
                PortProtocol::Tcp,
                no_external_conflict,
            )
            .unwrap();
        table.next_ephemeral_port = PORT;

        let (allocated, _) = table
            .bind(
                bind_config(ipv4(1), 0, false),
                PortProtocol::Tcp,
                no_external_conflict,
            )
            .unwrap();

        assert_eq!(allocated, PORT + 1);
    }
}

/// Interface type.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.0.18/source/include/uapi/linux/if_arp.h#L30>
#[repr(u16)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, TryFromInt)]
pub enum InterfaceType {
    // Arp protocol hardware identifiers
    /// from KA9Q: NET/ROM pseudo
    NETROM = 0,
    /// Ethernet 10Mbps
    ETHER = 1,
    /// Experimental Ethernet
    EETHER = 2,

    // Dummy types for non ARP hardware
    /// IPIP tunnel
    TUNNEL = 768,
    /// IP6IP6 tunnel
    TUNNEL6 = 769,
    /// Frame Relay Access Device
    FRAD = 770,
    /// SKIP vif
    SKIP = 771,
    /// Loopback device
    LOOPBACK = 772,
    /// Localtalk device
    LOCALTALK = 773,
    // TODO: This enum is not exhaustive
}

bitflags! {
    /// Interface flags.
    ///
    /// Reference: <https://elixir.bootlin.com/linux/v6.0.18/source/include/uapi/linux/if.h#L82>
    pub struct InterfaceFlags: u32 {
        /// Interface is up
        const UP				= 1<<0;
        /// Broadcast address valid
        const BROADCAST			= 1<<1;
        /// Turn on debugging
        const DEBUG			    = 1<<2;
        /// Loopback net
        const LOOPBACK			= 1<<3;
        /// Interface is has p-p link
        const POINTOPOINT		= 1<<4;
        /// Avoid use of trailers
        const NOTRAILERS		= 1<<5;
        /// Interface RFC2863 OPER_UP
        const RUNNING			= 1<<6;
        /// No ARP protocol
        const NOARP			    = 1<<7;
        /// Receive all packets
        const PROMISC			= 1<<8;
        /// Receive all multicast packets
        const ALLMULTI			= 1<<9;
        /// Master of a load balancer
        const MASTER			= 1<<10;
        /// Slave of a load balancer
        const SLAVE			    = 1<<11;
        /// Supports multicast
        const MULTICAST			= 1<<12;
        /// Can set media type
        const PORTSEL			= 1<<13;
        /// Auto media select active
        const AUTOMEDIA			= 1<<14;
        /// Dialup device with changing addresses
        const DYNAMIC			= 1<<15;
        /// Driver signals L1 up
        const LOWER_UP			= 1<<16;
        /// Driver signals dormant
        const DORMANT			= 1<<17;
        /// Echo sent packets
        const ECHO			    = 1<<18;
    }
}
