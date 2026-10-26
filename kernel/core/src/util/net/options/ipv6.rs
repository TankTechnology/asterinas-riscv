// SPDX-License-Identifier: MPL-2.0

//! IPv6-level socket options (`SOL_IPV6` / `IPPROTO_IPV6`).
//!
//! Implemented: `IPV6_V6ONLY`, `IPV6_RECVERR`, `IPV6_DONTFRAG` and
//! `IPV6_MTU`. They are stored and can be read back, but none of them drives
//! behavior yet:
//!
//! - `IPV6_V6ONLY`: the stack keeps one address family per socket, so a v6
//!   socket is already v6-only;
//! - `IPV6_RECVERR`: no ICMP error queue is delivered as control messages;
//! - `IPV6_DONTFRAG`: the IPv6 egress path does not set the DF bit;
//! - `IPV6_MTU`: there is no path-MTU enforcement.
//!
//! These are compatibility reads/writes so that tools which set them can run.
//! Every other `SOL_IPV6` option returns `ENOPROTOOPT`.

use int_to_c_enum::TryFromInt;

use super::{RawSocketOption, impl_raw_socket_option};
use crate::{
    net::socket::{
        ip::options::{Ipv6Dontfrag, Ipv6Mtu, Ipv6Recverr, Ipv6V6Only},
        options::SocketOption,
    },
    prelude::*,
};

/// IPv6 socket options.
///
/// The raw definitions can be found at:
/// <https://elixir.bootlin.com/linux/v6.0.19/source/include/uapi/linux/in6.h>.
#[expect(non_camel_case_types)]
#[repr(i32)]
#[derive(Clone, Copy, Debug, TryFromInt)]
pub(crate) enum CIpv6OptionName {
    MTU = 24,
    RECVERR = 25,
    V6ONLY = 26,
    DONTFRAG = 62,
}

pub(crate) fn new_ipv6_option(name: i32) -> Result<Box<dyn RawSocketOption>> {
    let name = CIpv6OptionName::try_from(name)
        .map_err(|_| Error::with_message(Errno::ENOPROTOOPT, "unknown IPv6 socket option"))?;

    match name {
        CIpv6OptionName::V6ONLY => Ok(Box::new(Ipv6V6Only::new())),
        CIpv6OptionName::RECVERR => Ok(Box::new(Ipv6Recverr::new())),
        CIpv6OptionName::DONTFRAG => Ok(Box::new(Ipv6Dontfrag::new())),
        CIpv6OptionName::MTU => Ok(Box::new(Ipv6Mtu::new())),
    }
}

impl_raw_socket_option!(Ipv6V6Only);
impl_raw_socket_option!(Ipv6Recverr);
impl_raw_socket_option!(Ipv6Dontfrag);
impl_raw_socket_option!(Ipv6Mtu);
