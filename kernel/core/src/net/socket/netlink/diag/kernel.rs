// SPDX-License-Identifier: MPL-2.0

//! Kernel side of `NETLINK_SOCK_DIAG`.
//!
//! The handler answers `SOCK_DIAG_BY_FAMILY` dumps from the inet socket
//! registry. Single-socket lookups use the same registry and are answered as a
//! filtered dump.

use super::message::{
    AF_INET, AF_INET6, IPPROTO_TCP, IPPROTO_UDP, InetDiagReqV2, SOCK_DIAG_BY_FAMILY,
    SockDiagMessage, build_response,
};
use crate::{
    net::socket::{
        SocketAddr,
        ip::{InetProtocol, InetSocketSnapshot, inet_sockets},
        netlink::{
            addr::PortNum,
            message::{CMsgSegHdr, DoneSegment},
            table::{NetlinkSockDiagProtocol, SupportedNetlinkProtocol},
        },
    },
    prelude::*,
};

pub(super) fn handle_request(
    request_header: &CMsgSegHdr,
    request: &InetDiagReqV2,
    dst_port: PortNum,
) {
    debug!("sock_diag request: {:?}", request);

    let mut response = SockDiagMessage::new();

    if request_header.type_ == SOCK_DIAG_BY_FAMILY {
        for socket in inet_sockets() {
            if !matches_request(&socket, request) {
                continue;
            }
            response.push_response(build_response(request_header, &socket));
        }
    }

    // A dump always terminates with `NLMSG_DONE`, including when nothing
    // matched; otherwise `ss` keeps waiting for the end of the dump.
    response.push_done(DoneSegment::new_from_request(request_header, None));
    NetlinkSockDiagProtocol::unicast(dst_port, response).unwrap();
}

fn matches_request(socket: &InetSocketSnapshot, request: &InetDiagReqV2) -> bool {
    let family_matches = match &socket.local {
        SocketAddr::IPv4(..) => request.family == AF_INET,
        SocketAddr::IPv6(..) => request.family == AF_INET6,
        _ => false,
    };
    if !family_matches {
        return false;
    }

    let protocol_matches = match socket.protocol {
        InetProtocol::Tcp => request.protocol == 0 || request.protocol == IPPROTO_TCP,
        InetProtocol::Udp => request.protocol == 0 || request.protocol == IPPROTO_UDP,
    };
    if !protocol_matches {
        return false;
    }

    if request.states != 0 {
        // `idiag_states` has one bit per Linux TCP state and sets bit
        // `sk_state` (not `sk_state - 1`): TCP_LISTEN (10) is bit 10.
        if socket.state >= 32 || request.states & (1u32 << socket.state) == 0 {
            return false;
        }
    }

    true
}
