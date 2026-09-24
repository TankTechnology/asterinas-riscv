// SPDX-License-Identifier: MPL-2.0

//! Reports the IPv4 routes currently supplied by network interfaces.

use aster_bigtcp::{
    iface::{InterfaceFlags, InterfaceType},
    wire::Ipv4Address,
};

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
const LOCAL_TABLE: u8 = 255;
const KERNEL_PROTOCOL: u8 = 2;
const BOOT_PROTOCOL: u8 = 3;
const UNICAST_ROUTE: u8 = 1;
const LOCAL_ROUTE: u8 = 2;
const BROADCAST_ROUTE: u8 = 3;
const CLONED_ROUTE: u32 = 0x200;

pub(super) fn do_get_route(
    request: &RouteSegment,
    net_ns: &NetNamespace,
) -> Result<Vec<RtnlSegment>> {
    let flags = GetRequestFlags::from_bits_truncate(request.header().flags);
    if !flags.contains(GetRequestFlags::DUMP) {
        return lookup_route(request, net_ns);
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
    if requested_table != 0
        && requested_table != MAIN_TABLE as u32
        && requested_table != LOCAL_TABLE as u32
    {
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
        if requested_table != LOCAL_TABLE as u32 {
            dump_main_routes(request.header(), net_ns, &mut response_segments);
        }
        if requested_table != MAIN_TABLE as u32 {
            dump_local_routes(request.header(), net_ns, &mut response_segments);
        }
    }

    finish_response(request.header(), true, &mut response_segments);
    Ok(response_segments)
}

fn dump_main_routes(
    request_header: &CMsgSegHdr,
    net_ns: &NetNamespace,
    response_segments: &mut Vec<RtnlSegment>,
) {
    for iface in net_ns.ifaces() {
        if iface.type_() == InterfaceType::LOOPBACK {
            continue;
        }
        let Some(cidr) = iface.ipv4_cidr() else {
            continue;
        };

        if let Some(gateway) = iface.ipv4_gateway() {
            response_segments.push(new_route(
                request_header,
                RouteSpec::new(
                    MAIN_TABLE,
                    0,
                    BOOT_PROTOCOL,
                    RtScope::UNIVERSE,
                    UNICAST_ROUTE,
                ),
                vec![
                    RouteAttr::Gateway(gateway.octets()),
                    RouteAttr::OutputInterface(iface.index()),
                ],
            ));
        }

        response_segments.push(new_route(
            request_header,
            RouteSpec::new(
                MAIN_TABLE,
                cidr.prefix_len(),
                KERNEL_PROTOCOL,
                RtScope::LINK,
                UNICAST_ROUTE,
            ),
            vec![
                RouteAttr::Destination(cidr.network().address().octets()),
                RouteAttr::OutputInterface(iface.index()),
                RouteAttr::PreferredSource(cidr.address().octets()),
            ],
        ));
    }
}

fn dump_local_routes(
    request_header: &CMsgSegHdr,
    net_ns: &NetNamespace,
    response_segments: &mut Vec<RtnlSegment>,
) {
    for iface in net_ns.ifaces() {
        let is_loopback = iface.type_() == InterfaceType::LOOPBACK;
        if is_loopback && !iface.flags().contains(InterfaceFlags::UP) {
            continue;
        }
        let Some(cidr) = iface.ipv4_cidr() else {
            continue;
        };
        let source = cidr.address().octets();
        let output = RouteAttr::OutputInterface(iface.index());

        if is_loopback {
            response_segments.push(new_route(
                request_header,
                RouteSpec::new(
                    LOCAL_TABLE,
                    cidr.prefix_len(),
                    KERNEL_PROTOCOL,
                    RtScope::HOST,
                    LOCAL_ROUTE,
                ),
                vec![
                    RouteAttr::Destination(cidr.network().address().octets()),
                    RouteAttr::OutputInterface(iface.index()),
                    RouteAttr::PreferredSource(source),
                ],
            ));
        }
        response_segments.push(new_route(
            request_header,
            RouteSpec::new(LOCAL_TABLE, 32, KERNEL_PROTOCOL, RtScope::HOST, LOCAL_ROUTE),
            vec![
                RouteAttr::Destination(source),
                output,
                RouteAttr::PreferredSource(source),
            ],
        ));

        if let Some(broadcast) = cidr.broadcast() {
            response_segments.push(new_route(
                request_header,
                RouteSpec::new(
                    LOCAL_TABLE,
                    32,
                    KERNEL_PROTOCOL,
                    RtScope::LINK,
                    BROADCAST_ROUTE,
                ),
                vec![
                    RouteAttr::Destination(broadcast.octets()),
                    RouteAttr::OutputInterface(iface.index()),
                    RouteAttr::PreferredSource(source),
                ],
            ));
        }
    }
}

fn lookup_route(request: &RouteSegment, net_ns: &NetNamespace) -> Result<Vec<RtnlSegment>> {
    let body = request.body();
    if body.family != CSocketAddrFamily::AF_INET as u8 {
        return_errno_with_message!(Errno::EAFNOSUPPORT, "route family is not supported");
    }
    if body.dst_len != 32 || body.src_len != 0 || body.tos != 0 {
        return_errno_with_message!(Errno::EOPNOTSUPP, "route lookup filter is not supported");
    }

    let mut destination = None;
    for attr in request.attrs() {
        match attr {
            RouteAttr::Destination(address) if destination.is_none() => {
                destination = Some(*address);
            }
            RouteAttr::Table(table) if *table == 0 || *table == MAIN_TABLE as u32 => (),
            _ => {
                return_errno_with_message!(
                    Errno::EOPNOTSUPP,
                    "route lookup filter is not supported"
                );
            }
        }
    }
    if body.table != 0 && body.table != MAIN_TABLE {
        return_errno_with_message!(Errno::EOPNOTSUPP, "route table is not supported");
    }
    if request.header().len as usize
        > RouteSegment::HEADER_LEN + RouteSegment::BODY_LEN + request.attrs_len()
    {
        return_errno_with_message!(Errno::EOPNOTSUPP, "route lookup filter is not supported");
    }
    let Some(destination) = destination else {
        return_errno_with_message!(Errno::EINVAL, "route lookup destination is missing");
    };
    let target = Ipv4Address::new(
        destination[0],
        destination[1],
        destination[2],
        destination[3],
    );

    let loopback = net_ns.loopback();
    let loopback_source = loopback
        .ipv4_cidr()
        .filter(|cidr| cidr.contains_addr(&target))
        .map(|cidr| cidr.address());
    let is_local = loopback_source.is_some()
        || net_ns.ifaces().iter().any(|iface| {
            iface
                .ipv4_cidr()
                .is_some_and(|cidr| cidr.address() == target)
        });
    if is_local {
        if !loopback.flags().contains(InterfaceFlags::UP) {
            return_errno_with_message!(Errno::ENETUNREACH, "loopback interface is down");
        }
        let source = loopback_source.unwrap_or(target);
        return Ok(vec![new_route(
            request.header(),
            RouteSpec::new(MAIN_TABLE, 32, 0, RtScope::UNIVERSE, LOCAL_ROUTE).cloned(),
            vec![
                RouteAttr::Destination(destination),
                RouteAttr::OutputInterface(loopback.index()),
                RouteAttr::PreferredSource(source.octets()),
            ],
        )]);
    }

    let iface = net_ns.default_iface();
    let Some(cidr) = iface.ipv4_cidr() else {
        return_errno_with_message!(Errno::ENETUNREACH, "no IPv4 route to destination");
    };
    let is_broadcast = target.is_broadcast() || iface.broadcast_addr() == Some(target);
    let gateway = if is_broadcast || cidr.contains_addr(&target) {
        None
    } else {
        Some(iface.ipv4_gateway().ok_or_else(|| {
            Error::with_message(Errno::ENETUNREACH, "no IPv4 route to destination")
        })?)
    };
    let mut attrs = vec![
        RouteAttr::Destination(destination),
        RouteAttr::OutputInterface(iface.index()),
        RouteAttr::PreferredSource(cidr.address().octets()),
    ];
    if let Some(gateway) = gateway {
        attrs.push(RouteAttr::Gateway(gateway.octets()));
    }
    Ok(vec![new_route(
        request.header(),
        RouteSpec::new(
            MAIN_TABLE,
            32,
            0,
            RtScope::UNIVERSE,
            if is_broadcast {
                BROADCAST_ROUTE
            } else {
                UNICAST_ROUTE
            },
        )
        .cloned(),
        attrs,
    )])
}

struct RouteSpec {
    table: u8,
    dst_len: u8,
    protocol: u8,
    scope: RtScope,
    route_type: u8,
    flags: u32,
}

impl RouteSpec {
    fn new(table: u8, dst_len: u8, protocol: u8, scope: RtScope, route_type: u8) -> Self {
        Self {
            table,
            dst_len,
            protocol,
            scope,
            route_type,
            flags: 0,
        }
    }

    fn cloned(mut self) -> Self {
        self.flags = CLONED_ROUTE;
        self
    }
}

fn new_route(request_header: &CMsgSegHdr, spec: RouteSpec, attrs: Vec<RouteAttr>) -> RtnlSegment {
    let header = CMsgSegHdr {
        len: 0,
        type_: CSegmentType::NEWROUTE as _,
        flags: SegHdrCommonFlags::empty().bits(),
        seq: request_header.seq,
        pid: request_header.pid,
    };
    let body = RouteSegmentBody {
        family: CSocketAddrFamily::AF_INET as _,
        dst_len: spec.dst_len,
        src_len: 0,
        tos: 0,
        table: spec.table,
        protocol: spec.protocol,
        scope: spec.scope as _,
        type_: spec.route_type,
        flags: spec.flags,
    };
    RtnlSegment::NewRoute(RouteSegment::new(header, body, attrs))
}
