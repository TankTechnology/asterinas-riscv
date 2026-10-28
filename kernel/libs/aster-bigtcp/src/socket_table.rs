// SPDX-License-Identifier: MPL-2.0

//! This module defines the socket table, which manages all TCP and UDP sockets,
//! for efficiently inserting, looking up, and removing sockets.

use alloc::{
    boxed::Box,
    collections::btree_map::{BTreeMap, Entry},
    sync::{Arc, Weak},
    vec::Vec,
};
use core::net::Ipv4Addr;

use aster_softirq::BottomHalfDisabled;
use jhash::{jhash_1vals, jhash_3vals, jhash_u32_array};
use ostd::{const_assert, sync::SpinLock};
use smoltcp::wire::{IpAddress, IpEndpoint, IpListenEndpoint};

use crate::{
    errors::BindError,
    ext::Ext,
    iface::Iface,
    socket::{TcpConnectionBg, TcpListenerBg, UdpSocketBg},
    wire::PortNum,
};

pub type SocketHash = u32;

struct DualStackPortRegistry {
    state: SpinLock<DualStackPortState, BottomHalfDisabled>,
}

struct DualStackPortState {
    dual_ports: Vec<PortNum>,
    ipv4_ports: BTreeMap<PortNum, usize>,
}

impl DualStackPortRegistry {
    fn new() -> Self {
        Self {
            state: SpinLock::new(DualStackPortState {
                dual_ports: Vec::new(),
                ipv4_ports: BTreeMap::new(),
            }),
        }
    }

    fn bind_ipv4(
        &self,
        bind: impl FnOnce(&[PortNum]) -> Result<(PortNum, bool), BindError>,
    ) -> Result<(PortNum, bool), BindError> {
        let mut state = self.state.lock();
        let result = bind(&state.dual_ports)?;
        *state.ipv4_ports.entry(result.0).or_default() += 1;
        Ok(result)
    }

    fn bind_dual_stack(
        &self,
        bind: impl FnOnce(&BTreeMap<PortNum, usize>, &[PortNum]) -> Result<(PortNum, bool), BindError>,
    ) -> Result<(PortNum, bool), BindError> {
        let mut state = self.state.lock();
        let result = bind(&state.ipv4_ports, &state.dual_ports)?;
        state.dual_ports.push(result.0);
        Ok(result)
    }

    fn unregister_ipv4(&self, port: PortNum) {
        let mut state = self.state.lock();
        let Entry::Occupied(mut entry) = state.ipv4_ports.entry(port) else {
            debug_assert!(false, "IPv4 port was not registered");
            return;
        };
        *entry.get_mut() -= 1;
        if *entry.get() == 0 {
            entry.remove();
        }
    }

    fn unregister_dual_stack(&self, port: PortNum) {
        let mut state = self.state.lock();
        let Some(index) = state.dual_ports.iter().position(|value| *value == port) else {
            debug_assert!(false, "dual-stack port was not registered");
            return;
        };
        state.dual_ports.swap_remove(index);
    }
}

/// Socket registries shared by every interface in one network environment.
pub struct SocketRegistries<E: Ext> {
    udp: UdpSocketRegistry<E>,
    tcp: TcpSocketRegistry<E>,
}

impl<E: Ext> SocketRegistries<E> {
    pub fn new() -> Self {
        Self {
            udp: UdpSocketRegistry::new(),
            tcp: TcpSocketRegistry::new(),
        }
    }

    /// Registers an interface in this registry's network environment.
    pub fn register_iface(&self, iface: &Arc<dyn Iface<E>>) {
        self.tcp.register_iface(iface);
    }

    pub(crate) const fn udp(&self) -> &UdpSocketRegistry<E> {
        &self.udp
    }

    pub(crate) const fn tcp(&self) -> &TcpSocketRegistry<E> {
        &self.tcp
    }
}

impl<E: Ext> Default for SocketRegistries<E> {
    fn default() -> Self {
        Self::new()
    }
}

/// UDP socket registry shared by all interfaces in one network environment.
///
/// Socket ownership and egress scheduling remain local to an interface. The
/// registry only provides weak-reference lookup for wildcard sockets when a
/// packet arrives through another interface.
pub struct UdpSocketRegistry<E: Ext> {
    state: SpinLock<UdpSocketRegistryState<E>, BottomHalfDisabled>,
    ports: DualStackPortRegistry,
}

struct UdpSocketRegistryState<E: Ext> {
    sockets: BTreeMap<PortNum, Vec<Weak<UdpSocketBg<E>>>>,
}

impl<E: Ext> UdpSocketRegistry<E> {
    pub fn new() -> Self {
        Self {
            state: SpinLock::new(UdpSocketRegistryState {
                sockets: BTreeMap::new(),
            }),
            ports: DualStackPortRegistry::new(),
        }
    }

    pub(crate) fn register_socket(&self, socket: &Arc<UdpSocketBg<E>>) {
        self.state
            .lock()
            .sockets
            .entry(socket.local_port())
            .or_default()
            .push(Arc::downgrade(socket));
    }

    pub(crate) fn unregister_socket(&self, socket: &Arc<UdpSocketBg<E>>) {
        let port = socket.local_port();
        let mut state = self.state.lock();
        let Some(entries) = state.sockets.get_mut(&port) else {
            return;
        };
        entries.retain(|entry| {
            entry
                .upgrade()
                .is_some_and(|existing| !Arc::ptr_eq(&existing, socket))
        });
        if entries.is_empty() {
            state.sockets.remove(&port);
        }
    }

    pub(crate) fn sockets_for_port(&self, port: PortNum) -> Vec<Arc<UdpSocketBg<E>>> {
        let mut state = self.state.lock();
        let mut sockets = Vec::new();
        let mut remove_port = false;
        if let Some(entries) = state.sockets.get_mut(&port) {
            entries.retain(|entry| {
                let Some(socket) = entry.upgrade() else {
                    return false;
                };
                sockets.push(socket);
                true
            });
            remove_port = entries.is_empty();
        }
        if remove_port {
            state.sockets.remove(&port);
        }
        sockets
    }

    pub(crate) fn bind_ipv4_port(
        &self,
        bind: impl FnOnce(&[PortNum]) -> Result<(PortNum, bool), BindError>,
    ) -> Result<(PortNum, bool), BindError> {
        self.ports.bind_ipv4(bind)
    }

    pub(crate) fn bind_dual_stack_port(
        &self,
        bind: impl FnOnce(&BTreeMap<PortNum, usize>, &[PortNum]) -> Result<(PortNum, bool), BindError>,
    ) -> Result<(PortNum, bool), BindError> {
        self.ports.bind_dual_stack(bind)
    }

    pub(crate) fn unregister_ipv4_port(&self, port: PortNum) {
        self.ports.unregister_ipv4(port);
    }

    pub(crate) fn unregister_dual_stack_port(&self, port: PortNum) {
        self.ports.unregister_dual_stack(port);
    }
}

impl<E: Ext> Default for UdpSocketRegistry<E> {
    fn default() -> Self {
        Self::new()
    }
}

/// A unique key for identifying a `TcpListener`.
///
/// Note that two `TcpListener`s cannot listen on the same address
/// even if both sockets set SO_REUSEADDR to true,
/// so there cannot be multiple listeners with the same `ListenerKey`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct ListenerKey {
    addr: IpAddress,
    port: PortNum,
    hash: SocketHash,
}

impl ListenerKey {
    pub(crate) const fn new(addr: IpAddress, port: PortNum) -> Self {
        // FIXME: If the socket is listening on an unspecified address (0.0.0.0),
        // Linux will get the hash value by port only.
        let hash = hash_addr_port(addr, port);
        Self { addr, port, hash }
    }

    pub(crate) const fn hash(&self) -> SocketHash {
        self.hash
    }
}

impl From<IpListenEndpoint> for ListenerKey {
    fn from(listen_endpoint: IpListenEndpoint) -> Self {
        let addr = listen_endpoint
            .addr
            .unwrap_or(IpAddress::Ipv4(Ipv4Addr::UNSPECIFIED));
        let port = listen_endpoint.port;
        Self::new(addr, port)
    }
}

/// A unique key for identifying a `TcpConnection`.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) struct ConnectionKey {
    local_addr: IpAddress,
    local_port: PortNum,
    remote_addr: IpAddress,
    remote_port: PortNum,
    hash: SocketHash,
}

impl ConnectionKey {
    pub(crate) fn new(
        local_addr: IpAddress,
        local_port: PortNum,
        remote_addr: IpAddress,
        remote_port: PortNum,
    ) -> Self {
        // Keep IPv4 and IPv4-mapped IPv6 tuples in the same connection
        // namespace. This lets a dual-stack listener find later IPv4 packets
        // after the first SYN has been represented as a mapped IPv6 endpoint.
        let local_addr = normalize_connection_addr(local_addr);
        let remote_addr = normalize_connection_addr(remote_addr);
        let hash = hash_local_remote(local_addr, local_port, remote_addr, remote_port);
        Self {
            local_addr,
            local_port,
            remote_addr,
            remote_port,
            hash,
        }
    }

    pub(crate) const fn hash(&self) -> SocketHash {
        self.hash
    }

    pub(crate) const fn local_port(&self) -> PortNum {
        self.local_port
    }

    pub(crate) const fn remote_port(&self) -> PortNum {
        self.remote_port
    }
}

fn normalize_connection_addr(addr: IpAddress) -> IpAddress {
    match addr {
        IpAddress::Ipv4(addr) => IpAddress::Ipv6(addr.to_ipv6_mapped()),
        IpAddress::Ipv6(addr) => IpAddress::Ipv6(addr),
    }
}

impl From<(IpEndpoint, IpEndpoint)> for ConnectionKey {
    fn from(value: (IpEndpoint, IpEndpoint)) -> Self {
        Self::new(value.0.addr, value.0.port, value.1.addr, value.1.port)
    }
}

/// TCP socket registry shared by all interfaces in one network environment.
///
/// Socket ownership and polling remain interface-local. This weak registry
/// supplies namespace-wide listener lookup for wildcard sockets whose traffic
/// arrives or loops back through another interface.
pub struct TcpSocketRegistry<E: Ext> {
    state: SpinLock<TcpSocketRegistryState<E>, BottomHalfDisabled>,
    ports: DualStackPortRegistry,
}

struct TcpSocketRegistryState<E: Ext> {
    listeners: BTreeMap<PortNum, Vec<Weak<TcpListenerBg<E>>>>,
    ifaces: BTreeMap<u32, Weak<dyn Iface<E>>>,
}

impl<E: Ext> TcpSocketRegistry<E> {
    pub fn new() -> Self {
        Self {
            state: SpinLock::new(TcpSocketRegistryState {
                listeners: BTreeMap::new(),
                ifaces: BTreeMap::new(),
            }),
            ports: DualStackPortRegistry::new(),
        }
    }

    fn register_iface(&self, iface: &Arc<dyn Iface<E>>) {
        self.state
            .lock()
            .ifaces
            .insert(iface.index(), Arc::downgrade(iface));
    }

    pub(crate) fn iface(&self, index: u32) -> Option<Arc<dyn Iface<E>>> {
        self.state.lock().ifaces.get(&index).and_then(Weak::upgrade)
    }

    pub(crate) fn register_listener(&self, listener: &Arc<TcpListenerBg<E>>) {
        self.state
            .lock()
            .listeners
            .entry(listener.listener_key().port)
            .or_default()
            .push(Arc::downgrade(listener));
    }

    pub(crate) fn unregister_listener(&self, listener: &Arc<TcpListenerBg<E>>) {
        let port = listener.listener_key().port;
        let mut state = self.state.lock();
        let Some(entries) = state.listeners.get_mut(&port) else {
            return;
        };
        entries.retain(|entry| {
            entry
                .upgrade()
                .is_some_and(|existing| !Arc::ptr_eq(&existing, listener))
        });
        if entries.is_empty() {
            state.listeners.remove(&port);
        }
    }

    pub(crate) fn lookup_listener(&self, key: &ListenerKey) -> Option<Arc<TcpListenerBg<E>>> {
        let mut state = self.state.lock();
        let mut listeners = Vec::new();
        let mut remove_port = false;
        if let Some(entries) = state.listeners.get_mut(&key.port) {
            entries.retain(|entry| {
                let Some(listener) = entry.upgrade() else {
                    return false;
                };
                listeners.push(listener);
                true
            });
            remove_port = entries.is_empty();
        }
        if remove_port {
            state.listeners.remove(&key.port);
        }
        drop(state);

        if let Some(listener) = listeners
            .iter()
            .find(|listener| !listener.is_closed() && listener.listener_key() == key)
        {
            return Some(listener.clone());
        }

        let wildcard_addr = match key.addr {
            IpAddress::Ipv4(_) => IpAddress::Ipv4(Ipv4Addr::UNSPECIFIED),
            IpAddress::Ipv6(_) => IpAddress::Ipv6(core::net::Ipv6Addr::UNSPECIFIED),
        };
        let wildcard_key = ListenerKey::new(wildcard_addr, key.port);
        if let Some(listener) = listeners
            .iter()
            .find(|listener| !listener.is_closed() && listener.listener_key() == &wildcard_key)
        {
            return Some(listener.clone());
        }

        if !matches!(key.addr, IpAddress::Ipv4(_)) {
            return None;
        }
        let v6_wildcard =
            ListenerKey::new(IpAddress::Ipv6(core::net::Ipv6Addr::UNSPECIFIED), key.port);
        listeners.into_iter().find(|listener| {
            !listener.is_closed()
                && listener.listener_key() == &v6_wildcard
                && listener.accepts_ipv4()
        })
    }

    pub(crate) fn bind_ipv4_port(
        &self,
        bind: impl FnOnce(&[PortNum]) -> Result<(PortNum, bool), BindError>,
    ) -> Result<(PortNum, bool), BindError> {
        self.ports.bind_ipv4(bind)
    }

    pub(crate) fn bind_dual_stack_port(
        &self,
        bind: impl FnOnce(&BTreeMap<PortNum, usize>, &[PortNum]) -> Result<(PortNum, bool), BindError>,
    ) -> Result<(PortNum, bool), BindError> {
        self.ports.bind_dual_stack(bind)
    }

    pub(crate) fn unregister_ipv4_port(&self, port: PortNum) {
        self.ports.unregister_ipv4(port);
    }

    pub(crate) fn unregister_dual_stack_port(&self, port: PortNum) {
        self.ports.unregister_dual_stack(port);
    }
}

impl<E: Ext> Default for TcpSocketRegistry<E> {
    fn default() -> Self {
        Self::new()
    }
}

// FIXME: The following two constants should be randomly-generated at runtime
const HASH_SECRET: u32 = 0xdeadbeef;

// FIXME: This constant should be a per-net-namespace value
const NET_HASHMIX: u32 = 0xbeefdead;

const fn hash_local_remote(
    local_addr: IpAddress,
    local_port: PortNum,
    remote_addr: IpAddress,
    remote_port: PortNum,
) -> SocketHash {
    match (local_addr, remote_addr) {
        (IpAddress::Ipv4(local_ipv4), IpAddress::Ipv4(remote_ipv4)) => jhash_3vals(
            local_ipv4.to_bits(),
            remote_ipv4.to_bits(),
            (local_port as u32).wrapping_shl(16) | remote_port as u32,
            HASH_SECRET.wrapping_add(NET_HASHMIX),
        ),
        (IpAddress::Ipv6(local_ipv6), IpAddress::Ipv6(remote_ipv6)) => {
            let local_bits = local_ipv6.to_bits();
            let remote_bits = remote_ipv6.to_bits();
            let hash_keys = &[
                (local_bits >> 96) as u32,
                (local_bits >> 64) as u32,
                (local_bits >> 32) as u32,
                local_bits as u32,
                (remote_bits >> 96) as u32,
                (remote_bits >> 64) as u32,
                (remote_bits >> 32) as u32,
                remote_bits as u32,
                (local_port as u32).wrapping_shl(16) | remote_port as u32,
            ];
            jhash_u32_array(hash_keys, HASH_SECRET.wrapping_add(NET_HASHMIX))
        }
        _ => panic!("cannot mix IPv4 and IPv6 addresses"),
    }
}

const fn hash_addr_port(addr: IpAddress, port: PortNum) -> SocketHash {
    match addr {
        IpAddress::Ipv4(ipv4_addr) => jhash_1vals(ipv4_addr.to_bits(), NET_HASHMIX) ^ (port as u32),
        IpAddress::Ipv6(ipv6_addr) => {
            let bits = ipv6_addr.to_bits();
            let hash_keys = &[
                (bits >> 96) as u32,
                (bits >> 64) as u32,
                (bits >> 32) as u32,
                bits as u32,
                port as u32,
            ];
            jhash_u32_array(hash_keys, NET_HASHMIX)
        }
    }
}

/// The socket table manages TCP and UDP sockets.
///
/// Unlike the Linux inet hashtable, which is shared across a single network namespace,
/// this table is currently limited to a single interface.
///
// TODO: Modify the table to be shared across a single network namespace
// to support INADDR_ANY (0.0.0.0).
pub(crate) struct SocketTable<E: Ext> {
    // TODO: Linux has two hashtables for listeners:
    // the first is hashed by local address and port,
    // the second is hashed by local port only.
    // The second table is the only place where sockets listening on INADDR_ANY (0.0.0.0) can exist.
    // Since we do not yet support INADDR_ANY, we only have the first table here.
    listener_buckets: Box<[ListenerHashBucket<E>]>,
    connection_buckets: Box<[ConnectionHashBucket<E>]>,
    // Linux does not include UDP sockets in the inet hashtable.
    // Here we include UDP sockets in the socket table for simplicity.
    // Note that multiple UDP sockets can be bound to the same address,
    // so we cannot use (addr, port) as a _unique_ key for UDP sockets.
    udp_sockets: Vec<Arc<UdpSocketBg<E>>>,
}

// On Linux, the number of buckets is determined at runtime based on the available memory.
// For simplicity, we use fixed values here.
// The bucket count should be a power of 2 to ensure efficient modulo calculations.
const LISTENER_BUCKET_COUNT: u32 = 64;
const LISTENER_BUCKET_MASK: u32 = LISTENER_BUCKET_COUNT - 1;
const CONNECTION_BUCKET_COUNT: u32 = 8192;
const CONNECTION_BUCKET_MASK: u32 = CONNECTION_BUCKET_COUNT - 1;

const_assert!(LISTENER_BUCKET_COUNT.is_power_of_two());
const_assert!(CONNECTION_BUCKET_COUNT.is_power_of_two());

impl<E: Ext> SocketTable<E> {
    pub(crate) fn new() -> Self {
        let listener_buckets = (0..LISTENER_BUCKET_COUNT)
            .map(|_| ListenerHashBucket::new())
            .collect();

        let connection_buckets = (0..CONNECTION_BUCKET_COUNT)
            .map(|_| ConnectionHashBucket::new())
            .collect();

        let udp_sockets = Vec::new();

        Self {
            listener_buckets,
            connection_buckets,
            udp_sockets,
        }
    }

    /// Inserts a TCP listener into the table.
    ///
    /// If a socket with the same [`ListenerKey`] has already been inserted,
    /// this method will return an error and the listener will not be inserted.
    pub(crate) fn insert_listener(
        &mut self,
        listener: Arc<TcpListenerBg<E>>,
    ) -> Result<(), Arc<TcpListenerBg<E>>> {
        let key = listener.listener_key();

        let bucket = {
            let hash = key.hash();
            let bucket_index = hash & LISTENER_BUCKET_MASK;
            &mut self.listener_buckets[bucket_index as usize]
        };

        if bucket
            .listeners
            .iter()
            .any(|tcp_listener| tcp_listener.listener_key() == listener.listener_key())
        {
            return Err(listener);
        }

        bucket.listeners.push(listener);
        Ok(())
    }

    pub(crate) fn insert_connection(
        &mut self,
        connection: Arc<TcpConnectionBg<E>>,
    ) -> Result<(), Arc<TcpConnectionBg<E>>> {
        let key = connection.connection_key();

        let bucket = {
            let hash = key.hash();
            let bucket_index = hash & CONNECTION_BUCKET_MASK;
            &mut self.connection_buckets[bucket_index as usize]
        };

        if bucket
            .connections
            .iter()
            .any(|tcp_connection| tcp_connection.connection_key() == connection.connection_key())
        {
            return Err(connection);
        }

        bucket.connections.push(connection);
        Ok(())
    }

    pub(crate) fn insert_udp_socket(&mut self, udp_socket: Arc<UdpSocketBg<E>>) {
        debug_assert!(
            !self
                .udp_sockets
                .iter()
                .any(|socket| Arc::ptr_eq(socket, &udp_socket))
        );
        self.udp_sockets.push(udp_socket);
    }

    pub(crate) fn lookup_listener(&self, key: &ListenerKey) -> Option<&Arc<TcpListenerBg<E>>> {
        let exact_bucket = {
            let hash = key.hash();
            let bucket_index = hash & LISTENER_BUCKET_MASK;
            &self.listener_buckets[bucket_index as usize]
        };
        if let Some(listener) = exact_bucket
            .listeners
            .iter()
            .find(|listener| listener.listener_key() == key)
        {
            return Some(listener);
        }

        let wildcard_addr = match key.addr {
            IpAddress::Ipv4(_) => IpAddress::Ipv4(Ipv4Addr::UNSPECIFIED),
            IpAddress::Ipv6(_) => IpAddress::Ipv6(core::net::Ipv6Addr::UNSPECIFIED),
        };
        let wildcard_key = ListenerKey::new(wildcard_addr, key.port);
        let wildcard_bucket = {
            let hash = wildcard_key.hash();
            let bucket_index = hash & LISTENER_BUCKET_MASK;
            &self.listener_buckets[bucket_index as usize]
        };
        if let Some(listener) = wildcard_bucket
            .listeners
            .iter()
            .find(|listener| listener.listener_key() == &wildcard_key)
        {
            return Some(listener);
        }

        if !matches!(key.addr, IpAddress::Ipv4(_)) {
            return None;
        }

        // An IPv6 wildcard listener with IPV6_V6ONLY cleared also owns the
        // IPv4 wildcard namespace. Keep the normal IPv4 exact/wildcard
        // lookup priority above, then fall back to this listener.
        let v6_wildcard =
            ListenerKey::new(IpAddress::Ipv6(core::net::Ipv6Addr::UNSPECIFIED), key.port);
        let bucket_index = v6_wildcard.hash() & LISTENER_BUCKET_MASK;
        self.listener_buckets[bucket_index as usize]
            .listeners
            .iter()
            .find(|listener| listener.listener_key() == &v6_wildcard && listener.accepts_ipv4())
    }

    pub(crate) fn lookup_connection(
        &self,
        key: &ConnectionKey,
    ) -> Option<&Arc<TcpConnectionBg<E>>> {
        let bucket = {
            let hash = key.hash();
            let bucket_index = hash & CONNECTION_BUCKET_MASK;
            &self.connection_buckets[bucket_index as usize]
        };

        bucket
            .connections
            .iter()
            .find(|connection| connection.connection_key() == key)
    }

    pub(crate) fn remove_listener(&mut self, key: &ListenerKey) -> Option<Arc<TcpListenerBg<E>>> {
        let bucket = {
            let hash = key.hash();
            let bucket_index = hash & LISTENER_BUCKET_MASK;
            &mut self.listener_buckets[bucket_index as usize]
        };

        let index = bucket
            .listeners
            .iter()
            .position(|tcp_listener| tcp_listener.listener_key() == key)?;
        Some(bucket.listeners.swap_remove(index))
    }

    pub(crate) fn remove_dead_tcp_connection(&mut self, key: &ConnectionKey) {
        let bucket = {
            let hash = key.hash();
            let bucket_index = hash & CONNECTION_BUCKET_MASK;
            &mut self.connection_buckets[bucket_index as usize]
        };

        let index = bucket
            .connections
            .iter()
            .position(|tcp_connection| tcp_connection.connection_key() == key)
            .unwrap();
        let connection = bucket.connections.swap_remove(index);
        debug_assert!(
            !connection.poll_key().is_active(),
            "there should be no need to poll a dead TCP connection",
        );

        connection.notify_dead_events();
    }

    pub(crate) fn remove_udp_socket(
        &mut self,
        socket: &Arc<UdpSocketBg<E>>,
    ) -> Option<Arc<UdpSocketBg<E>>> {
        let index = self
            .udp_sockets
            .iter()
            .position(|udp_socket| Arc::ptr_eq(udp_socket, socket))?;
        Some(self.udp_sockets.swap_remove(index))
    }

    pub(crate) fn udp_socket_iter(&self) -> impl Iterator<Item = &Arc<UdpSocketBg<E>>> {
        self.udp_sockets.iter()
    }
}

impl<E: Ext> Default for SocketTable<E> {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(ktest)]
mod tests {
    use core::net::Ipv6Addr;

    use ostd::prelude::*;

    use super::*;

    #[ktest]
    fn ipv4_and_mapped_ipv6_connection_keys_are_equal() {
        let local_v4 = Ipv4Addr::new(127, 0, 0, 1);
        let remote_v4 = Ipv4Addr::new(192, 0, 2, 1);
        let ipv4_key = ConnectionKey::new(
            IpAddress::Ipv4(local_v4),
            8080,
            IpAddress::Ipv4(remote_v4),
            49152,
        );
        let mapped_key = ConnectionKey::new(
            IpAddress::Ipv6(local_v4.to_ipv6_mapped()),
            8080,
            IpAddress::Ipv6(remote_v4.to_ipv6_mapped()),
            49152,
        );

        assert_eq!(ipv4_key, mapped_key);
        assert_eq!(ipv4_key.hash(), mapped_key.hash());
    }

    #[ktest]
    fn native_ipv6_connection_key_stays_distinct_from_ipv4() {
        let ipv4_key = ConnectionKey::new(
            IpAddress::Ipv4(Ipv4Addr::LOCALHOST),
            8080,
            IpAddress::Ipv4(Ipv4Addr::new(192, 0, 2, 1)),
            49152,
        );
        let ipv6_key = ConnectionKey::new(
            IpAddress::Ipv6(Ipv6Addr::LOCALHOST),
            8080,
            IpAddress::Ipv6(Ipv6Addr::LOCALHOST),
            49152,
        );

        assert_ne!(ipv4_key, ipv6_key);
    }

    #[ktest]
    fn dual_stack_ports_conflict_in_both_bind_orders() {
        const PORT: PortNum = 8080;

        let registry = DualStackPortRegistry::new();
        registry
            .bind_ipv4(|dual_ports| {
                assert!(!dual_ports.contains(&PORT));
                Ok((PORT, false))
            })
            .unwrap();
        assert_eq!(
            registry.bind_dual_stack(|ipv4_ports, dual_ports| {
                if ipv4_ports.contains_key(&PORT) || dual_ports.contains(&PORT) {
                    Err(BindError::InUse)
                } else {
                    Ok((PORT, false))
                }
            }),
            Err(BindError::InUse)
        );
        registry.unregister_ipv4(PORT);

        registry
            .bind_dual_stack(|ipv4_ports, dual_ports| {
                assert!(!ipv4_ports.contains_key(&PORT));
                assert!(!dual_ports.contains(&PORT));
                Ok((PORT, false))
            })
            .unwrap();
        assert_eq!(
            registry.bind_dual_stack(|ipv4_ports, dual_ports| {
                if ipv4_ports.contains_key(&PORT) || dual_ports.contains(&PORT) {
                    Err(BindError::InUse)
                } else {
                    Ok((PORT, true))
                }
            }),
            Err(BindError::InUse)
        );
        assert_eq!(
            registry.bind_ipv4(|dual_ports| {
                if dual_ports.contains(&PORT) {
                    Err(BindError::InUse)
                } else {
                    Ok((PORT, false))
                }
            }),
            Err(BindError::InUse)
        );
        registry.unregister_dual_stack(PORT);
    }

    #[ktest]
    fn ipv4_port_remains_registered_until_last_owner_drops() {
        const PORT: PortNum = 8080;

        let registry = DualStackPortRegistry::new();
        for _ in 0..2 {
            registry
                .bind_ipv4(|dual_ports| {
                    assert!(!dual_ports.contains(&PORT));
                    Ok((PORT, true))
                })
                .unwrap();
        }

        registry.unregister_ipv4(PORT);
        assert_eq!(
            registry.bind_dual_stack(|ipv4_ports, dual_ports| {
                if ipv4_ports.contains_key(&PORT) || dual_ports.contains(&PORT) {
                    Err(BindError::InUse)
                } else {
                    Ok((PORT, false))
                }
            }),
            Err(BindError::InUse)
        );
        registry.unregister_ipv4(PORT);
        registry
            .bind_dual_stack(|ipv4_ports, dual_ports| {
                assert!(!ipv4_ports.contains_key(&PORT));
                assert!(!dual_ports.contains(&PORT));
                Ok((PORT, false))
            })
            .unwrap();
        registry.unregister_dual_stack(PORT);
    }
}

struct ListenerHashBucket<E: Ext> {
    listeners: Vec<Arc<TcpListenerBg<E>>>,
}

impl<E: Ext> ListenerHashBucket<E> {
    const fn new() -> Self {
        Self {
            listeners: Vec::new(),
        }
    }
}

struct ConnectionHashBucket<E: Ext> {
    connections: Vec<Arc<TcpConnectionBg<E>>>,
}

impl<E: Ext> ConnectionHashBucket<E> {
    const fn new() -> Self {
        Self {
            connections: Vec::new(),
        }
    }
}
