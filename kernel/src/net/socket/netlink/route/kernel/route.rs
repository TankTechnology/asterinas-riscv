// SPDX-License-Identifier: MPL-2.0

//! Reports the IPv4 routes currently supplied by network interfaces.

use aster_bigtcp::iface::InterfaceType;

use super::util::finish_response;
use crate::{
    net::{
        net_ns::NetNamespace,
        socket::netlink::{
            message::{CMsgSegHdr, CSegmentType, GetRequestFlags, SegHdrCommonFlags},
            route::message::{RouteAttr, RouteSegment, RouteSegmentBody, RtScope, RtnlSegment},
        },
    },
    prelude::*,
    util::net::CSocketAddrFamily,
};

const MAIN_TABLE: u8 = 254;
const KERNEL_PROTOCOL: u8 = 2;
const BOOT_PROTOCOL: u8 = 3;
const UNICAST_ROUTE: u8 = 1;

pub(super) fn do_get_route(
    request: &RouteSegment,
    net_ns: &NetNamespace,
) -> Result<Vec<RtnlSegment>> {
    let flags = GetRequestFlags::from_bits_truncate(request.header().flags);
    if !flags.contains(GetRequestFlags::DUMP) {
        return_errno_with_message!(Errno::EOPNOTSUPP, "GETROUTE only supports dump requests");
    }

    let body = request.body();
    if body.family != CSocketAddrFamily::AF_UNSPEC as u8
        && body.family != CSocketAddrFamily::AF_INET as u8
        && body.family != CSocketAddrFamily::AF_INET6 as u8
    {
        return_errno_with_message!(Errno::EAFNOSUPPORT, "route family is not supported");
    }
    let requested_table = request
        .attrs()
        .iter()
        .find_map(|attr| match attr {
            RouteAttr::Table(table) => Some(*table),
            _ => None,
        })
        .unwrap_or(body.table as u32);
    if requested_table != 0 && requested_table != MAIN_TABLE as u32 {
        return_errno_with_message!(Errno::EOPNOTSUPP, "route table is not supported");
    }
    if body.dst_len != 0
        || body.src_len != 0
        || request
            .attrs()
            .iter()
            .any(|attr| !matches!(attr, RouteAttr::Table(_)))
        || request.header().len as usize
            > RouteSegment::HEADER_LEN + RouteSegment::BODY_LEN + request.attrs_len()
    {
        return_errno_with_message!(Errno::EOPNOTSUPP, "filtered route dumps are not supported");
    }

    let mut response_segments = Vec::new();
    if body.family == CSocketAddrFamily::AF_UNSPEC as u8
        || body.family == CSocketAddrFamily::AF_INET as u8
    {
        for iface in net_ns.ifaces() {
            if iface.type_() == InterfaceType::LOOPBACK {
                continue;
            }
            let Some(cidr) = iface.ipv4_cidr() else {
                continue;
            };

            if let Some(gateway) = iface.ipv4_gateway() {
                response_segments.push(new_route(
                    request.header(),
                    0,
                    BOOT_PROTOCOL,
                    RtScope::UNIVERSE,
                    vec![
                        RouteAttr::Gateway(gateway.octets()),
                        RouteAttr::OutputInterface(iface.index()),
                    ],
                ));
            }

            response_segments.push(new_route(
                request.header(),
                cidr.prefix_len(),
                KERNEL_PROTOCOL,
                RtScope::LINK,
                vec![
                    RouteAttr::Destination(cidr.network().address().octets()),
                    RouteAttr::OutputInterface(iface.index()),
                    RouteAttr::PreferredSource(cidr.address().octets()),
                ],
            ));
        }
    }

    finish_response(request.header(), true, &mut response_segments);
    Ok(response_segments)
}

fn new_route(
    request_header: &CMsgSegHdr,
    dst_len: u8,
    protocol: u8,
    scope: RtScope,
    attrs: Vec<RouteAttr>,
) -> RtnlSegment {
    let header = CMsgSegHdr {
        len: 0,
        type_: CSegmentType::NEWROUTE as _,
        flags: SegHdrCommonFlags::empty().bits(),
        seq: request_header.seq,
        pid: request_header.pid,
    };
    let body = RouteSegmentBody {
        family: CSocketAddrFamily::AF_INET as _,
        dst_len,
        src_len: 0,
        tos: 0,
        table: MAIN_TABLE,
        protocol,
        scope: scope as _,
        type_: UNICAST_ROUTE,
        flags: 0,
    };
    RtnlSegment::NewRoute(RouteSegment::new(header, body, attrs))
}
