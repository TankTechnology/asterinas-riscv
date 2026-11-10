// SPDX-License-Identifier: MPL-2.0

//! ICMP datagram sockets, the kinds `ping` opens.
//!
//! Linux gives a ping socket the `SOCK_DGRAM`/`IPPROTO_ICMP` pair. The user
//! writes a complete ICMP message (header included); the kernel takes the
//! identifier from the socket's own allocation, zeroes the checksum so the
//! network stack computes it, and delivers echo replies back with their ICMP
//! header intact.

use core::{
    num::NonZeroU8,
    sync::atomic::{AtomicBool, AtomicU8, AtomicU16, Ordering},
};

use aster_bigtcp::{
    errors::BindError,
    socket::{IcmpRecvError, IcmpSendError, ReceiveBehavior},
    wire::{IpAddress, IpEndpoint},
};

use super::{
    DatagramObserver, IpAddressFamily,
    addr::{UNSPECIFIED_LOCAL_ENDPOINT, UNSPECIFIED_LOCAL_ENDPOINT_V6},
};
use crate::{
    events::IoEvents,
    fs::{
        file::{FileCommon, StatusFlags},
        pseudofs::SockFs,
    },
    net::{
        iface::{IcmpSocketHandle, Iface},
        net_ns::{NetNamespace, current_net_ns},
        socket::{
            Socket,
            ip::options::{IpTtl, RecvOpts, RecvTtl, Ttl},
            options::{
                Error as SocketError, SocketOption, SocketType, Timestamp, TimestampNs,
                macros::{sock_option_mut, sock_option_ref},
            },
            private::SocketPrivate,
            util::{
                ControlMessage, MessageHeader, RecvFlags, RecvOutput, SendFlags, SocketAddr,
                SocketControlMessage, options::SocketTimeouts,
            },
        },
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
    time::{SystemTime, suseconds_t, time_t, timespec_t, timeval_t},
    util::{MultiRead, MultiWrite, net::SockType},
};

/// The first identifier handed out by the ephemeral allocator.
const FIRST_EPHEMERAL_IDENT: u16 = 0x8000;

static NEXT_ICMP_IDENT: AtomicU16 = AtomicU16::new(FIRST_EPHEMERAL_IDENT);

pub struct IcmpSocket {
    inner: IcmpSocketHandle,
    family: IpAddressFamily,
    remote: RwLock<Option<IpAddress>>,
    timeouts: SocketTimeouts,
    /// `IP_RECVTTL`: attach the received hop limit to each message.
    recv_ttl: AtomicBool,
    /// `IP_RETOPTS`: accepted for compatibility; IP options are not delivered.
    recv_opts: AtomicBool,
    timestamp: AtomicBool,
    timestamp_ns: AtomicBool,
    /// Hop limit used for outgoing messages (`IP_TTL`).
    ttl: AtomicU8,
    pollee: Pollee,
    net_ns: Arc<NetNamespace>,
    common: FileCommon,
}

impl IcmpSocket {
    pub fn new(is_nonblocking: bool, family: IpAddressFamily) -> Result<Arc<Self>> {
        // The ICMP tables are per interface; the kernel has one default
        // interface to send through, the same rule UDP uses for an
        // unspecified destination.
        let net_ns = current_net_ns();
        let iface = net_ns.default_iface().clone();
        let inner = bind_ident(&iface)?;

        let pollee = Pollee::new();
        inner.init_observer(DatagramObserver::new(pollee.clone()));

        let status_flags = if is_nonblocking {
            StatusFlags::O_NONBLOCK
        } else {
            StatusFlags::empty()
        };

        Ok(Arc::new(Self {
            inner,
            family,
            remote: RwLock::new(None),
            timeouts: SocketTimeouts::new(),
            recv_ttl: AtomicBool::new(false),
            recv_opts: AtomicBool::new(false),
            timestamp: AtomicBool::new(false),
            timestamp_ns: AtomicBool::new(false),
            ttl: AtomicU8::new(64),
            pollee,
            net_ns,
            common: FileCommon::new(SockFs::new_path(), status_flags),
        }))
    }

    fn try_recv(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, SocketAddr, u8)> {
        let result = self
            .inner
            .recv(flags.receive_behavior(), |data, remote, hop_limit| {
                let message_len = data.len();
                let copied = writer.write(&mut VmReader::from(data)).map_err(Into::into);
                (copied, remote, message_len, hop_limit)
            });

        match result {
            Ok((Ok(copied_len), remote, message_len, hop_limit)) => {
                let output = RecvOutput::new_for_packet(flags, copied_len, message_len);
                Ok((output, ip_socket_addr(remote, 0)?, hop_limit))
            }
            Ok((Err(err), _, _, _)) => Err(err),
            Err(IcmpRecvError::Exhausted) => {
                return_errno_with_message!(Errno::EAGAIN, "the receive buffer is empty")
            }
            Err(IcmpRecvError::Truncated) => {
                unreachable!("`recv` should never fail with `RecvError::Truncated`")
            }
        }
    }

    fn try_send(
        &self,
        reader: &mut dyn MultiRead,
        remote: Option<IpAddress>,
        _flags: SendFlags,
    ) -> Result<usize> {
        let remote = match remote {
            Some(remote) => remote,
            None => *self.remote.read().as_ref().ok_or_else(|| {
                Error::with_message(
                    Errno::EDESTADDRREQ,
                    "the destination address is not specified",
                )
            })?,
        };

        let size = reader.sum_lens();
        if size < ICMP_HEADER_LEN {
            return_errno_with_message!(Errno::EINVAL, "the ICMP message is too short");
        }

        let ident = self.inner.ident();
        let iface = self.inner.iface().clone();
        let result = self.inner.send(size, remote, |buffer| {
            let copied: Result<usize> = reader
                .read(&mut VmWriter::from(&mut *buffer))
                .map_err(|_| Error::with_message(Errno::EFAULT, "failed to read the ICMP message"));
            if copied.is_ok() {
                // A ping socket owns the identifier, and the checksum is
                // computed when the packet is emitted.
                buffer[2..4].fill(0);
                buffer[4..6].copy_from_slice(&ident.to_be_bytes());
            }
            copied
        });

        let copied = match result {
            Ok(copied) => copied?,
            Err(IcmpSendError::Unaddressable) => {
                return_errno_with_message!(Errno::EINVAL, "the destination address is invalid")
            }
            Err(IcmpSendError::BufferFull) => {
                return_errno_with_message!(Errno::EAGAIN, "the send buffer is full")
            }
        };

        self.pollee.invalidate();
        iface.poll();

        Ok(copied)
    }

    fn check_io_events(&self) -> IoEvents {
        self.inner.raw_with(|socket| {
            let mut events = IoEvents::empty();
            if socket.can_recv() {
                events |= IoEvents::IN;
            }
            if socket.can_send() {
                events |= IoEvents::OUT;
            }
            events
        })
    }

    fn check_endpoint_family(&self, endpoint: &IpEndpoint) -> Result<()> {
        if IpAddressFamily::from(endpoint.addr) == self.family {
            Ok(())
        } else {
            return_errno_with_message!(
                Errno::EAFNOSUPPORT,
                "the address is in a different address family"
            );
        }
    }
}

const ICMP_HEADER_LEN: usize = 8;

fn ip_socket_addr(addr: IpAddress, port: u16) -> Result<SocketAddr> {
    match addr {
        IpAddress::Ipv4(addr) => Ok(SocketAddr::IPv4(addr, port)),
        IpAddress::Ipv6(_) => {
            return_errno_with_message!(Errno::EAFNOSUPPORT, "IPv6 ICMP is not supported yet")
        }
    }
}

/// Binds a free identifier on `iface`.
fn bind_ident(iface: &Arc<Iface>) -> Result<IcmpSocketHandle> {
    for _ in 0..u16::MAX {
        let ident = NEXT_ICMP_IDENT.fetch_add(1, Ordering::Relaxed);
        if ident == 0 {
            continue;
        }
        match iface.bind_icmp(ident) {
            Ok(socket) => return Ok(socket),
            Err(BindError::InUse) => continue,
            Err(BindError::Exhausted) => break,
        }
    }
    return_errno_with_message!(Errno::EAGAIN, "no ICMP identifier is available")
}

impl Pollable for IcmpSocket {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee
            .poll_with(mask, poller, || self.check_io_events())
    }
}

impl SocketPrivate for IcmpSocket {
    fn is_nonblocking(&self) -> bool {
        self.common.is_nonblocking()
    }
}

impl Socket for IcmpSocket {
    fn net_ns(&self) -> &NetNamespace {
        &self.net_ns
    }

    fn bind(&self, socket_addr: SocketAddr) -> Result<()> {
        let endpoint = IpEndpoint::try_from(socket_addr)?;
        self.check_endpoint_family(&endpoint)?;

        if endpoint.port != 0 && endpoint.port != self.inner.ident() {
            return_errno_with_message!(
                Errno::EADDRINUSE,
                "the ICMP identifier is already owned by this socket"
            );
        }

        if !endpoint.addr.is_unspecified() {
            let iface_addr = self
                .inner
                .iface()
                .ipv4_cidr()
                .map(|cidr| IpAddress::Ipv4(cidr.address()));
            if iface_addr != Some(endpoint.addr) {
                return_errno_with_message!(
                    Errno::EADDRNOTAVAIL,
                    "the address is not available from the local machine"
                );
            }
        }

        Ok(())
    }

    fn connect(&self, socket_addr: SocketAddr) -> Result<()> {
        let endpoint = IpEndpoint::try_from(socket_addr)?;
        self.check_endpoint_family(&endpoint)?;
        *self.remote.write() = Some(endpoint.addr);
        Ok(())
    }

    fn addr(&self) -> Result<SocketAddr> {
        let unspecified = match self.family {
            IpAddressFamily::IPv4 => UNSPECIFIED_LOCAL_ENDPOINT,
            IpAddressFamily::IPv6 => UNSPECIFIED_LOCAL_ENDPOINT_V6,
        };
        Ok(IpEndpoint::new(unspecified.addr, self.inner.ident()).into())
    }

    fn peer_addr(&self) -> Result<SocketAddr> {
        let remote =
            *self.remote.read().as_ref().ok_or_else(|| {
                Error::with_message(Errno::ENOTCONN, "the socket is not connected")
            })?;
        Ok(ip_socket_addr(remote, 0)?)
    }

    fn sendmsg(
        &self,
        reader: &mut dyn MultiRead,
        message_header: MessageHeader,
        flags: SendFlags,
    ) -> Result<usize> {
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let MessageHeader {
            addr,
            control_messages,
        } = message_header;

        if !control_messages.is_empty() {
            warn!("sending control message is not supported");
        }

        let remote = match addr {
            Some(addr) => {
                let endpoint = IpEndpoint::try_from(addr)?;
                self.check_endpoint_family(&endpoint)?;
                Some(endpoint.addr)
            }
            None => None,
        };

        self.try_send(reader, remote, flags)
    }

    fn recvmsg(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, MessageHeader)> {
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let (output, peer_addr, hop_limit) = if flags.contains(RecvFlags::MSG_DONTWAIT) {
            self.try_recv(writer, flags)?
        } else {
            self.block_on(IoEvents::IN, self.timeouts.recv_timeout(), || {
                self.try_recv(writer, flags)
            })?
        };

        let mut control_messages = Vec::new();
        if self.timestamp.load(Ordering::Relaxed) || self.timestamp_ns.load(Ordering::Relaxed) {
            // Like an IP socket, the timestamp is taken when the message is
            // delivered to the caller; the nanosecond form wins when both
            // options are enabled.
            let elapsed = SystemTime::now()
                .duration_since(&SystemTime::UNIX_EPOCH)
                .unwrap_or_default();
            if self.timestamp_ns.load(Ordering::Relaxed) {
                control_messages.push(ControlMessage::Socket(SocketControlMessage::TimestampNs(
                    timespec_t {
                        sec: elapsed.as_secs() as time_t,
                        nsec: elapsed.subsec_nanos() as i64,
                    },
                )));
            } else {
                control_messages.push(ControlMessage::Socket(SocketControlMessage::Timestamp(
                    timeval_t {
                        sec: elapsed.as_secs() as time_t,
                        usec: elapsed.subsec_micros() as suseconds_t,
                    },
                )));
            }
        }
        if self.recv_ttl.load(Ordering::Relaxed) {
            control_messages.push(ControlMessage::Socket(SocketControlMessage::IpTtl(
                i32::from(hop_limit),
            )));
        }

        Ok((
            output,
            MessageHeader::new(Some(peer_addr), control_messages),
        ))
    }

    fn get_option(&self, option: &mut dyn SocketOption) -> Result<()> {
        sock_option_mut!(match option {
            // A ping socket never reports asynchronous errors; the ICMP
            // errors themselves arrive as queued messages.
            socket_errors @ SocketError => {
                socket_errors.set(None);
                return Ok(());
            }
            ttl @ Ttl => {
                ttl.set(IpTtl::new(NonZeroU8::new(self.ttl.load(Ordering::Relaxed))));
            }
            recv_ttl @ RecvTtl => {
                recv_ttl.set(self.recv_ttl.load(Ordering::Relaxed));
            }
            recv_opts @ RecvOpts => {
                recv_opts.set(self.recv_opts.load(Ordering::Relaxed));
            }
            timestamp @ Timestamp => {
                timestamp.set(self.timestamp.load(Ordering::Relaxed));
            }
            timestamp_ns @ TimestampNs => {
                timestamp_ns.set(self.timestamp_ns.load(Ordering::Relaxed));
            }
            socket_type @ SocketType => {
                socket_type.set(SockType::SOCK_DGRAM);
            }
            _ => {
                return_errno_with_message!(Errno::ENOPROTOOPT, "the ICMP socket option is unknown")
            }
        });

        Ok(())
    }

    fn set_option(&self, option: &dyn SocketOption) -> Result<()> {
        sock_option_ref!(match option {
            ttl @ Ttl => {
                let ttl = ttl.get().unwrap().get();
                self.ttl.store(ttl, Ordering::Relaxed);
                self.inner.set_hop_limit(ttl);
            }
            recv_ttl @ RecvTtl => {
                self.recv_ttl
                    .store(*recv_ttl.get().unwrap(), Ordering::Relaxed);
            }
            recv_opts @ RecvOpts => {
                self.recv_opts
                    .store(*recv_opts.get().unwrap(), Ordering::Relaxed);
            }
            timestamp @ Timestamp => {
                self.timestamp
                    .store(*timestamp.get().unwrap(), Ordering::Relaxed);
            }
            timestamp_ns @ TimestampNs => {
                self.timestamp_ns
                    .store(*timestamp_ns.get().unwrap(), Ordering::Relaxed);
            }
            _ => {
                return_errno_with_message!(Errno::ENOPROTOOPT, "the ICMP socket option is unknown")
            }
        });

        Ok(())
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }
}

impl core::fmt::Debug for IcmpSocket {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("IcmpSocket")
            .field("ident", &self.inner.ident())
            .finish_non_exhaustive()
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn ephemeral_idents_are_neither_zero_nor_reused_immediately() {
        let first = NEXT_ICMP_IDENT.fetch_add(1, Ordering::Relaxed);
        let second = NEXT_ICMP_IDENT.fetch_add(1, Ordering::Relaxed);
        assert_ne!(first, 0);
        assert_eq!(second, first.wrapping_add(1));
    }
}
