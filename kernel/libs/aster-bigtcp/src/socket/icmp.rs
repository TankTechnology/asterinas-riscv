// SPDX-License-Identifier: MPL-2.0

//! ICMP sockets, the sockets behind `ping`.
//!
//! One ICMP socket receives the echo replies whose identifier matches the one
//! it is bound to, and it sends complete ICMP messages supplied by the caller.
//! The kernel-side socket fills in the identifier and the checksum; this type
//! is only responsible for queueing packets on the interface it belongs to.
//!
//! smoltcp's ICMP socket is used as the queueing and parsing implementation:
//! it already matches replies by identifier and refuses requests, which is
//! exactly the demultiplexing a ping socket needs.

use alloc::{boxed::Box, sync::Arc, vec};
use core::sync::atomic::{AtomicBool, Ordering};

use aster_softirq::BottomHalfDisabled;
use ostd::sync::SpinLock;
use smoltcp::{
    iface::Context,
    socket::icmp::{Endpoint, PacketBuffer, PacketMetadata, Socket as RawIcmpSocket},
    wire::{IcmpRepr, IpAddress, IpRepr, Ipv4Repr, Ipv6Repr},
};
use spin::once::Once;

use super::{ReceiveBehavior, SocketEventObserver, SocketEvents};
use crate::{errors::BindError, ext::Ext, iface::Iface};

const ICMP_PAYLOAD_LEN: usize = 16 * 1024;
const ICMP_METADATA_LEN: usize = 64;

pub type RawIcmpSocketExt = RawIcmpSocket<'static>;

pub use smoltcp::socket::icmp::{RecvError, SendError};

/// A foreground ICMP socket.
pub struct IcmpSocket<E: Ext> {
    inner: Arc<IcmpSocketBg<E>>,
}

/// The part of an ICMP socket that handles packets from the network.
pub struct IcmpSocketBg<E: Ext> {
    socket: SpinLock<Box<RawIcmpSocketExt>, BottomHalfDisabled>,
    need_dispatch: AtomicBool,
    observer: Once<E::IcmpEventObserver>,
    iface: Arc<dyn Iface<E>>,
    ident: u16,
}

impl<E: Ext> IcmpSocket<E> {
    /// Creates a socket bound to `ident` on `iface` and registers it for
    /// incoming packets.
    ///
    /// The caller must make sure the identifier is not already in use on the
    /// interface; [`Iface::bind_icmp`] does that.
    ///
    /// [`Iface::bind_icmp`]: crate::iface::Iface::bind_icmp
    pub(crate) fn new_unregistered(
        iface: Arc<dyn Iface<E>>,
        ident: u16,
    ) -> Result<Self, BindError> {
        let mut socket = Box::new(new_raw_icmp_socket());
        socket
            .bind(Endpoint::Ident(ident))
            .map_err(|_| BindError::InUse)?;

        Ok(Self {
            inner: Arc::new(IcmpSocketBg {
                socket: SpinLock::new(socket),
                need_dispatch: AtomicBool::new(false),
                observer: Once::new(),
                iface,
                ident,
            }),
        })
    }

    pub(crate) fn inner(&self) -> &Arc<IcmpSocketBg<E>> {
        &self.inner
    }

    /// The ICMP identifier this socket owns.
    pub fn ident(&self) -> u16 {
        self.inner.ident
    }

    /// The interface the socket is bound to.
    pub fn iface(&self) -> &Arc<dyn Iface<E>> {
        &self.inner.iface
    }

    /// Initializes the observer whose `on_events` is called when packets
    /// arrive.
    pub fn init_observer(&self, observer: E::IcmpEventObserver) {
        self.inner.observer.call_once(|| observer);
    }

    /// Queues one ICMP message for `remote`.
    ///
    /// The closure fills in the message; the caller is responsible for the
    /// ICMP header (including the identifier and the checksum that this
    /// socket's identifier demands).
    pub fn send<F, R>(&self, size: usize, remote: IpAddress, f: F) -> Result<R, SendError>
    where
        F: FnOnce(&mut [u8]) -> R,
    {
        let mut socket = self.inner.socket.lock();
        if size > socket.payload_send_capacity() {
            return Err(SendError::BufferFull);
        }
        let buffer = socket.send(size, remote)?;
        let result = f(buffer);
        self.inner.need_dispatch.store(true, Ordering::Relaxed);
        Ok(result)
    }

    /// Dequeues one received ICMP message with the hop limit it arrived with.
    pub fn recv<F, R>(&self, behavior: ReceiveBehavior, f: F) -> Result<R, RecvError>
    where
        F: FnOnce(&[u8], IpAddress, u8) -> R,
    {
        let mut socket = self.inner.socket.lock();
        let (data, remote, hop_limit) = match behavior {
            ReceiveBehavior::Recv => socket.recv_with_header()?,
            ReceiveBehavior::Peek => socket.peek_with_header()?,
        };
        Ok(f(data, remote, hop_limit))
    }

    /// Sets the hop limit (TTL) used for messages this socket sends.
    pub fn set_hop_limit(&self, hop_limit: u8) {
        let mut socket = self.inner.socket.lock();
        socket.set_hop_limit(Some(hop_limit));
    }

    /// Returns the hop limit this socket sends with.
    pub fn hop_limit(&self) -> u8 {
        self.raw_with(|socket| socket.hop_limit().unwrap_or(64))
    }

    /// Runs `f` with the raw smoltcp socket.
    pub fn raw_with<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&RawIcmpSocketExt) -> R,
    {
        let socket = self.inner.socket.lock();
        f(&socket)
    }
}

impl<E: Ext> Drop for IcmpSocket<E> {
    fn drop(&mut self) {
        // Remove the socket from the interface table before the last
        // reference goes away, so a packet polled concurrently cannot find a
        // socket that its caller considers closed.
        self.inner.iface.common().remove_icmp_socket(&self.inner);
    }
}

impl<E: Ext> IcmpSocketBg<E> {
    /// Tries to hand one incoming ICMP message to this socket.
    pub(crate) fn process(
        &self,
        cx: &mut Context,
        ipv4_repr: &Ipv4Repr,
        icmp_repr: &smoltcp::wire::Icmpv4Repr<'_>,
    ) -> bool {
        let mut socket = self.socket.lock();
        if !socket.accepts_v4(cx, ipv4_repr, icmp_repr) {
            return false;
        }
        socket.process_v4(cx, ipv4_repr, icmp_repr);
        drop(socket);
        if let Some(observer) = self.observer.get() {
            observer.on_events(SocketEvents::CAN_RECV);
        }
        true
    }

    /// Tries to generate one queued message and hands the packet to
    /// `dispatch`.
    pub(crate) fn dispatch<D>(&self, cx: &mut Context, dispatch: D)
    where
        D: FnOnce(&mut Context, IpRepr, IcmpRepr),
    {
        let mut socket = self.socket.lock();
        let _ = socket.dispatch(cx, |cx, (ip_repr, icmp_repr)| {
            dispatch(cx, ip_repr, icmp_repr);
            Ok::<(), ()>(())
        });
        // One dispatch call emits at most one packet. Keep the interface
        // polling while messages remain queued, or a queue of two probes would
        // stall after the first one.
        let has_more = socket.send_queue() > 0;
        drop(socket);
        self.need_dispatch.store(has_more, Ordering::Relaxed);
    }

    pub(crate) fn ident(&self) -> u16 {
        self.ident
    }

    pub(crate) fn need_dispatch(&self) -> bool {
        self.need_dispatch.load(Ordering::Relaxed)
    }

    /// Returns whether the socket can accept a reply with this identifier.
    pub(crate) fn needs_ident(&self, ident: u16) -> bool {
        self.ident == ident
    }
}

fn new_raw_icmp_socket() -> RawIcmpSocketExt {
    let metadata = PacketMetadata::EMPTY;
    let rx_buffer = PacketBuffer::new(
        vec![metadata; ICMP_METADATA_LEN],
        vec![0u8; ICMP_PAYLOAD_LEN],
    );
    let tx_buffer = PacketBuffer::new(
        vec![metadata; ICMP_METADATA_LEN],
        vec![0u8; ICMP_PAYLOAD_LEN],
    );
    RawIcmpSocketExt::new(rx_buffer, tx_buffer)
}
