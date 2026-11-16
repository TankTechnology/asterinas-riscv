// SPDX-License-Identifier: MPL-2.0

//! `SOCK_DIAG` protocol messages: `inet_diag_req_v2` in, `inet_diag_msg` out.

#![short_vis_path::add(netlink)]

use crate::{
    net::socket::netlink::message::{
        CMsgSegHdr, ContinueRead, DoneSegment, ErrorSegment, ProtocolSegment,
    },
    prelude::*,
    util::{MultiRead, MultiWrite},
};

/// `SOCK_DIAG_BY_FAMILY`: query the sockets of one address family.
pub(crate) const SOCK_DIAG_BY_FAMILY: u16 = 20;

/// `struct inet_diag_req_v2` from the uapi (56 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub(crate) struct CDiagReqV2 {
    family: u8,
    protocol: u8,
    ext: u8,
    _pad: u8,
    /// Bit `i` set means "report sockets whose state is `i`"; zero means all.
    states: u32,
    sport: u16,
    dport: u16,
    src: [u32; 4],
    dst: [u32; 4],
    if_index: u32,
    cookie: [u32; 2],
}

impl CDiagReqV2 {
    pub(crate) fn family(&self) -> u8 {
        self.family
    }

    pub(crate) fn protocol(&self) -> u8 {
        self.protocol
    }

    pub(crate) fn states(&self) -> u32 {
        self.states
    }

    /// The source port the request filters on, in host order, if any.
    pub(crate) fn filter_source_port(&self) -> Option<u16> {
        Some(u16::from_be(self.sport)).filter(|port| *port != 0)
    }

    /// The destination port the request filters on, in host order, if any.
    pub(crate) fn filter_destination_port(&self) -> Option<u16> {
        Some(u16::from_be(self.dport)).filter(|port| *port != 0)
    }
}

/// `struct inet_diag_msg` from the uapi (72 bytes).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(crate) struct CDiagMsg {
    family: u8,
    state: u8,
    timer: u8,
    retrans: u8,
    expires: u32,
    rqueue: u32,
    wqueue: u32,
    uid: u32,
    inode: u32,
    sport: u16,
    dport: u16,
    src: [u32; 4],
    dst: [u32; 4],
    if_index: u32,
    cookie: [u32; 2],
}

impl CDiagMsg {
    pub(crate) const AF_INET: u8 = 2;
    pub(crate) const PROTOCOL_TCP: u8 = 6;
    pub(crate) const PROTOCOL_UDP: u8 = 17;

    /// Builds the reply for one TCP/UDP socket.
    pub(crate) fn new(
        family: u8,
        state: u8,
        local_port: u16,
        remote_port: u16,
        local_addr: [u8; 4],
        remote_addr: [u8; 4],
        uid: u32,
        inode: u32,
    ) -> Self {
        Self {
            family,
            state,
            timer: 0,
            retrans: 0,
            expires: 0,
            rqueue: 0,
            wqueue: 0,
            uid,
            inode,
            sport: local_port.to_be(),
            dport: remote_port.to_be(),
            // Addresses are stored in network order in the uapi; the low word
            // holds the address as written on the wire.
            src: [
                u32::from_be_bytes(local_addr),
                0,
                0,
                0,
            ],
            dst: [
                u32::from_be_bytes(remote_addr),
                0,
                0,
                0,
            ],
            if_index: 0,
            cookie: [0, 0],
        }
    }
}

/// One segment of the `SOCK_DIAG` protocol.
#[derive(Debug)]
pub(crate) enum DiagSegment {
    Request {
        header: CMsgSegHdr,
        request: CDiagReqV2,
    },
    Reply {
        header: CMsgSegHdr,
        reply: CDiagMsg,
    },
    Done(DoneSegment),
    Error(ErrorSegment),
}

impl DiagSegment {
    /// Returns the request, if this segment is one.
    pub(crate) fn request(&self) -> Option<&CDiagReqV2> {
        match self {
            DiagSegment::Request { request, .. } => Some(request),
            _ => None,
        }
    }

    /// Builds a reply segment for one socket, carrying the request's sequence
    /// and port so user space can match it.
    pub(crate) fn new_reply(request_header: &CMsgSegHdr, reply: CDiagMsg) -> Self {
        let header = CMsgSegHdr {
            len: (size_of::<CMsgSegHdr>() + size_of::<CDiagMsg>()) as u32,
            type_: SOCK_DIAG_BY_FAMILY,
            flags: 0,
            seq: request_header.seq,
            pid: request_header.pid,
        };
        DiagSegment::Reply { header, reply }
    }
}

impl ProtocolSegment for DiagSegment {
    fn header(&self) -> &CMsgSegHdr {
        match self {
            DiagSegment::Request { header, .. } | DiagSegment::Reply { header, .. } => header,
            DiagSegment::Done(done) => done.header(),
            DiagSegment::Error(error) => error.header(),
        }
    }

    fn header_mut(&mut self) -> &mut CMsgSegHdr {
        match self {
            DiagSegment::Request { header, .. } | DiagSegment::Reply { header, .. } => header,
            DiagSegment::Done(done) => done.header_mut(),
            DiagSegment::Error(error) => error.header_mut(),
        }
    }

    fn read_from(
        reader: &mut dyn MultiRead,
        _strict_check: bool,
    ) -> Result<ContinueRead<Self, ErrorSegment>> {
        let header = reader
            .read_val_opt::<CMsgSegHdr>()?
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "the reader length is too small"))?;

        // The padded payload length also moves the reader to the next segment
        // on every path below.
        let payload_len = header.calc_payload_len_with_padding(reader)?;

        if header.type_ != SOCK_DIAG_BY_FAMILY {
            reader.skip_some(payload_len);
            return Ok(ContinueRead::SkippedInvalid(ErrorSegment::new_from_request(
                &header,
                Some(Error::with_message(
                    Errno::EOPNOTSUPP,
                    "the diag request type is not supported",
                )),
            )));
        }
        if payload_len < size_of::<CDiagReqV2>() {
            reader.skip_some(payload_len);
            return Ok(ContinueRead::SkippedInvalid(ErrorSegment::new_from_request(
                &header,
                Some(Error::with_message(
                    Errno::EINVAL,
                    "the diag request body is too small",
                )),
            )));
        }

        let request = reader.read_val_opt::<CDiagReqV2>()?.unwrap();
        // Any extension attributes after the body are ignored.
        reader.skip_some(payload_len - size_of::<CDiagReqV2>());

        Ok(ContinueRead::Parsed(DiagSegment::Request { header, request }))
    }

    fn write_to(&self, writer: &mut dyn MultiWrite) -> Result<()> {
        match self {
            DiagSegment::Reply { header, reply } => {
                writer.write_val_trunc(header)?;
                writer.write_val_trunc(reply)?;
                // The uapi structs are 4-aligned, so no padding is needed, but
                // keep the writer's contract explicit.
                Ok(())
            }
            DiagSegment::Done(done) => done.write_to(writer),
            DiagSegment::Error(error) => error.write_to(writer),
            DiagSegment::Request { .. } => {
                return_errno_with_message!(Errno::EINVAL, "requests are not written")
            }
        }
    }
}
