// SPDX-License-Identifier: MPL-2.0

//! This module defines the kernel socket,
//! which is responsible for handling requests from user space.

use core::marker::PhantomData;

use super::message::{RtnlMessage, RtnlSegment};
use crate::{
    net::socket::netlink::{
        addr::PortNum,
        message::{ErrorSegment, ProtocolSegment, SegHdrCommonFlags},
        table::{NetlinkRouteProtocol, SupportedNetlinkProtocol},
    },
    prelude::*,
};

mod addr;
mod link;
mod util;

pub(super) struct NetlinkRouteKernelSocket {
    _private: PhantomData<()>,
}

impl NetlinkRouteKernelSocket {
    const fn new() -> Self {
        Self {
            _private: PhantomData,
        }
    }

    pub(super) fn handle_request(&self, request: &RtnlSegment, dst_port: PortNum) {
        debug!("netlink route request: {:?}", request);

        let request_header = request.header();

        let response_segments = match request {
            RtnlSegment::NewLink(request_segment) => link::do_new_link(request_segment),
            RtnlSegment::GetLink(request_segment) => link::do_get_link(request_segment),
            RtnlSegment::GetAddr(request_segment) => addr::do_get_addr(request_segment),
            RtnlSegment::SetLink(request_segment) => link::do_set_link(request_segment),
            RtnlSegment::NewAddr(request_segment) => addr::do_new_addr(request_segment),
            _ => Err(Error::with_message(
                Errno::EOPNOTSUPP,
                "the netlink route request is not supported",
            )),
        };

        let mut response_segments = match response_segments {
            Ok(segments) => {
                // A successful request that produces no response segments
                // (e.g. RTM_SETLINK/RTM_NEWADDR) is acknowledged with an empty
                // NLMSG_ERROR when the ACK flag is set, as Linux does.
                let ack_requested = SegHdrCommonFlags::from_bits_truncate(request_header.flags)
                    .contains(SegHdrCommonFlags::ACK);
                if segments.is_empty() && ack_requested {
                    let ack_segment = ErrorSegment::new_from_request(request_header, None);
                    self.report_error(ack_segment, dst_port);
                    return;
                }
                segments
            }
            Err(error) => {
                // TODO: Deal with the `NetlinkMessageCommonFlags::ACK` flag.
                // Should we return `ErrorSegment` if ACK flag does not exist?
                // Reference: <https://docs.kernel.org/userspace-api/netlink/intro.html#netlink-message-types>.
                let err_segment = ErrorSegment::new_from_request(request_header, Some(error));
                self.report_error(err_segment, dst_port);
                return;
            }
        };

        // Modern Linux may place the `NLMSG_DONE` segment
        // in the same message as the preceding segments,
        // depending on the message type.
        // Legacy Linux generally delivered the `NLMSG_DONE` segment
        // in a separate message.
        // We follow the legacy Linux behavior here because it is simpler.
        // Reference: <https://elixir.bootlin.com/linux/v7.1/source/net/core/rtnetlink.c#L6870>.
        let done_segments = if matches!(response_segments.last(), Some(RtnlSegment::Done(_))) {
            response_segments.split_off(response_segments.len() - 1)
        } else {
            Vec::new()
        };

        for segments in [response_segments, done_segments] {
            if segments.is_empty() {
                continue;
            }

            let response = RtnlMessage::new(segments);
            debug!("netlink route response: {:?}", response);

            NetlinkRouteProtocol::unicast(dst_port, response).unwrap();
        }
    }

    pub(super) fn report_error(&self, err_segment: ErrorSegment, dst_port: PortNum) {
        let response = RtnlMessage::new(vec![RtnlSegment::Error(err_segment)]);

        debug!("netlink route error: {:?}", response);

        NetlinkRouteProtocol::unicast(dst_port, response).unwrap();
    }
}

/// FIXME: NETLINK_ROUTE_KERNEL should be a per-network namespace socket
static NETLINK_ROUTE_KERNEL: NetlinkRouteKernelSocket = NetlinkRouteKernelSocket::new();

pub(super) fn get_netlink_route_kernel() -> &'static NetlinkRouteKernelSocket {
    &NETLINK_ROUTE_KERNEL
}
