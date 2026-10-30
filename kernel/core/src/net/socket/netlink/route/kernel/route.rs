// SPDX-License-Identifier: MPL-2.0

//! Handles IPv4 route dumps, lookups, and insertions for rtnetlink.

use aster_bigtcp::wire::Ipv4Cidr;

use super::util::finish_response;
use crate::{
    net::{
        Ipv4RouteSnapshot, add_ipv4_route, ipv4_route_snapshots, iter_all_ifaces,
        lookup_ipv4_route,
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

pub(super) fn do_get_route(request: &RouteSegment) -> Result<Vec<RtnlSegment>> {
    let flags = GetRequestFlags::from_bits_truncate(request.header().flags);
    if !flags.contains(GetRequestFlags::DUMP) {
        return lookup_route(request);
    }

    let body = request.body();
    if body.family != CSocketAddrFamily::AF_UNSPEC as u8
        && body.family != CSocketAddrFamily::AF_INET as u8
    {
        return_errno_with_message!(Errno::EAFNOSUPPORT, "route family is not supported");
    }
    let requested_table = requested_table(request);
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
    for route in ipv4_route_snapshots() {
        if requested_table != 0 && route.table != requested_table {
            continue;
        }
        response_segments.push(route_to_new_route(request.header(), route));
    }

    finish_response(request.header(), true, &mut response_segments);
    Ok(response_segments)
}

/// Handles an `RTM_NEWROUTE` request.
pub(super) fn do_new_route(request: &RouteSegment) -> Result<Vec<RtnlSegment>> {
    let body = request.body();
    if body.family != CSocketAddrFamily::AF_INET as u8 {
        return_errno_with_message!(Errno::EAFNOSUPPORT, "route family is not supported");
    }
    if body.dst_len > 32 || body.src_len != 0 || body.tos != 0 {
        return_errno_with_message!(Errno::EOPNOTSUPP, "route attributes are not supported");
    }

    let table = requested_table(request);
    if table != 0 && table != 253 && table != MAIN_TABLE as u32 && table != LOCAL_TABLE as u32 {
        return_errno_with_message!(Errno::EOPNOTSUPP, "route table is not supported");
    }
    let table = if table == 0 { MAIN_TABLE as u32 } else { table };

    let mut destination = None;
    let mut gateway = None;
    let mut output_index = None;
    for attr in request.attrs() {
        match attr {
            RouteAttr::Destination(address) if destination.is_none() => {
                destination = Some(*address);
            }
            RouteAttr::Gateway(address) if gateway.is_none() => {
                gateway = Some(*address);
            }
            RouteAttr::OutputInterface(index) if output_index.is_none() => {
                output_index = Some(*index);
            }
            RouteAttr::PreferredSource(_) => (),
            RouteAttr::Table(_) => (),
            _ => return_errno_with_message!(Errno::EINVAL, "duplicate route attribute"),
        }
    }

    if body.dst_len != 0 && destination.is_none() {
        return_errno_with_message!(Errno::EINVAL, "route destination is missing");
    }
    let destination = destination.unwrap_or([0, 0, 0, 0]);
    let dst = Ipv4Cidr::new(
        aster_bigtcp::wire::Ipv4Address::new(
            destination[0],
            destination[1],
            destination[2],
            destination[3],
        ),
        body.dst_len,
    );
    let dst = dst.network();
    let gateway = gateway.map(|address| {
        aster_bigtcp::wire::Ipv4Address::new(address[0], address[1], address[2], address[3])
    });

    let iface = if let Some(index) = output_index {
        iter_all_ifaces()
            .find(|iface| iface.index() == index)
            .cloned()
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "output interface is absent"))?
    } else if gateway.is_some() {
        default_iface()
            .ok_or_else(|| Error::with_message(Errno::ENETUNREACH, "no output interface"))?
    } else {
        iter_all_ifaces()
            .find(|iface| {
                iface
                    .ipv4_cidr()
                    .is_some_and(|cidr| cidr.contains_addr(&dst.address()))
            })
            .cloned()
            .or_else(default_iface)
            .ok_or_else(|| Error::with_message(Errno::ENETUNREACH, "no output interface"))?
    };

    let route_type = if body.type_ == 0 { 1 } else { body.type_ };
    add_ipv4_route(dst, gateway, iface, table, 0, route_type)?;

    Ok(Vec::new())
}

fn lookup_route(request: &RouteSegment) -> Result<Vec<RtnlSegment>> {
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
    let target = aster_bigtcp::wire::Ipv4Address::new(
        destination[0],
        destination[1],
        destination[2],
        destination[3],
    );
    let route = lookup_ipv4_route(target)
        .ok_or_else(|| Error::with_message(Errno::ENETUNREACH, "no route to destination"))?;

    Ok(vec![route_to_lookup(request.header(), target, route)])
}

fn route_to_lookup(
    request_header: &CMsgSegHdr,
    target: aster_bigtcp::wire::Ipv4Address,
    route: Ipv4RouteSnapshot,
) -> RtnlSegment {
    let scope = match route.type_ {
        2 => RtScope::HOST,
        3 => RtScope::LINK,
        _ => RtScope::UNIVERSE,
    };
    let mut attrs = vec![
        RouteAttr::Destination(target.octets()),
        RouteAttr::OutputInterface(route.iface.index()),
    ];
    if let Some(gateway) = route.gateway {
        attrs.push(RouteAttr::Gateway(gateway.octets()));
    }
    if let Some(cidr) = route.iface.ipv4_cidr() {
        attrs.push(RouteAttr::PreferredSource(cidr.address().octets()));
    }

    new_route(
        request_header,
        RouteSpec {
            table: MAIN_TABLE,
            dst_len: 32,
            protocol: 0,
            scope,
            route_type: route.type_,
            flags: 0x200,
        },
        attrs,
    )
}

fn requested_table(request: &RouteSegment) -> u32 {
    request
        .attrs()
        .iter()
        .find_map(|attr| match attr {
            RouteAttr::Table(table) => Some(*table),
            _ => None,
        })
        .unwrap_or(request.body().table as u32)
}

fn route_to_new_route(request_header: &CMsgSegHdr, route: Ipv4RouteSnapshot) -> RtnlSegment {
    let dst_len = route.dst.prefix_len();
    let scope = match route.type_ {
        2 => RtScope::HOST,
        3 => RtScope::LINK,
        _ if route.gateway.is_some() => RtScope::UNIVERSE,
        _ => RtScope::LINK,
    };
    let protocol = if dst_len == 0 {
        BOOT_PROTOCOL
    } else {
        KERNEL_PROTOCOL
    };

    let mut attrs = vec![
        RouteAttr::Destination(route.dst.network().address().octets()),
        RouteAttr::OutputInterface(route.iface.index()),
    ];
    if let Some(gateway) = route.gateway {
        attrs.push(RouteAttr::Gateway(gateway.octets()));
    }
    if dst_len != 0
        && let Some(cidr) = route.iface.ipv4_cidr()
    {
        attrs.push(RouteAttr::PreferredSource(cidr.address().octets()));
    }

    new_route(
        request_header,
        RouteSpec {
            table: route.table as u8,
            dst_len,
            protocol,
            scope,
            route_type: route.type_,
            flags: 0,
        },
        attrs,
    )
}

fn default_iface() -> Option<Arc<crate::net::Iface>> {
    iter_all_ifaces()
        .find(|iface| iface.type_() != aster_bigtcp::iface::InterfaceType::LOOPBACK)
        .cloned()
        .or_else(|| iter_all_ifaces().next().cloned())
}

struct RouteSpec {
    table: u8,
    dst_len: u8,
    protocol: u8,
    scope: RtScope,
    route_type: u8,
    flags: u32,
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
