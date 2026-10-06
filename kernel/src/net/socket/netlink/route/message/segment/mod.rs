// SPDX-License-Identifier: MPL-2.0

//! This module defines the message segment,
//! which is the basic unit of a netlink message.
//!
//! Typically, a segment will consist of three parts:
//!
//! 1. Header: The headers of all segments are of type [`CMsgSegHdr`],
//!    which indicate the type and total length of the segment.
//!
//! 2. Body: The body is the main component of a segment.
//!    Each segment will have one and only one body.
//!    The body type is defined by the `type_` field of the header.
//!
//! 3. Attributes: Attributes are optional.
//!    A segment can have zero or multiple attributes.
//!    Attributes belong to different classes,
//!    with the class defined by the `type_` field of the header.
//!    The total number of attributes is controlled by the `len` field of the header.
//!
//! Note that all headers, bodies, and attributes require
//! their starting address in memory to be aligned to [`NLMSG_ALIGN`]
//! when copying to and from user space.
//! Therefore, necessary padding must be added to ensure alignment.
//!
//! The layout of a segment in memory is shown below:
//!
//! ┌────────┬─────────┬──────┬─────────┬──────┬──────┬──────┐
//! │ Header │ Padding │ Body │ Padding │ Attr │ Attr │ Attr │
//! └────────┴─────────┴──────┴─────────┴──────┴──────┴──────┘
//!
//! [`NLMSG_ALIGN`]: crate::net::socket::netlink::message::NLMSG_ALIGN

pub mod addr;
mod legacy;
pub mod link;
pub mod route;

use addr::AddrSegment;
use link::LinkSegment;
use route::RouteSegment;

use crate::{
    net::socket::netlink::message::{
        CMsgSegHdr, CSegmentType, ContinueRead, DoneSegment, ErrorSegment, ProtocolSegment,
    },
    prelude::*,
    util::{MultiRead, MultiWrite},
};

/// The netlink route segment, which is the basic unit of a netlink route message.
#[derive(Debug)]
pub enum RtnlSegment {
    NewLink(LinkSegment),
    SetLink(LinkSegment),
    NewAddr(AddrSegment),
    GetAddr(AddrSegment),
    GetLink(LinkSegment),
    GetRoute(RouteSegment),
    NewRoute(RouteSegment),
    GetQdisc(QdiscSegment),
    GetTclass(QdiscSegment),
    GetTfilter(QdiscSegment),
    GetNeigh(QdiscSegment),
    GetRule(QdiscSegment),
    GetNexthop(QdiscSegment),
    Done(DoneSegment),
    Error(ErrorSegment),
}

impl ProtocolSegment for RtnlSegment {
    fn header(&self) -> &CMsgSegHdr {
        match self {
            RtnlSegment::NewLink(link_segment)
            | RtnlSegment::SetLink(link_segment)
            | RtnlSegment::GetLink(link_segment) => link_segment.header(),
            RtnlSegment::NewAddr(addr_segment) | RtnlSegment::GetAddr(addr_segment) => {
                addr_segment.header()
            }
            RtnlSegment::GetRoute(route_segment) | RtnlSegment::NewRoute(route_segment) => {
                route_segment.header()
            }
            RtnlSegment::GetQdisc(qdisc_segment) => qdisc_segment.header(),
            RtnlSegment::GetTclass(qdisc_segment) => qdisc_segment.header(),
            RtnlSegment::GetTfilter(qdisc_segment) => qdisc_segment.header(),
            RtnlSegment::GetNeigh(qdisc_segment) => qdisc_segment.header(),
            RtnlSegment::GetRule(qdisc_segment) => qdisc_segment.header(),
            RtnlSegment::GetNexthop(qdisc_segment) => qdisc_segment.header(),
            RtnlSegment::Done(done_segment) => done_segment.header(),
            RtnlSegment::Error(error_segment) => error_segment.header(),
        }
    }

    fn header_mut(&mut self) -> &mut CMsgSegHdr {
        match self {
            RtnlSegment::NewLink(link_segment)
            | RtnlSegment::SetLink(link_segment)
            | RtnlSegment::GetLink(link_segment) => link_segment.header_mut(),
            RtnlSegment::NewAddr(addr_segment) | RtnlSegment::GetAddr(addr_segment) => {
                addr_segment.header_mut()
            }
            RtnlSegment::GetRoute(route_segment) | RtnlSegment::NewRoute(route_segment) => {
                route_segment.header_mut()
            }
            RtnlSegment::GetQdisc(qdisc_segment) => qdisc_segment.header_mut(),
            RtnlSegment::GetTclass(qdisc_segment) => qdisc_segment.header_mut(),
            RtnlSegment::GetTfilter(qdisc_segment) => qdisc_segment.header_mut(),
            RtnlSegment::GetNeigh(qdisc_segment) => qdisc_segment.header_mut(),
            RtnlSegment::GetRule(qdisc_segment) => qdisc_segment.header_mut(),
            RtnlSegment::GetNexthop(qdisc_segment) => qdisc_segment.header_mut(),
            RtnlSegment::Done(done_segment) => done_segment.header_mut(),
            RtnlSegment::Error(error_segment) => error_segment.header_mut(),
        }
    }

    fn read_from(reader: &mut dyn MultiRead) -> Result<ContinueRead<Self, ErrorSegment>> {
        let header = reader
            .read_val_opt::<CMsgSegHdr>()?
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "the reader length is too small"))?;

        let segment = match CSegmentType::try_from(header.type_) {
            Ok(CSegmentType::GETLINK) => {
                LinkSegment::read_from(&header, reader)?.map(RtnlSegment::GetLink)
            }
            Ok(CSegmentType::NEWLINK) => {
                LinkSegment::read_from(&header, reader)?.map(RtnlSegment::NewLink)
            }
            Ok(CSegmentType::GETADDR) => {
                AddrSegment::read_from(&header, reader)?.map(RtnlSegment::GetAddr)
            }
            Ok(CSegmentType::SETLINK) => {
                LinkSegment::read_from(&header, reader)?.map(RtnlSegment::SetLink)
            }
            Ok(CSegmentType::NEWADDR) => {
                AddrSegment::read_from(&header, reader)?.map(RtnlSegment::NewAddr)
            }
            Ok(CSegmentType::GETROUTE) => {
                RouteSegment::read_from(&header, reader)?.map(RtnlSegment::GetRoute)
            }
            Ok(CSegmentType::GETQDISC) => {
                QdiscSegment::read_from(&header, reader)?.map(RtnlSegment::GetQdisc)
            }
            Ok(CSegmentType::GETTCLASS) => {
                QdiscSegment::read_from(&header, reader)?.map(RtnlSegment::GetTclass)
            }
            Ok(CSegmentType::GETTFILTER) => {
                QdiscSegment::read_from(&header, reader)?.map(RtnlSegment::GetTfilter)
            }
            Ok(CSegmentType::GETNEIGH) => {
                QdiscSegment::read_from(&header, reader)?.map(RtnlSegment::GetNeigh)
            }
            Ok(CSegmentType::GETRULE) => {
                QdiscSegment::read_from(&header, reader)?.map(RtnlSegment::GetRule)
            }
            Ok(CSegmentType::GETNEXTHOP) => {
                QdiscSegment::read_from(&header, reader)?.map(RtnlSegment::GetNexthop)
            }
            _ => {
                let payload_len = header.calc_payload_len_with_padding(reader)?;
                reader.skip_some(payload_len);
                ContinueRead::skipped_with_error(
                    Errno::EOPNOTSUPP,
                    "the segment type is not supported",
                )
            }
        };

        Ok(segment.map_err(|error| ErrorSegment::new_from_request(&header, Some(error))))
    }

    fn write_to(&self, writer: &mut dyn MultiWrite) -> Result<()> {
        match self {
            RtnlSegment::NewLink(link_segment) => link_segment.write_to(writer)?,
            RtnlSegment::NewAddr(addr_segment) => addr_segment.write_to(writer)?,
            RtnlSegment::NewRoute(route_segment) => route_segment.write_to(writer)?,
            RtnlSegment::Done(done_segment) => done_segment.write_to(writer)?,
            RtnlSegment::Error(error_segment) => error_segment.write_to(writer)?,
            RtnlSegment::SetLink(_)
            | RtnlSegment::GetAddr(_)
            | RtnlSegment::GetLink(_)
            | RtnlSegment::GetRoute(_)
            | RtnlSegment::GetQdisc(_)
            | RtnlSegment::GetTclass(_)
            | RtnlSegment::GetTfilter(_)
            | RtnlSegment::GetNeigh(_)
            | RtnlSegment::GetRule(_)
            | RtnlSegment::GetNexthop(_) => {
                unreachable!("kernel should not write set/get requests to user space");
            }
        }
        Ok(())
    }
}

/// A `RTM_GETQDISC` request. Asterinas currently exposes no traffic-control
/// qdisc objects, so retaining the header is sufficient to return `NLMSG_DONE`
/// for an empty dump instead of leaving rtnetlink clients blocked.
#[derive(Debug)]
pub struct QdiscSegment {
    header: CMsgSegHdr,
}

impl QdiscSegment {
    fn read_from(header: &CMsgSegHdr, reader: &mut dyn MultiRead) -> Result<ContinueRead<Self>> {
        let payload_len = header.calc_payload_len_with_padding(reader)?;
        reader.skip_some(payload_len);
        Ok(ContinueRead::Parsed(Self { header: *header }))
    }

    pub fn header(&self) -> &CMsgSegHdr {
        &self.header
    }

    pub fn header_mut(&mut self) -> &mut CMsgSegHdr {
        &mut self.header
    }
}
