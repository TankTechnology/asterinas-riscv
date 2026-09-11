// SPDX-License-Identifier: MPL-2.0

use alloc::{boxed::Box, sync::Arc};
use core::sync::atomic::{AtomicBool, Ordering};

use aster_softirq::BottomHalfDisabled;
use ostd::sync::SpinLock;
use smoltcp::{
    iface::Context,
    socket::udp::UdpMetadata,
    wire::{IpAddress, IpListenEndpoint, IpRepr, UdpRepr},
};

use super::{
    ReceiveBehavior,
    common::{Inner, Socket, SocketBg},
};
use crate::{
    errors::udp::SendError,
    ext::Ext,
    iface::BoundUdpPort,
    socket::{RawUdpSocket, event::SocketEvents, unbound::new_udp_socket},
};

pub type UdpSocket<E> = Socket<UdpSocketInner<E>, E>;

/// States needed by [`UdpSocketBg`].
pub struct UdpSocketInner<E: Ext> {
    socket: SpinLock<Box<RawUdpSocket>, BottomHalfDisabled>,
    need_dispatch: AtomicBool,
    accepts_ipv4: bool,
    _ipv4_bound: Option<BoundUdpPort<E>>,
}

impl<E: Ext> Inner<E> for UdpSocketInner<E> {
    type BoundPort = BoundUdpPort<E>;
    type Observer = E::UdpEventObserver;

    fn on_drop(this: &Arc<SocketBg<Self, E>>) {
        this.inner.socket.lock().close();

        // Keep the socket-table -> UDP-registry lock order used by poll.
        // Once removed locally, a concurrent registry lookup can at most see a
        // closed socket through its weak reference.
        this.bound.iface().common().remove_udp_socket(this);

        if this.inner.accepts_ipv4 {
            this.bound
                .iface()
                .common()
                .udp_registry()
                .unregister_dual_port(this.bound.port());
        }
        this.bound
            .iface()
            .common()
            .udp_registry()
            .unregister_socket(this);
    }
}

pub(crate) type UdpSocketBg<E> = SocketBg<UdpSocketInner<E>, E>;

impl<E: Ext> UdpSocketBg<E> {
    pub(crate) fn local_port(&self) -> u16 {
        self.bound.port()
    }

    /// Tries to process an incoming packet and returns whether the packet is processed.
    pub(crate) fn process(
        &self,
        cx: &mut Context,
        ip_repr: &IpRepr,
        udp_repr: &UdpRepr,
        udp_payload: &[u8],
    ) -> bool {
        let local_addr = self.bound.endpoint().addr;
        if !matches!(
            (local_addr, ip_repr.dst_addr()),
            (IpAddress::Ipv4(_), IpAddress::Ipv4(_)) | (IpAddress::Ipv6(_), IpAddress::Ipv6(_))
        ) {
            return false;
        }

        let mut socket = self.inner.socket.lock();

        if !socket.accepts(cx, ip_repr, udp_repr) {
            return false;
        }

        socket.process(
            cx,
            smoltcp::phy::PacketMeta::default(),
            ip_repr,
            udp_repr,
            udp_payload,
        );

        self.notify_events(SocketEvents::CAN_RECV);

        true
    }

    /// Tries to generate an outgoing packet and dispatches the generated packet.
    pub(crate) fn dispatch<D>(&self, cx: &mut Context, dispatch: D)
    where
        D: FnOnce(&mut Context, &IpRepr, &UdpRepr, &[u8]),
    {
        let mut socket = self.inner.socket.lock();

        socket
            .dispatch(cx, |cx, _meta, (ip_repr, udp_repr, udp_payload)| {
                dispatch(cx, &ip_repr, &udp_repr, udp_payload);
                Ok::<(), ()>(())
            })
            .unwrap();

        // For UDP, dequeuing a packet means that we can queue more packets.
        self.notify_events(SocketEvents::CAN_SEND);

        self.inner
            .need_dispatch
            .store(socket.send_queue() > 0, Ordering::Relaxed);
    }

    /// Returns whether the socket _may_ generate an outgoing packet.
    ///
    /// The check is intended to be lock-free and fast, but may have false positives.
    pub(crate) fn need_dispatch(&self) -> bool {
        self.inner.need_dispatch.load(Ordering::Relaxed)
    }

    pub(crate) const fn accepts_ipv4(&self) -> bool {
        self.inner.accepts_ipv4
    }
}

impl<E: Ext> UdpSocket<E> {
    /// Binds to a specified endpoint.
    ///
    /// Polling the iface is _not_ required after this method succeeds.
    pub fn new_bind(
        bound: BoundUdpPort<E>,
        observer: E::UdpEventObserver,
        v6only: bool,
    ) -> Result<Self, (BoundUdpPort<E>, smoltcp::socket::udp::BindError)> {
        let local_endpoint = bound.endpoint();
        let accepts_ipv4 = matches!(
            local_endpoint.addr,
            IpAddress::Ipv6(addr) if addr.is_unspecified()
        ) && !v6only;

        let socket = {
            let mut socket = new_udp_socket();

            // Linux wildcard addresses accept packets for every local address
            // in their family. Represent both 0.0.0.0 and :: with smoltcp's
            // wildcard (`None`); `accepts_ipv4` separately controls whether
            // an IPv6 wildcard also receives translated IPv4 packets.
            let bind_endpoint = IpListenEndpoint {
                addr: if local_endpoint.addr.is_unspecified() {
                    None
                } else {
                    Some(local_endpoint.addr)
                },
                port: local_endpoint.port,
            };
            if let Err(err) = socket.bind(bind_endpoint) {
                return Err((bound, err));
            }

            socket
        };

        let ipv4_bound = if accepts_ipv4 {
            let ipv4_endpoint = smoltcp::wire::IpEndpoint::new(
                IpAddress::Ipv4(core::net::Ipv4Addr::UNSPECIFIED),
                local_endpoint.port,
            );
            let Ok(ipv4_bound) = bound
                .iface()
                .bind_udp(crate::iface::BindPortConfig::new(ipv4_endpoint, false))
            else {
                return Err((bound, smoltcp::socket::udp::BindError::Unaddressable));
            };
            Some(ipv4_bound)
        } else {
            None
        };

        if accepts_ipv4
            && !bound
                .iface()
                .common()
                .udp_registry()
                .register_dual_port(local_endpoint.port)
        {
            return Err((bound, smoltcp::socket::udp::BindError::InvalidState));
        }

        let inner = UdpSocketInner {
            socket: SpinLock::new(socket),
            need_dispatch: AtomicBool::new(false),
            accepts_ipv4,
            _ipv4_bound: ipv4_bound,
        };

        let socket = Self::new(bound, inner);
        socket.init_observer(observer);
        socket
            .iface()
            .common()
            .register_udp_socket(socket.inner().clone());
        socket
            .iface()
            .common()
            .udp_registry()
            .register_socket(socket.inner());

        Ok(socket)
    }

    /// Sends some data.
    ///
    /// Polling the iface is _always_ required after this method succeeds.
    pub fn send<F, R>(
        &self,
        size: usize,
        meta: impl Into<UdpMetadata>,
        f: F,
    ) -> Result<R, SendError>
    where
        F: FnOnce(&mut [u8]) -> R,
    {
        let mut socket = self.0.inner.socket.lock();

        if size > socket.payload_send_capacity() {
            return Err(SendError::TooLarge);
        }

        let buffer = socket.send(size, meta)?;
        let result = f(buffer);

        self.0
            .inner
            .need_dispatch
            .store(socket.send_queue() > 0, Ordering::Relaxed);

        Ok(result)
    }

    /// Receives some data.
    ///
    /// Polling the iface is _not_ required after this method succeeds.
    pub fn recv<CopyFn, R>(
        &self,
        behavior: ReceiveBehavior,
        copy_fn: CopyFn,
    ) -> Result<R, smoltcp::socket::udp::RecvError>
    where
        CopyFn: FnOnce(&[u8], UdpMetadata) -> R,
    {
        let mut socket = self.0.inner.socket.lock();

        let (data, meta) = match behavior {
            ReceiveBehavior::Recv => socket.recv()?,
            ReceiveBehavior::Peek => {
                let (data, meta) = socket.peek()?;
                (data, *meta)
            }
        };
        let result = copy_fn(data, meta);

        Ok(result)
    }

    /// Calls `f` with an immutable reference to the associated [`RawUdpSocket`].
    //
    // NOTE: If a mutable reference is required, add a method above that correctly updates the next
    // polling time.
    pub fn raw_with<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&RawUdpSocket) -> R,
    {
        let socket = self.0.inner.socket.lock();
        f(&socket)
    }
}
