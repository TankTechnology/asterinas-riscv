// SPDX-License-Identifier: MPL-2.0

use aster_bigtcp::{
    errors::udp::{RecvError, SendError},
    wire::IpEndpoint,
};

use crate::{
    events::IoEvents,
    net::{
        iface::{BoundUdpPort, Iface, UdpSocket},
        socket::util::{RecvFlags, RecvOutput, SendFlags, datagram_common},
    },
    prelude::*,
    util::{MultiRead, MultiWrite},
};

pub(super) struct BoundDatagram {
    bound_socket: UdpSocket,
    remote_endpoint: Option<IpEndpoint>,
}

impl BoundDatagram {
    pub(super) fn new(bound_socket: UdpSocket) -> Self {
        Self {
            bound_socket,
            remote_endpoint: None,
        }
    }

    pub(super) fn iface(&self) -> &Arc<Iface> {
        self.bound_socket.iface()
    }

    pub(super) fn bound_port(&self) -> &BoundUdpPort {
        self.bound_socket.bound_port()
    }
}

impl datagram_common::Bound for BoundDatagram {
    type Endpoint = IpEndpoint;

    fn local_endpoint(&self) -> Self::Endpoint {
        self.bound_socket.local_endpoint()
    }

    fn remote_endpoint(&self) -> Option<&Self::Endpoint> {
        self.remote_endpoint.as_ref()
    }

    fn set_remote_endpoint(&mut self, endpoint: &Self::Endpoint) {
        self.remote_endpoint = Some(*endpoint)
    }

    fn try_recv(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, Self::Endpoint)> {
        let result = self
            .bound_socket
            .recv(flags.receive_behavior(), |packet, udp_metadata| {
                let message_len = packet.len();
                let copied_res = writer
                    .write(&mut VmReader::from(packet))
                    .map_err(Into::into);
                let endpoint = udp_metadata.endpoint;
                (copied_res, endpoint, message_len)
            });

        match result {
            Ok((Ok(copied_len), endpoint, message_len)) => {
                let output = RecvOutput::new_for_packet(flags, copied_len, message_len);
                Ok((output, endpoint))
            }
            Ok((Err(e), _, _)) => Err(e),
            Err(RecvError::Exhausted) => {
                return_errno_with_message!(Errno::EAGAIN, "the receive buffer is empty")
            }
            Err(RecvError::Truncated) => {
                unreachable!("`recv` should never fail with `RecvError::Truncated`")
            }
        }
    }

    fn try_send(
        &self,
        reader: &mut dyn MultiRead,
        remote: &Self::Endpoint,
        _flags: SendFlags,
    ) -> Result<usize> {
        let payload_len = reader.sum_lens();
        // The UDP length field is 16 bits, so reject absurd requests before
        // allocating a staging buffer.
        if payload_len > u16::MAX as usize {
            return_errno_with_message!(Errno::EMSGSIZE, "the message is too large");
        }

        // Copy the payload before taking the socket lock. The lock is a
        // spinlock, so a user page fault (which may require blocking I/O)
        // cannot be served inside the copy closure. Staging also avoids
        // sending a partially copied datagram (the old FIXME).
        let mut payload = vec![0u8; payload_len];
        let mut writer = VmWriter::from(payload.as_mut_slice());
        let copied = reader
            .read(&mut writer)
            .map_err(|(err, _)| Error::from(err))?;
        if copied != payload_len {
            return_errno_with_message!(Errno::EFAULT, "failed to copy the datagram payload");
        }

        let result = self
            .bound_socket
            .send(payload_len, *remote, |socket_buffer| {
                socket_buffer.copy_from_slice(&payload);
            });

        match result {
            Ok(()) => Ok(payload_len),
            Err(SendError::TooLarge) => {
                return_errno_with_message!(Errno::EMSGSIZE, "the message is too large");
            }
            Err(SendError::Unaddressable) => {
                return_errno_with_message!(Errno::EINVAL, "the destination address is invalid");
            }
            Err(SendError::BufferFull) => {
                return_errno_with_message!(Errno::EAGAIN, "the send buffer is full");
            }
        }
    }

    fn check_io_events(&self) -> IoEvents {
        self.bound_socket.raw_with(|socket| {
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
}
