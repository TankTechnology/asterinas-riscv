// SPDX-License-Identifier: MPL-2.0

//! Bound `NETLINK_SOCK_DIAG` sockets.

use super::{kernel, message::SockDiagMessage};
use crate::{
    events::IoEvents,
    net::socket::{
        netlink::{
            NetlinkSocketAddr, common::BoundNetlink, diag::message::InetDiagReqV2,
            message::CMsgSegHdr,
        },
        util::{RecvFlags, RecvOutput, SendFlags, datagram_common},
    },
    prelude::*,
    util::{MultiRead, MultiWrite},
};

pub(super) type BoundNetlinkDiag = BoundNetlink<SockDiagMessage>;

impl datagram_common::Bound for BoundNetlinkDiag {
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
                "sending sock_diag messages to user space is not supported"
            );
        }

        let sum_lens = reader.sum_lens();
        let local_port = self.handle.port();

        loop {
            let Some(mut header) = reader.read_val_opt::<CMsgSegHdr>()? else {
                break;
            };
            let Some(payload_len) = (header.len as usize).checked_sub(size_of::<CMsgSegHdr>())
            else {
                break;
            };
            let remaining = reader.sum_lens();
            if payload_len > remaining {
                break;
            }
            let padded_len = (payload_len + 3) & !3;

            // The sender may leave the pid unspecified; responses must carry a
            // concrete one, as the route socket does.
            if header.pid == 0 {
                header.pid = local_port;
            }

            if payload_len < size_of::<InetDiagReqV2>() {
                reader.skip_some(padded_len.min(remaining));
                // Answer malformed requests with an empty dump instead of
                // silence, so the tool reports no sockets rather than hanging.
                kernel::handle_request(&header, &zero_request(), local_port);
                continue;
            }

            let request = reader
                .read_val_opt::<InetDiagReqV2>()?
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "truncated sock_diag request"))?;
            let consumed = size_of::<InetDiagReqV2>();
            reader.skip_some(padded_len.saturating_sub(consumed).min(reader.sum_lens()));

            kernel::handle_request(&header, &request, local_port);
        }

        Ok(sum_lens)
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

        receive_queue.dequeue_if(|response, response_len| {
            let copied_len = response_len.min(writer.sum_lens());
            response.write_to(writer)?;

            let remote = NetlinkSocketAddr::new_unspecified();

            let should_dequeue = flags.receive_behavior().will_consume_data();
            let output = RecvOutput::new_for_packet(flags, copied_len, response_len);
            Ok((should_dequeue, (output, remote)))
        })
    }

    fn check_io_events(&self) -> IoEvents {
        self.check_io_events_common()
    }
}

fn zero_request() -> InetDiagReqV2 {
    InetDiagReqV2 {
        family: 0,
        protocol: 0,
        ext: 0,
        pad: 0,
        states: 0,
        id: super::message::InetDiagSockId {
            sport: 0,
            dport: 0,
            src: [0; 4],
            dst: [0; 4],
            if_: 0,
            cookie: [0; 2],
        },
    }
}
