// SPDX-License-Identifier: MPL-2.0

use alloc::{boxed::Box, collections::btree_map::BTreeMap, sync::Arc, vec::Vec};

use aster_softirq::BottomHalfDisabled;
use ostd::sync::SpinLock;
use smoltcp::{
    socket::PollAt,
    time::Duration,
    wire::{IpEndpoint, IpListenEndpoint, IpRepr, TcpRepr},
};

use super::{
    common::{Inner, NeedIfacePoll, Socket, SocketBg},
    tcp_conn::{TcpConnection, TcpConnectionBg, TcpConnectionInner, TcpProcessResult},
};
use crate::{
    errors::tcp::ListenError,
    ext::Ext,
    iface::{BindPortConfig, BoundTcpPort, Iface, PollableIfaceMut},
    socket::{
        option::{RawTcpOption, RawTcpSetOption},
        unbound::{RawTcpSocket, new_tcp_socket},
    },
    socket_table::{ConnectionKey, ListenerKey},
};

pub type TcpListener<E> = Socket<TcpListenerInner<E>, E>;

pub struct TcpBacklog<E: Ext> {
    socket: Box<RawTcpSocket>,
    max_conn: usize,
    pub(super) connecting: BTreeMap<ConnectionKey, TcpConnection<E>>,
    pub(super) connected: Vec<TcpConnection<E>>,
}

/// States needed by [`TcpListenerBg`].
pub struct TcpListenerInner<E: Ext> {
    pub(super) backlog: SpinLock<TcpBacklog<E>, BottomHalfDisabled>,
    listener_key: ListenerKey,
    accepts_ipv4: bool,
    // A dual-stack IPv6 listener also reserves the corresponding IPv4
    // wildcard port on its owning interface. The namespace registry extends
    // that reservation to ephemeral allocations on other interfaces.
    _ipv4_bound: Option<BoundTcpPort<E>>,
}

impl<E: Ext> TcpListenerInner<E> {
    fn new(
        backlog: TcpBacklog<E>,
        listener_key: ListenerKey,
        accepts_ipv4: bool,
        ipv4_bound: Option<BoundTcpPort<E>>,
    ) -> Self {
        Self {
            backlog: SpinLock::new(backlog),
            listener_key,
            accepts_ipv4,
            _ipv4_bound: ipv4_bound,
        }
    }
}

impl<E: Ext> Inner<E> for TcpListenerInner<E> {
    type BoundPort = BoundTcpPort<E>;
    type Observer = E::TcpEventObserver;

    fn on_drop(this: &Arc<SocketBg<Self, E>>) {
        debug_assert_eq!(
            Arc::strong_count(this),
            1,
            "a listener must be closed before dropping"
        );

        let registry = this.bound.iface().common().tcp_registry();
        registry.unregister_listener(this);
        if this.inner.accepts_ipv4 {
            registry.unregister_dual_port(this.bound.port());
        }
    }
}

pub(crate) type TcpListenerBg<E> = SocketBg<TcpListenerInner<E>, E>;

impl<E: Ext> TcpListener<E> {
    /// Listens at a specified endpoint.
    ///
    /// Polling the iface is _not_ required after this method succeeds.
    pub fn new_listen(
        bound: BoundTcpPort<E>,
        max_conn: usize,
        option: &RawTcpOption,
        observer: E::TcpEventObserver,
        v6only: bool,
    ) -> Result<Self, (BoundTcpPort<E>, ListenError)> {
        let local_endpoint = bound.endpoint();

        let iface = bound.iface().clone();
        let mut sockets = iface.common().sockets();

        let listener_key = ListenerKey::new(local_endpoint.addr, local_endpoint.port);
        let accepts_ipv4 = matches!(
            local_endpoint.addr,
            smoltcp::wire::IpAddress::Ipv6(addr) if addr.is_unspecified()
        ) && !v6only;

        let has_conflict = sockets.lookup_listener(&listener_key).is_some()
            || (accepts_ipv4
                && sockets
                    .lookup_listener(&ListenerKey::new(
                        smoltcp::wire::IpAddress::Ipv4(core::net::Ipv4Addr::UNSPECIFIED),
                        local_endpoint.port,
                    ))
                    .is_some());
        if has_conflict {
            return Err((bound, ListenError::AddressInUse));
        }

        let socket = {
            let mut socket = new_tcp_socket();

            option.apply(&mut socket);

            let listen_endpoint = if local_endpoint.addr.is_unspecified() {
                IpListenEndpoint {
                    addr: None,
                    port: local_endpoint.port,
                }
            } else {
                IpListenEndpoint {
                    addr: Some(local_endpoint.addr),
                    port: local_endpoint.port,
                }
            };
            if let Err(err) = socket.listen(listen_endpoint) {
                return Err((bound, err.into()));
            }

            socket
        };

        let ipv4_bound = if accepts_ipv4 {
            let ipv4_endpoint = IpEndpoint::new(
                smoltcp::wire::IpAddress::Ipv4(core::net::Ipv4Addr::UNSPECIFIED),
                local_endpoint.port,
            );
            let Ok(ipv4_bound) = iface.bind_tcp(BindPortConfig::new(ipv4_endpoint, false)) else {
                return Err((bound, ListenError::AddressInUse));
            };
            Some(ipv4_bound)
        } else {
            None
        };

        if accepts_ipv4
            && !iface
                .common()
                .tcp_registry()
                .register_dual_port(local_endpoint.port)
        {
            return Err((bound, ListenError::AddressInUse));
        }

        let inner = {
            let backlog = TcpBacklog {
                socket,
                max_conn,
                connecting: BTreeMap::new(),
                connected: Vec::new(),
            };

            TcpListenerInner::new(backlog, listener_key, accepts_ipv4, ipv4_bound)
        };

        let listener = Self::new(bound, inner);
        listener.init_observer(observer);
        let res = sockets.insert_listener(listener.inner().clone());
        debug_assert!(res.is_ok());
        iface
            .common()
            .tcp_registry()
            .register_listener(listener.inner());

        Ok(listener)
    }

    /// Accepts a TCP connection.
    ///
    /// Polling the iface is _not_ required after this method succeeds.
    pub fn accept(&self) -> Option<(TcpConnection<E>, IpEndpoint)> {
        let accepted = {
            let mut backlog = self.0.inner.backlog.lock();
            backlog.connected.pop()?
        };

        let remote_endpoint = {
            // The lock on `accepted` cannot be locked after locking `self`, otherwise we might get
            // a deadlock due to inconsistent lock order problems.
            let mut socket = accepted.0.inner.lock();

            socket.listener = None;
            socket.remote_endpoint()
        };

        Some((accepted, remote_endpoint.unwrap()))
    }

    /// Returns whether there is a TCP connection to accept.
    ///
    /// It's the caller's responsibility to deal with race conditions when using this method.
    pub fn can_accept(&self) -> bool {
        !self.0.inner.backlog.lock().connected.is_empty()
    }

    /// Closes the listener.
    ///
    /// Polling the iface is _always_ required after this method succeeds.
    ///
    /// Note that this method must be called before dropping the TCP listener to avoid resource
    /// leakage.
    pub fn close(&self) {
        // A TCP listener can be removed immediately.
        self.0.bound.iface().common().remove_tcp_listener(&self.0);

        let (connecting, connected) = {
            let mut socket = self.0.inner.backlog.lock();
            (
                core::mem::take(&mut socket.connecting),
                core::mem::take(&mut socket.connected),
            )
        };

        // The lock on `connecting`/`connected` cannot be locked after locking `self`, otherwise we
        // might get a deadlock. due to inconsistent lock order problems.
        connecting.values().for_each(|socket| socket.reset());
        connected.iter().for_each(|socket| socket.reset());
    }
}

impl<E: Ext> RawTcpSetOption for TcpListener<E> {
    fn set_keep_alive(&self, interval: Option<Duration>) -> NeedIfacePoll {
        let mut backlog = self.0.inner.backlog.lock();
        backlog.socket.set_keep_alive(interval);

        NeedIfacePoll::FALSE
    }

    fn set_nagle_enabled(&self, enabled: bool) {
        let mut backlog = self.0.inner.backlog.lock();
        backlog.socket.set_nagle_enabled(enabled);
    }
}

impl<E: Ext> TcpListenerBg<E> {
    pub(crate) const fn listener_key(&self) -> &ListenerKey {
        &self.inner.listener_key
    }

    pub(crate) const fn accepts_ipv4(&self) -> bool {
        self.inner.accepts_ipv4
    }
}

impl<E: Ext> TcpListenerBg<E> {
    /// Tries to process an incoming packet and returns whether the packet is processed.
    pub(crate) fn process(
        self: &Arc<Self>,
        iface: &mut PollableIfaceMut<E>,
        connection_iface: &Arc<dyn Iface<E>>,
        ip_repr: &IpRepr,
        tcp_repr: &TcpRepr,
    ) -> (TcpProcessResult, Option<Arc<TcpConnectionBg<E>>>) {
        let mut backlog = self.inner.backlog.lock();

        if !backlog
            .socket
            .accepts(iface.context_mut(), ip_repr, tcp_repr)
        {
            return (TcpProcessResult::NotProcessed, None);
        }

        // FIXME: According to the Linux implementation, `max_conn` is the upper bound of
        // `connected.len()`. We currently limit it to `connected.len() + connecting.len()` for
        // simplicity.
        if backlog.connected.len() + backlog.connecting.len() >= backlog.max_conn {
            return (TcpProcessResult::Processed, None);
        }

        let result = match backlog
            .socket
            .process(iface.context_mut(), ip_repr, tcp_repr)
        {
            None => TcpProcessResult::Processed,
            Some((ip_repr, tcp_repr)) => TcpProcessResult::ProcessedWithReply(ip_repr, tcp_repr),
        };

        if backlog.socket.state() == smoltcp::socket::tcp::State::Listen {
            return (result, None);
        }

        let new_socket = {
            let mut socket = new_tcp_socket();
            RawTcpOption::inherit(&backlog.socket, &mut socket);
            socket.listen(backlog.socket.listen_endpoint()).unwrap();
            socket
        };

        let conn = TcpConnection::new_cyclic(
            connection_iface
                .bind_tcp(BindPortConfig::new_backlog(self.bound.endpoint()))
                .unwrap(),
            |weak| {
                TcpConnectionInner::new(
                    core::mem::replace(&mut backlog.socket, new_socket),
                    Some(self.clone()),
                    weak,
                )
            },
        );
        let conn_bg = conn.inner().clone();

        let old_conn = backlog.connecting.insert(*conn_bg.connection_key(), conn);
        debug_assert!(old_conn.is_none());

        iface.update_next_poll_at_ms(&conn_bg, PollAt::Now);

        (result, Some(conn_bg))
    }
}
