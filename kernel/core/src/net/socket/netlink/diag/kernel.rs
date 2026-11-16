// SPDX-License-Identifier: MPL-2.0

//! The kernel side of `SOCK_DIAG`: answers `SOCK_DIAG_BY_FAMILY` from the same
//! socket registry `/proc/net` reads.

use core::marker::PhantomData;

use crate::{
    net::{
        net_ns::current_net_ns,
        socket::{
            netlink::{
                addr::PortNum,
                diag::message::{CDiagMsg, DiagSegment},
                message::{DoneSegment, ErrorSegment, ProtocolSegment, SegHdrCommonFlags},
                table::{NetlinkDiagProtocol, SupportedNetlinkProtocol},
            },
            proc_net::{InetProtocol, ProcNetEntry},
        },
    },
    prelude::*,
};

pub(super) struct NetlinkDiagKernelSocket {
    _private: PhantomData<()>,
}

static NETLINK_DIAG_KERNEL: NetlinkDiagKernelSocket = NetlinkDiagKernelSocket {
    _private: PhantomData,
};

pub(super) fn get_netlink_diag_kernel() -> &'static NetlinkDiagKernelSocket {
    &NETLINK_DIAG_KERNEL
}

impl NetlinkDiagKernelSocket {
    /// Answers one parsed request. A request with no matching sockets still
    /// gets a `NLMSG_DONE`, which is what user space waits for.
    pub(super) fn handle_request(&self, request: &DiagSegment, dst_port: PortNum) {
        let Some(req) = request.request() else {
            return;
        };
        let request_header = *request.header();

        let net_ns = current_net_ns();
        let mut replies = Vec::new();
        for entry in net_ns.proc_net_sockets().snapshot() {
            let ProcNetEntry::Inet(inet) = entry else {
                continue;
            };
            let protocol = match inet.protocol {
                InetProtocol::Tcp => CDiagMsg::PROTOCOL_TCP,
                InetProtocol::Udp => CDiagMsg::PROTOCOL_UDP,
            };
            if req.protocol() != 0 && req.protocol() != protocol {
                continue;
            }
            if let Some(port) = req.filter_source_port()
                && inet.local.port != port
            {
                continue;
            }
            if let Some(port) = req.filter_destination_port()
                && inet.remote.map(|remote| remote.port) != Some(port)
            {
                continue;
            }
            // A zero state mask means every state, as in the uapi.
            if req.states() != 0 && (req.states() >> inet.state) & 1 == 0 {
                continue;
            }

            let IpAddress::Ipv4(local_ipv4) = inet.local.addr else {
                continue;
            };
            let (remote_port, remote_addr) = match inet.remote {
                Some(remote) => match remote.addr {
                    IpAddress::Ipv4(remote_ipv4) => (remote.port, remote_ipv4.octets()),
                    IpAddress::Ipv6(_) => continue,
                },
                None => (0, [0u8; 4]),
            };
            replies.push(CDiagMsg::new(
                CDiagMsg::AF_INET,
                inet.state,
                inet.local.port,
                remote_port,
                local_ipv4.octets(),
                remote_addr,
                inet.uid,
                inet.inode as u32,
            ));
        }

        let multi = if replies.is_empty() {
            0
        } else {
            SegHdrCommonFlags::MULTI.bits()
        };
        let mut segments: Vec<DiagSegment> = replies
            .into_iter()
            .map(|reply| DiagSegment::new_reply(&request_header, reply))
            .collect();
        for segment in segments.iter_mut() {
            segment.header_mut().flags |= multi;
        }
        let mut done = DoneSegment::new_from_request(&request_header, None);
        done.header_mut().flags |= multi;
        segments.push(DiagSegment::Done(done));

        let message = super::DiagMessage::new(segments);
        if let Err(error) = NetlinkDiagProtocol::unicast(dst_port, message) {
            debug!("failed to deliver a diag response: {:?}", error);
        }
    }

    pub(super) fn report_error(&self, err_segment: ErrorSegment, dst_port: PortNum) {
        let message = super::DiagMessage::new(vec![DiagSegment::Error(err_segment)]);
        if let Err(error) = NetlinkDiagProtocol::unicast(dst_port, message) {
            debug!("failed to deliver a diag error: {:?}", error);
        }
    }
}

use aster_bigtcp::wire::IpAddress;
