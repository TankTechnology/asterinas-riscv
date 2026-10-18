// SPDX-License-Identifier: MPL-2.0

//! `NETLINK_SOCK_DIAG` messages.
//!
//! The wire format is binary and does not use netlink attributes for the
//! fields this kernel reports. A request is `nlmsghdr` plus
//! `struct inet_diag_req_v2`; a response is `nlmsghdr` plus
//! `struct inet_diag_msg`.
//!
//! Reference: <https://man7.org/linux/man-pages/man7/sock_diag.7.html>.

use crate::{
    net::socket::{
        SocketAddr,
        ip::InetSocketSnapshot,
        netlink::{
            message::{CMsgSegHdr, DoneSegment, NoAttr, SegHdrCommonFlags, SegmentCommon},
            receiver::QueueableMessage,
        },
    },
    prelude::*,
    util::MultiWrite,
};

/// `SOCK_DIAG_BY_FAMILY`, the only request type this implementation answers.
pub(crate) const SOCK_DIAG_BY_FAMILY: u16 = 20;

pub(crate) const AF_INET: u8 = 2;
pub(crate) const AF_INET6: u8 = 10;
pub(crate) const IPPROTO_TCP: u8 = 6;
pub(crate) const IPPROTO_UDP: u8 = 17;

/// `struct inet_diag_sockid`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(crate) struct InetDiagSockId {
    /// Source port in network byte order.
    pub sport: u16,
    /// Destination port in network byte order.
    pub dport: u16,
    pub src: [u32; 4],
    pub dst: [u32; 4],
    pub if_: u32,
    pub cookie: [u32; 2],
}

const _: () = assert!(size_of::<InetDiagSockId>() == 48);

/// `struct inet_diag_req_v2`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(crate) struct InetDiagReqV2 {
    pub family: u8,
    pub protocol: u8,
    pub ext: u8,
    pub pad: u8,
    /// Bit mask of the states to dump.
    pub states: u32,
    pub id: InetDiagSockId,
}

const _: () = assert!(size_of::<InetDiagReqV2>() == 56);

/// `struct inet_diag_msg`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(crate) struct InetDiagMsg {
    pub family: u8,
    pub state: u8,
    pub timer: u8,
    pub retrans: u8,
    pub id: InetDiagSockId,
    pub expires: u32,
    pub rqueue: u32,
    pub wqueue: u32,
    pub uid: u32,
    pub inode: u32,
}

const _: () = assert!(size_of::<InetDiagMsg>() == 72);

impl crate::net::socket::netlink::message::SegmentBody for InetDiagMsg {
    type CType = Self;
}

pub(super) type SockDiagResponse = SegmentCommon<InetDiagMsg, NoAttr>;

/// A dump response: zero or more socket records followed by `NLMSG_DONE`.
#[derive(Debug)]
pub(crate) struct SockDiagMessage {
    segments: Vec<SockDiagSegment>,
}

#[derive(Debug)]
enum SockDiagSegment {
    Response(SockDiagResponse),
    Done(DoneSegment),
}

impl SockDiagMessage {
    pub(crate) fn new() -> Self {
        Self {
            segments: Vec::new(),
        }
    }

    pub(crate) fn push_response(&mut self, response: SockDiagResponse) {
        self.segments.push(SockDiagSegment::Response(response));
    }

    pub(crate) fn push_done(&mut self, done: DoneSegment) {
        self.segments.push(SockDiagSegment::Done(done));
    }

    pub(crate) fn write_to(&self, writer: &mut dyn MultiWrite) -> Result<()> {
        for segment in self.segments.iter() {
            match segment {
                SockDiagSegment::Response(response) => response.write_to(writer)?,
                SockDiagSegment::Done(done) => done.write_to(writer)?,
            }
        }
        Ok(())
    }
}

impl QueueableMessage for SockDiagMessage {
    fn total_len(&self) -> usize {
        self.segments
            .iter()
            .map(|segment| match segment {
                SockDiagSegment::Response(response) => response.total_len(),
                SockDiagSegment::Done(done) => done.header().len as usize,
            })
            .sum()
    }
}

/// Builds one `inet_diag_msg` for a socket snapshot.
pub(crate) fn build_response(
    request_header: &CMsgSegHdr,
    socket: &InetSocketSnapshot,
) -> SockDiagResponse {
    let (family, id) = socket_identity(socket);
    let header = CMsgSegHdr {
        len: 0,
        type_: SOCK_DIAG_BY_FAMILY,
        flags: SegHdrCommonFlags::MULTI.bits(),
        seq: request_header.seq,
        pid: request_header.pid,
    };
    let body = InetDiagMsg {
        family,
        state: socket.state,
        timer: 0,
        retrans: 0,
        id,
        expires: 0,
        rqueue: 0,
        wqueue: 0,
        uid: socket.uid,
        inode: socket.inode as u32,
    };

    SockDiagResponse::new(header, body, Vec::new())
}

fn socket_identity(socket: &InetSocketSnapshot) -> (u8, InetDiagSockId) {
    match &socket.local {
        SocketAddr::IPv4(local_ip, local_port) => {
            let (dst, dport) = match &socket.remote {
                Some(SocketAddr::IPv4(ip, port)) => (ipv4_words(*ip), *port),
                _ => ([0; 4], 0),
            };
            (
                AF_INET,
                InetDiagSockId {
                    sport: local_port.to_be(),
                    dport: dport.to_be(),
                    src: ipv4_words(*local_ip),
                    dst,
                    if_: 0,
                    cookie: [0; 2],
                },
            )
        }
        SocketAddr::IPv6(local_ip, local_port) => {
            let (dst, dport) = match &socket.remote {
                Some(SocketAddr::IPv6(ip, port)) => (ipv6_words(*ip), *port),
                _ => ([0; 4], 0),
            };
            (
                AF_INET6,
                InetDiagSockId {
                    sport: local_port.to_be(),
                    dport: dport.to_be(),
                    src: ipv6_words(*local_ip),
                    dst,
                    if_: 0,
                    cookie: [0; 2],
                },
            )
        }
        _ => (
            AF_INET,
            InetDiagSockId {
                sport: 0,
                dport: 0,
                src: [0; 4],
                dst: [0; 4],
                if_: 0,
                cookie: [0; 2],
            },
        ),
    }
}

fn ipv4_words(addr: aster_bigtcp::wire::Ipv4Address) -> [u32; 4] {
    // The `src`/`dst` fields are `__be32` arrays; storing the address through
    // `from_ne_bytes` makes the in-memory bytes match the network order the
    // user-space tools expect on a little-endian machine.
    [u32::from_ne_bytes(addr.octets()), 0, 0, 0]
}

fn ipv6_words(addr: aster_bigtcp::wire::Ipv6Address) -> [u32; 4] {
    let octets = addr.octets();
    let mut words = [0u32; 4];
    for (index, chunk) in octets.chunks_exact(4).enumerate() {
        words[index] = u32::from_ne_bytes(chunk.try_into().unwrap());
    }
    words
}
