// SPDX-License-Identifier: MPL-2.0

use super::message::UeventMessage;
use crate::{
    events::IoEvents,
    net::socket::{
        netlink::{NetlinkSocketAddr, common::BoundNetlink},
        util::{RecvFlags, RecvOutput, SendFlags, datagram_common},
    },
    prelude::*,
    util::{MultiRead, MultiWrite},
};

pub(super) type BoundNetlinkUevent = BoundNetlink<UeventMessage>;

impl datagram_common::Bound for BoundNetlinkUevent {
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
        // TODO: Deal with flags
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        if *remote != NetlinkSocketAddr::new_unspecified() {
            return_errno_with_message!(
                Errno::ECONNREFUSED,
                "sending uevent messages to user space is not supported"
            );
        }

        // FIXME: How to deal with sending message to kernel socket?
        // Here we simply ignore the message and return the message length.
        Ok(reader.sum_lens())
    }

    fn try_recv(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, Self::Endpoint)> {
        // TODO: Deal with other flags.
        if !flags.is_all_supported() {
            warn!("unsupported flags: {:?}", flags);
        }

        let mut receive_queue = self.receive_queue.lock();

        // Drop messages rejected by the socket filter (SO_ATTACH_FILTER) and
        // continue with the next one. Linux applies the filter at enqueue
        // time; applying it at receive time is observably equivalent here.
        loop {
            let result = receive_queue.dequeue_if(|response, response_len| {
                if !self.filter_allows(response.as_bytes()) {
                    return Ok((true, None));
                }

                let copied_len = response_len.min(writer.sum_lens());
                response.write_to(writer)?;

                let remote = *response.src_addr();

                let should_dequeue = flags.receive_behavior().will_consume_data();
                let output = RecvOutput::new_for_packet(flags, copied_len, response_len);
                Ok((should_dequeue, Some((output, remote))))
            })?;

            if let Some(output) = result {
                return Ok(output);
            }
        }
    }

    fn check_io_events(&self) -> IoEvents {
        self.check_io_events_common()
    }
}
