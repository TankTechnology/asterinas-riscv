// SPDX-License-Identifier: MPL-2.0

use aster_bigtcp::{socket::UdpSocket, wire::IpEndpoint};

use super::{bound::BoundDatagram, observer::DatagramObserver};
use crate::{
    events::IoEvents,
    net::{
        iface::BoundUdpPort,
        net_ns::NetNamespace,
        socket::{
            ip::common::{get_ephemeral_endpoint, resolve_bind_iface_and_config},
            util::datagram_common,
        },
    },
    prelude::*,
    process::signal::Pollee,
};

pub(super) struct UnboundDatagram {
    _private: (),
}

impl UnboundDatagram {
    pub(super) fn new() -> Self {
        Self { _private: () }
    }
}

pub(super) struct BindOptions {
    pub(super) can_reuse: bool,
    pub(super) v6only: bool,
    pub(super) net_ns: Arc<NetNamespace>,
}

impl datagram_common::Unbound for UnboundDatagram {
    type Endpoint = IpEndpoint;
    type BindOptions = BindOptions;

    type Bound = BoundDatagram;

    fn bind(
        &mut self,
        endpoint: &Self::Endpoint,
        pollee: &Pollee,
        options: BindOptions,
    ) -> Result<Self::Bound> {
        let bound_port = bind_port(endpoint, options.can_reuse, options.v6only, &options.net_ns)?;

        let bound_socket = match UdpSocket::new_bind(
            bound_port,
            DatagramObserver::new(pollee.clone()),
            options.v6only,
        ) {
            Ok(bound_socket) => bound_socket,
            Err((_, err)) => {
                unreachable!("`new_bind` fails with {:?}, which should not happen", err)
            }
        };

        Ok(BoundDatagram::new(bound_socket))
    }

    fn bind_ephemeral(
        &mut self,
        remote_endpoint: &Self::Endpoint,
        pollee: &Pollee,
        options: BindOptions,
    ) -> Result<Self::Bound> {
        let endpoint =
            get_ephemeral_endpoint(remote_endpoint, &options.net_ns).ok_or_else(|| {
                Error::with_message(
                    Errno::EADDRNOTAVAIL,
                    "no interface has an address for the specified family",
                )
            })?;
        self.bind(&endpoint, pollee, options)
    }

    fn check_io_events(&self) -> IoEvents {
        IoEvents::OUT
    }
}

fn bind_port(
    endpoint: &IpEndpoint,
    can_reuse: bool,
    v6only: bool,
    net_ns: &NetNamespace,
) -> Result<BoundUdpPort> {
    let dual_stack = matches!(endpoint.addr, aster_bigtcp::wire::IpAddress::Ipv6(addr) if addr.is_unspecified())
        && !v6only;
    let (iface, config) = resolve_bind_iface_and_config(endpoint, can_reuse, dual_stack, net_ns)?;
    Ok(iface.bind_udp(config)?)
}
