// SPDX-License-Identifier: MPL-2.0

pub use smoltcp::wire::{
    EthernetAddress, IpAddress, IpCidr, IpEndpoint, IpListenEndpoint, Ipv4Address, Ipv4Cidr,
    Ipv6Address, Ipv6AddressExt, Ipv6Cidr,
};

pub type PortNum = u16;

/// Converts a bound endpoint into a smoltcp listen endpoint.
///
/// smoltcp's `From<IpEndpoint>` always yields a specified address, so a socket
/// bound to the unspecified address (`0.0.0.0` or `::`) would only accept
/// traffic addressed to that exact address. Linux instead treats such a bind as
/// a wildcard that accepts traffic for any local address, so map it to smoltcp's
/// wildcard (`addr: None`).
pub fn to_listen_endpoint(endpoint: IpEndpoint) -> IpListenEndpoint {
    let addr = (!endpoint.addr.is_unspecified()).then_some(endpoint.addr);
    IpListenEndpoint {
        addr,
        port: endpoint.port,
    }
}
