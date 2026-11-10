// SPDX-License-Identifier: MPL-2.0

use aster_bigtcp::{
    socket::{NeedIfacePoll, UDP_RECV_PAYLOAD_LEN},
    wire::IpEndpoint,
};
use bound::BoundDatagram;
use unbound::{BindOptions, UnboundDatagram};

use super::addr::{UNSPECIFIED_LOCAL_ENDPOINT, is_ipv4_mapped};
use crate::{
    events::IoEvents,
    fs::{
        file::{FileCommon, StatusFlags},
        pseudofs::SockFs,
    },
    net::{
        iface::is_broadcast_endpoint,
        net_ns::{NetNamespace, current_net_ns},
        socket::{
            Socket,
            ip::{
                IpAddressFamily,
                options::{IpOptionSet, Ipv6OptionSet, SetIpLevelOption},
            },
            options::{Error as SocketError, SocketOption, macros::sock_option_mut},
            private::SocketPrivate,
            proc_net::{InetProtocol, InetSocketEntry, ProcNetEntry, ProcNetSocket},
            util::{
                ControlMessage, MessageHeader, RecvFlags, RecvOutput, SendFlags, SocketAddr,
                SocketControlMessage,
                datagram_common::{Bound, Inner, select_remote_and_bind},
                options::{
                    GetSocketLevelOption, SetSocketLevelOption, SocketOptionSet, SocketTimeouts,
                },
            },
        },
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
    time::{SystemTime, suseconds_t, time_t, timespec_t, timeval_t},
    util::{MultiRead, MultiWrite, net::SockType},
};

mod bound;
pub(super) mod observer;
mod unbound;

pub struct DatagramSocket {
    // Lock order: `inner` first, `options` second
    inner: RwMutex<Inner<UnboundDatagram, BoundDatagram>>,
    family: IpAddressFamily,
    options: RwLock<OptionSet>,
    timeouts: SocketTimeouts,

    pollee: Pollee,
    net_ns: Arc<NetNamespace>,
    common: FileCommon,
}

#[derive(Clone, Debug)]
struct OptionSet {
    socket: SocketOptionSet,
    ip: IpOptionSet,
    ipv6: Ipv6OptionSet,
    // TODO: UDP option set
}

impl OptionSet {
    fn new() -> Self {
        let socket = SocketOptionSet::new_udp();
        let ip = IpOptionSet::new_udp();
        let ipv6 = Ipv6OptionSet::new();
        OptionSet { socket, ip, ipv6 }
    }
}

impl DatagramSocket {
    pub fn new(is_nonblocking: bool, family: IpAddressFamily) -> Arc<Self> {
        let unbound_datagram = UnboundDatagram::new();
        let status_flags = if is_nonblocking {
            StatusFlags::O_NONBLOCK
        } else {
            StatusFlags::empty()
        };
        let net_ns = current_net_ns();
        let socket = Arc::new(Self {
            inner: RwMutex::new(Inner::Unbound(unbound_datagram)),
            family,
            options: RwLock::new(OptionSet::new()),
            timeouts: SocketTimeouts::new(),
            pollee: Pollee::new(),
            net_ns,
            common: FileCommon::new(SockFs::new_path(), status_flags),
        });
        let socket_object: Arc<dyn ProcNetSocket> = socket.clone();
        let weak = Arc::downgrade(&socket_object);
        socket.net_ns.proc_net_sockets().register(weak);
        socket
    }

    fn try_recv(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, SocketAddr)> {
        let result = self
            .inner
            .read()
            .try_recv(writer, flags)
            .map(|(output, remote_endpoint)| (output, remote_endpoint.into()))?;
        self.pollee.invalidate();

        Ok(result)
    }

    fn try_send(
        &self,
        reader: &mut dyn MultiRead,
        remote: Option<&IpEndpoint>,
        flags: SendFlags,
    ) -> Result<usize> {
        let (sent_bytes, iface_to_poll) = select_remote_and_bind(
            &self.inner,
            remote,
            || {
                let remote_endpoint = remote.ok_or_else(|| {
                    Error::with_message(
                        Errno::EDESTADDRREQ,
                        "the destination address is not specified",
                    )
                })?;
                self.inner.write().bind_ephemeral(
                    remote_endpoint,
                    &self.pollee,
                    BindOptions {
                        can_reuse: false,
                        v6only: self.options.read().ipv6.v6only(),
                        net_ns: self.net_ns.clone(),
                    },
                )
            },
            |bound_datagram, remote_endpoint| {
                let sent_bytes = bound_datagram.try_send(reader, remote_endpoint, flags)?;
                let iface_to_poll = bound_datagram.iface().clone();
                Ok((sent_bytes, iface_to_poll))
            },
        )?;

        self.pollee.invalidate();
        iface_to_poll.poll();

        Ok(sent_bytes)
    }
}

impl Pollable for DatagramSocket {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee
            .poll_with(mask, poller, || self.inner.read().check_io_events())
    }
}

impl SocketPrivate for DatagramSocket {
    fn is_nonblocking(&self) -> bool {
        self.common.is_nonblocking()
    }
}

impl Socket for DatagramSocket {
    fn net_ns(&self) -> &NetNamespace {
        &self.net_ns
    }

    fn max_recv_len(&self) -> Option<usize> {
        Some(UDP_RECV_PAYLOAD_LEN)
    }

    fn bind(&self, socket_addr: SocketAddr) -> Result<()> {
        let endpoint = socket_addr.try_into()?;
        self.check_endpoint_family(&endpoint)?;
        let can_reuse = self.options.read().socket.reuse_addr();

        self.inner.write().bind(
            &endpoint,
            &self.pollee,
            BindOptions {
                can_reuse,
                v6only: self.options.read().ipv6.v6only(),
                net_ns: self.net_ns.clone(),
            },
        )
    }

    fn connect(&self, socket_addr: SocketAddr) -> Result<()> {
        let endpoint = socket_addr.try_into()?;
        self.check_endpoint_family(&endpoint)?;
        let can_broadcast = self.options.read().socket.broadcast();
        if !can_broadcast && is_broadcast_endpoint(&endpoint) {
            return_errno_with_message!(
                Errno::EACCES,
                "connecting to a broadcast address without SO_BROADCAST is not allowed"
            );
        }

        self.inner.write().connect(
            &endpoint,
            &self.pollee,
            BindOptions {
                can_reuse: false,
                v6only: self.options.read().ipv6.v6only(),
                net_ns: self.net_ns.clone(),
            },
        )
    }

    fn addr(&self) -> Result<SocketAddr> {
        let endpoint = self
            .inner
            .read()
            .addr()
            .unwrap_or(UNSPECIFIED_LOCAL_ENDPOINT);

        Ok(endpoint.into())
    }

    fn peer_addr(&self) -> Result<SocketAddr> {
        let endpoint =
            *self.inner.read().peer_addr().ok_or_else(|| {
                Error::with_message(Errno::ENOTCONN, "the socket is not connected")
            })?;

        Ok(endpoint.into())
    }

    fn sendmsg(
        &self,
        reader: &mut dyn MultiRead,
        message_header: MessageHeader,
        flags: SendFlags,
    ) -> Result<usize> {
        // TODO: Deal with flags
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let MessageHeader {
            addr,
            control_messages,
        } = message_header;

        let endpoint = match addr {
            Some(addr) => Some(addr.try_into()?),
            None => None,
        };

        if let Some(endpoint) = endpoint.as_ref() {
            self.check_endpoint_family(endpoint)?;
            let can_broadcast = self.options.read().socket.broadcast();
            if !can_broadcast && is_broadcast_endpoint(endpoint) {
                return_errno_with_message!(
                    Errno::EACCES,
                    "sending to a broadcast address without SO_BROADCAST is not allowed"
                );
            }
        }

        if !control_messages.is_empty() {
            // TODO: Support sending control message
            warn!("sending control message is not supported");
        }

        // TODO: Block if the send buffer is full
        self.try_send(reader, endpoint.as_ref(), flags)
    }

    fn recvmsg(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, MessageHeader)> {
        // TODO: Deal with flags
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let (output, peer_addr) = if flags.contains(RecvFlags::MSG_DONTWAIT) {
            self.try_recv(writer, flags)?
        } else {
            self.block_on(IoEvents::IN, self.timeouts.recv_timeout(), || {
                self.try_recv(writer, flags)
            })?
        };

        let control_messages = {
            let options = self.options.read();
            let socket = &options.socket;
            if socket.timestamp() || socket.timestamp_ns() {
                // The timestamp is taken when the packet is delivered to the
                // caller, not when it arrived at the device; queueing delay is
                // therefore part of the value. Arrival-time stamping would
                // have to live in the network stack (smoltcp).
                let elapsed = SystemTime::now()
                    .duration_since(&SystemTime::UNIX_EPOCH)
                    .unwrap_or_default();
                let sec = elapsed.as_secs() as time_t;
                // Like Linux, a socket with both options set reports the
                // nanosecond variant only, so the caller sees one cmsg.
                let message = if socket.timestamp_ns() {
                    SocketControlMessage::TimestampNs(timespec_t {
                        sec,
                        nsec: elapsed.subsec_nanos() as i64,
                    })
                } else {
                    SocketControlMessage::Timestamp(timeval_t {
                        sec,
                        usec: elapsed.subsec_micros() as suseconds_t,
                    })
                };
                vec![ControlMessage::Socket(message)]
            } else {
                Vec::new()
            }
        };

        let message_header = MessageHeader::new(Some(peer_addr), control_messages);

        Ok((output, message_header))
    }

    fn get_option(&self, option: &mut dyn SocketOption) -> Result<()> {
        sock_option_mut!(match option {
            socket_errors @ SocketError => {
                // TODO: Support socket errors for UDP sockets
                socket_errors.set(None);
                return Ok(());
            }
            _ => (),
        });

        let inner = self.inner.read();
        let options = self.options.read();

        // Deal with socket-level options
        match options
            .socket
            .get_option(option, &(&*inner, &self.timeouts))
        {
            Err(err) if err.error() == Errno::ENOPROTOOPT => (),
            res => return res,
        }

        // Deal with IP-level options
        match options.ip.get_option(option) {
            Err(err) if err.error() == Errno::ENOPROTOOPT => {
                if self.family == IpAddressFamily::IPv6 {
                    options.ipv6.get_option(option)
                } else {
                    Err(err)
                }
            }
            result => result,
        }
    }

    fn set_option(&self, option: &dyn SocketOption) -> Result<()> {
        let inner = self.inner.read();
        let mut options = self.options.write();

        // Deal with socket-level options
        let need_iface_poll = match options
            .socket
            .set_option(option, &(&*inner, &self.timeouts))
        {
            Err(err) if err.error() == Errno::ENOPROTOOPT => {
                // Deal with IP-level options
                match options.ip.set_option(option, &*inner) {
                    Err(err) if err.error() == Errno::ENOPROTOOPT => {
                        if self.family == IpAddressFamily::IPv6 {
                            let is_socket_bound = matches!(&*inner, Inner::Bound(_));
                            options.ipv6.set_option(option, is_socket_bound)?;
                            NeedIfacePoll::FALSE
                        } else {
                            return Err(err);
                        }
                    }
                    result => result?,
                }
            }
            Err(err) => return Err(err),
            Ok(need_iface_poll) => need_iface_poll,
        };

        let iface_to_poll = need_iface_poll
            .then(|| match &*inner {
                Inner::Unbound(_) => None,
                Inner::Bound(bound_datagram) => Some(bound_datagram.iface().clone()),
            })
            .flatten();

        drop(inner);
        drop(options);

        if let Some(iface) = iface_to_poll {
            iface.poll();
        }

        Ok(())
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }
}

impl DatagramSocket {
    fn check_endpoint_family(&self, endpoint: &IpEndpoint) -> Result<()> {
        let is_family_mismatch = IpAddressFamily::from(endpoint.addr) != self.family;
        let is_disallowed_mapped_address = self.family == IpAddressFamily::IPv6
            && self.options.read().ipv6.v6only()
            && is_ipv4_mapped(endpoint.addr);
        if is_family_mismatch || is_disallowed_mapped_address {
            return_errno_with_message!(
                Errno::EAFNOSUPPORT,
                "the protocol family does not match the address family"
            );
        }
        Ok(())
    }
}

impl GetSocketLevelOption for (&Inner<UnboundDatagram, BoundDatagram>, &SocketTimeouts) {
    fn socket_type(&self) -> SockType {
        SockType::SOCK_DGRAM
    }

    fn is_listening(&self) -> bool {
        false
    }

    fn socket_timeouts(&self) -> Option<&SocketTimeouts> {
        Some(self.1)
    }
}

impl SetSocketLevelOption for (&Inner<UnboundDatagram, BoundDatagram>, &SocketTimeouts) {
    fn set_reuse_addr(&self, reuse_addr: bool) {
        let Inner::Bound(bound) = self.0 else {
            return;
        };

        bound.bound_port().set_can_reuse(reuse_addr);
    }

    fn socket_timeouts(&self) -> Option<&SocketTimeouts> {
        Some(self.1)
    }
}

impl SetIpLevelOption for Inner<UnboundDatagram, BoundDatagram> {
    fn set_hdrincl(&self, _hdrincl: bool) -> Result<()> {
        return_errno_with_message!(
            Errno::ENOPROTOOPT,
            "IP_HDRINCL cannot be set on UDP sockets"
        );
    }
}

impl ProcNetSocket for DatagramSocket {
    fn proc_net_entries(&self, out: &mut Vec<ProcNetEntry>) {
        let inner = self.inner.read();
        // An unbound UDP socket is not in the kernel's UDP hash table, so
        // Linux does not list it either.
        let Some(local) = inner.addr() else {
            return;
        };
        let remote = inner.peer_addr().copied();
        out.push(ProcNetEntry::Inet(InetSocketEntry {
            protocol: InetProtocol::Udp,
            local,
            remote,
            // TCP_CLOSE for an unconnected UDP socket, TCP_ESTABLISHED for
            // one that `connect()` has given a peer, as Linux reports.
            state: if remote.is_some() { 0x01 } else { 0x07 },
            inode: self.common.path().inode().ino(),
            uid: 0,
        }));
    }
}
