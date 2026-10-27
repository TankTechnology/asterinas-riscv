// SPDX-License-Identifier: MPL-2.0

//! Minimal generic-netlink transport.
//!
//! systemd-networkd opens a `NETLINK_GENERIC` socket while setting up its
//! manager, even when no generic-netlink family is used by the configured
//! interfaces.  Asterinas does not yet expose a generic-netlink family, but
//! rejecting the socket at creation time prevents networkd from starting at
//! all.  This transport provides the socket, bind, and option plumbing;
//! family-specific request handling remains a follow-up.

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

#[derive(Clone, Debug)]
pub struct GenericMessage {
    len: usize,
}

impl QueueableMessage for GenericMessage {
    fn total_len(&self) -> usize {
        self.len
    }
}

pub type NetlinkGenericSocket = super::common::NetlinkSocket<NetlinkGenericProtocol>;

type BoundNetlinkGeneric = BoundNetlink<GenericMessage>;

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
                "sending generic netlink messages to user space is not supported"
            );
        }

        // No generic-netlink family is registered yet. Consume the request
        // so manager setup receives normal datagram send semantics instead
        // of failing at socket creation time with EAFNOSUPPORT.
        let len = reader.sum_lens();
        reader.skip_some(len);
        Ok(len)
    }

    fn try_recv(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, NetlinkSocketAddr)> {
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let mut receive_queue = self.receive_queue.lock();
        receive_queue.dequeue_if(|_response, response_len| {
            let copied_len = response_len.min(writer.sum_lens());
            writer.skip_some(copied_len);
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
