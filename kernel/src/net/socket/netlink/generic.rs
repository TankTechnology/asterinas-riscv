// SPDX-License-Identifier: MPL-2.0

//! Minimal generic-netlink transport.
//!
//! Generic netlink is used by systemd-networkd for optional family discovery
//! (for example, nl80211).  Asterinas does not yet expose kernel generic
//! families, but the socket itself must be usable so networkd can start and
//! continue with the route netlink API.  Requests are consumed and reported
//! as unsupported by the userspace transaction; no fake family data is
//! generated.

use crate::{
    events::IoEvents,
    net::socket::{
        netlink::{
            NetlinkSocketAddr, common::BoundNetlink, receiver::QueueableMessage,
            table::NetlinkGenericProtocol,
        },
        util::{RecvFlags, RecvOutput, SendFlags, datagram_common},
    },
    prelude::*,
    util::{MultiRead, MultiWrite},
};

pub type NetlinkGenericSocket = super::common::NetlinkSocket<NetlinkGenericProtocol>;

impl QueueableMessage for () {
    fn total_len(&self) -> usize {
        0
    }
}

pub(super) type BoundNetlinkGeneric = BoundNetlink<()>;

impl datagram_common::Bound for BoundNetlinkGeneric {
    type Endpoint = NetlinkSocketAddr;

    fn local_endpoint(&self) -> Self::Endpoint {
        self.handle.addr()
    }

    fn bind(&mut self, endpoint: &Self::Endpoint) -> Result<()> {
        self.bind_common(endpoint)
    }

    fn remote_endpoint(&self) -> Option<&Self::Endpoint> {
        Some(&self.remote_addr)
    }

    fn set_remote_endpoint(&mut self, endpoint: &Self::Endpoint) {
        self.remote_addr = *endpoint;
    }

    fn try_send(
        &self,
        reader: &mut dyn MultiRead,
        remote: &Self::Endpoint,
        flags: SendFlags,
    ) -> Result<usize> {
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }
        if *remote != NetlinkSocketAddr::new_unspecified() {
            return_errno_with_message!(
                Errno::ECONNREFUSED,
                "sending generic netlink messages to userspace is not supported"
            );
        }

        // There are no generic-netlink kernel families yet.  Returning
        // EOPNOTSUPP makes sd-netlink classify discovery as an optional,
        // unsupported family instead of treating a malformed route reply as
        // EINVAL and aborting systemd-networkd startup.
        let _ = reader.sum_lens();
        return_errno_with_message!(
            Errno::EOPNOTSUPP,
            "generic netlink families are not implemented"
        );
    }

    fn try_recv(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, Self::Endpoint)> {
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }
        let mut receive_queue = self.receive_queue.lock();
        receive_queue.dequeue_if(|_, response_len| {
            let copied_len = response_len.min(writer.sum_lens());
            let should_dequeue = flags.receive_behavior().will_consume_data();
            let output = RecvOutput::new_for_packet(flags, copied_len, response_len);
            Ok((
                should_dequeue,
                (output, NetlinkSocketAddr::new_unspecified()),
            ))
        })
    }

    fn check_io_events(&self) -> IoEvents {
        self.check_io_events_common()
    }
}
