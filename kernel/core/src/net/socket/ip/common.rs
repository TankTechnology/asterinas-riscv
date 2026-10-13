// SPDX-License-Identifier: MPL-2.0

use aster_bigtcp::{
    errors::BindError,
    iface::{BindPortConfig, InterfaceType},
    wire::{IpAddress, IpEndpoint, Ipv4Address, Ipv6Address},
};

use crate::{
    net::{
        iface::{Iface, iter_all_ifaces},
        route,
        socket::util::check_port_privilege,
    },
    prelude::*,
};

pub(super) fn resolve_bind_iface_and_config(
    endpoint: &IpEndpoint,
    can_reuse: bool,
) -> Result<(Arc<Iface>, BindPortConfig)> {
    check_port_privilege(endpoint.port)?;

    let iface = if endpoint.addr.is_unspecified() {
        // A wildcard bind accepts traffic addressed to any local address.
        default_bind_iface()
    } else {
        route::lookup_local_iface(endpoint.addr)?
    };

    let bind_port_config = BindPortConfig::new(*endpoint, can_reuse);

    Ok((iface, bind_port_config))
}

/// Returns the interface that a wildcard bind is placed on.
///
/// Linux lets a socket bound to the unspecified address (`0.0.0.0` or `::`)
/// receive traffic for every local address, across every interface. An Asterinas
/// socket belongs to exactly one interface, so a wildcard bind must pick one.
/// The loopback interface is chosen because its addresses are unconditionally
/// local, which preserves the part of `INADDR_ANY` that a server reached from
/// its own machine depends on. A wildcard bind is therefore *not* reachable
/// through the configured network interface; a server that must be reachable
/// from the network has to bind that interface's address explicitly.
///
/// FIXME: Support the full Linux semantics by registering a wildcard-bound
/// socket on every interface. That needs a way for `aster-bigtcp` to enumerate
/// interfaces and to register one socket in several socket tables.
fn default_bind_iface() -> Arc<Iface> {
    iter_all_ifaces()
        .find(|iface| iface.type_() == InterfaceType::LOOPBACK)
        .or_else(|| iter_all_ifaces().next())
        .cloned()
        .expect("at least one network interface must be registered")
}

impl From<BindError> for Error {
    fn from(value: BindError) -> Self {
        match value {
            BindError::Exhausted => {
                Error::with_message(Errno::EAGAIN, "no ephemeral port is available")
            }
            BindError::InUse => {
                Error::with_message(Errno::EADDRINUSE, "the address is already in use")
            }
        }
    }
}

pub(super) fn get_ephemeral_endpoint(remote_endpoint: &IpEndpoint) -> Result<IpEndpoint> {
    let route_dst = remote_endpoint.addr;
    let iface = route::lookup_iface(route_dst)?;

    let source = match route_dst {
        IpAddress::Ipv4(_) => iface
            .ipv4_cidr()
            .map(|cidr| IpAddress::Ipv4(cidr.address()))
            .ok_or_else(|| {
                Error::with_message(
                    Errno::EADDRNOTAVAIL,
                    "the route output iface has no IPv4 address",
                )
            })?,
        IpAddress::Ipv6(_) => iface
            .ipv6_cidr()
            .map(|cidr| IpAddress::Ipv6(cidr.address()))
            .ok_or_else(|| {
                Error::with_message(
                    Errno::EADDRNOTAVAIL,
                    "the route output iface has no IPv6 address",
                )
            })?,
    };

    Ok(IpEndpoint::new(source, 0))
}

/// Maps an unspecified `connect` destination to the loopback address.
///
/// Linux treats `connect` to an unspecified address as connecting to localhost.
///
/// Reference: <https://elixir.bootlin.com/linux/v7.0/source/net/ipv4/route.c#L2803-L2812>.
/// Reference: <https://elixir.bootlin.com/linux/v7.0/source/net/ipv6/tcp_ipv6.c#L171-L181>.
pub(super) fn map_unspecified_to_localhost(mut endpoint: IpEndpoint) -> IpEndpoint {
    endpoint.addr = match endpoint.addr {
        IpAddress::Ipv4(address) if address.is_unspecified() => Ipv4Address::LOCALHOST.into(),
        IpAddress::Ipv6(address) if address.is_unspecified() => Ipv6Address::LOCALHOST.into(),
        address => address,
    };
    endpoint
}
