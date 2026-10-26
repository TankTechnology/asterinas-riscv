// SPDX-License-Identifier: MPL-2.0

//! IPv6-level socket options (`SOL_IPV6` / `IPPROTO_IPV6`).
//!
//! Only `IPV6_V6ONLY` is implemented. The stack keeps one address family per
//! socket, so the flag is stored and can be read back, but it does not change
//! which packets the socket accepts (a socket is already v6-only).

use int_to_c_enum::TryFromInt;

use super::{RawSocketOption, impl_raw_socket_option};
use crate::{
    net::socket::{ip::options::Ipv6V6Only, options::SocketOption},
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
    V6ONLY = 26,
}

pub(crate) fn new_ipv6_option(name: i32) -> Result<Box<dyn RawSocketOption>> {
    let name = CIpv6OptionName::try_from(name)
        .map_err(|_| Error::with_message(Errno::ENOPROTOOPT, "unknown IPv6 socket option"))?;

    match name {
        CIpv6OptionName::V6ONLY => Ok(Box::new(Ipv6V6Only::new())),
    }
}

impl_raw_socket_option!(Ipv6V6Only);
