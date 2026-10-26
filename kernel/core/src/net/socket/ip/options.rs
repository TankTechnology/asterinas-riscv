// SPDX-License-Identifier: MPL-2.0

use core::num::NonZeroU8;

use aster_bigtcp::socket::NeedIfacePoll;

use crate::{
    net::socket::{
        ip::IpAddressFamily,
        options::{
            SocketOption,
            macros::{impl_socket_options, sock_option_mut, sock_option_ref},
        },
    },
    prelude::*,
};

/// IP-level socket options.
#[derive(Clone, Copy, CopyGetters, Debug, Setters)]
#[get_copy = "pub"]
#[set = "pub"]
pub(super) struct IpOptionSet {
    tos: u8,
    ttl: IpTtl,
    hdrincl: bool,
    recverr: bool,
    /// `IPV6_V6ONLY`. Linux defaults to 0 (dual-stack); this stack supports a
    /// single address family per socket, so the flag is stored for
    /// compatibility and does not change which packets a socket accepts.
    ipv6_v6only: bool,
    /// `IPV6_RECVERR`. Stored only: this stack does not queue ICMP errors for
    /// delivery as control messages.
    ipv6_recverr: bool,
    /// `IPV6_DONTFRAG`. Stored only: the IPv6 egress path does not set DF.
    ipv6_dontfrag: bool,
    /// `IPV6_MTU`. Stored only: there is no path-MTU enforcement.
    ipv6_mtu: i32,
}

const DEFAULT_TTL: u8 = 64;
pub(super) const INET_ECN_MASK: u8 = 3;

impl IpOptionSet {
    pub(super) const fn new_tcp() -> Self {
        Self {
            tos: 0,
            ttl: IpTtl(None),
            hdrincl: false,
            recverr: false,
            ipv6_v6only: false,
            ipv6_recverr: false,
            ipv6_dontfrag: false,
            ipv6_mtu: 0,
        }
    }

    pub(super) const fn new_udp() -> Self {
        Self {
            tos: 0,
            ttl: IpTtl(None),
            hdrincl: false,
            recverr: false,
            ipv6_v6only: false,
            ipv6_recverr: false,
            ipv6_dontfrag: false,
            ipv6_mtu: 0,
        }
    }

    pub(super) fn get_option(
        &self,
        option: &mut dyn SocketOption,
        family: IpAddressFamily,
    ) -> Result<()> {
        sock_option_mut!(match option {
            ip_tos @ Tos => {
                let tos = self.tos();
                ip_tos.set(tos as _);
            }
            ip_ttl @ Ttl => {
                let ttl = self.ttl();
                ip_ttl.set(ttl);
            }
            ip_hdrincl @ Hdrincl => {
                let hdrincl = self.hdrincl();
                ip_hdrincl.set(hdrincl);
            }
            ip_recverr @ Recverr => {
                let recverr = self.recverr();
                ip_recverr.set(recverr);
            }
            ipv6_v6only @ Ipv6V6Only => {
                Self::check_ipv6(family)?;
                ipv6_v6only.set(self.ipv6_v6only());
            }
            ipv6_recverr @ Ipv6Recverr => {
                Self::check_ipv6(family)?;
                ipv6_recverr.set(self.ipv6_recverr());
            }
            ipv6_dontfrag @ Ipv6Dontfrag => {
                Self::check_ipv6(family)?;
                ipv6_dontfrag.set(self.ipv6_dontfrag());
            }
            ipv6_mtu @ Ipv6Mtu => {
                Self::check_ipv6(family)?;
                ipv6_mtu.set(self.ipv6_mtu());
            }
            _ => return_errno_with_message!(Errno::ENOPROTOOPT, "the socket option is unknown"),
        });

        Ok(())
    }

    pub(super) fn set_option(
        &mut self,
        option: &dyn SocketOption,
        socket: &dyn SetIpLevelOption,
        family: IpAddressFamily,
    ) -> Result<NeedIfacePoll> {
        sock_option_ref!(match option {
            ip_tos @ Tos => {
                let old_value = self.tos();
                let mut val = *ip_tos.get().unwrap() as u8;
                val &= !INET_ECN_MASK;
                val |= old_value & INET_ECN_MASK;
                self.set_tos(val);
            }
            ip_ttl @ Ttl => {
                let ttl = ip_ttl.get().unwrap();
                self.set_ttl(*ttl);
            }
            ip_hdrincl @ Hdrincl => {
                let hdrincl = ip_hdrincl.get().unwrap();
                socket.set_hdrincl(*hdrincl)?;
                self.set_hdrincl(*hdrincl);
            }
            ip_recverr @ Recverr => {
                let recverr = ip_recverr.get().unwrap();
                self.set_recverr(*recverr);
            }
            ipv6_v6only @ Ipv6V6Only => {
                Self::check_ipv6(family)?;
                let value = ipv6_v6only.get().unwrap();
                self.set_ipv6_v6only(*value);
            }
            ipv6_recverr @ Ipv6Recverr => {
                Self::check_ipv6(family)?;
                let value = ipv6_recverr.get().unwrap();
                self.set_ipv6_recverr(*value);
            }
            ipv6_dontfrag @ Ipv6Dontfrag => {
                Self::check_ipv6(family)?;
                let value = ipv6_dontfrag.get().unwrap();
                self.set_ipv6_dontfrag(*value);
            }
            ipv6_mtu @ Ipv6Mtu => {
                Self::check_ipv6(family)?;
                let value = ipv6_mtu.get().unwrap();
                self.set_ipv6_mtu(*value);
            }
            _ => return_errno_with_message!(
                Errno::ENOPROTOOPT,
                "the socket option to be set is unknown"
            ),
        });

        Ok(NeedIfacePoll::FALSE)
    }
}

impl IpOptionSet {
    /// Rejects an IPv6-only option on a socket of another family.
    fn check_ipv6(family: IpAddressFamily) -> Result<()> {
        if family != IpAddressFamily::IPv6 {
            return_errno_with_message!(
                Errno::ENOPROTOOPT,
                "an IPv6 option is not valid on a non-IPv6 socket"
            );
        }

        Ok(())
    }
}

impl_socket_options!(
    pub(crate) struct Tos(i32);
    pub(crate) struct Ttl(IpTtl);
    pub(crate) struct Hdrincl(bool);
    pub(crate) struct Recverr(bool);
    pub(crate) struct Ipv6V6Only(bool);
    pub(crate) struct Ipv6Recverr(bool);
    pub(crate) struct Ipv6Dontfrag(bool);
    pub(crate) struct Ipv6Mtu(i32);
);

#[derive(Clone, Copy, Debug)]
pub(crate) struct IpTtl(Option<NonZeroU8>);

impl IpTtl {
    pub(crate) const fn new(val: Option<NonZeroU8>) -> Self {
        Self(val)
    }

    pub(crate) const fn get(&self) -> u8 {
        if let Some(val) = self.0 {
            val.get()
        } else {
            DEFAULT_TTL
        }
    }
}

pub(super) trait SetIpLevelOption {
    fn set_hdrincl(&self, _hdrincl: bool) -> Result<()>;
}
