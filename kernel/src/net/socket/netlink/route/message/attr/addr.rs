// SPDX-License-Identifier: MPL-2.0

use core::net::{IpAddr, Ipv4Addr};

use zerocopy::Immutable;

use crate::{
    net::socket::netlink::{
        message::{Attribute, CAttrHeader, ContinueRead},
        route::message::AddrMessageFlags,
    },
    prelude::*,
    util::MultiRead,
};

/// Address-related attributes.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.13/source/include/uapi/linux/if_addr.h#L26>.
#[expect(non_camel_case_types)]
#[expect(clippy::upper_case_acronyms)]
#[repr(u16)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, TryFromInt)]
enum AddrAttrClass {
    UNSPEC = 0,
    ADDRESS = 1,
    LOCAL = 2,
    LABEL = 3,
    BROADCAST = 4,
    ANYCAST = 5,
    CACHEINFO = 6,
    MULTICAST = 7,
    FLAGS = 8,
    RT_PRIORITY = 9,
    TARGET_NETNSID = 10,
    PROTO = 11,
}

/// The source protocol of an interface address.
///
/// Reference: <https://elixir.bootlin.com/linux/v7.1/source/include/uapi/linux/if_addr.h#L73>.
#[repr(u8)]
#[derive(Clone, Copy, Debug, Immutable, IntoBytes)]
pub enum AddrProtocol {
    /// An unspecified address source.
    #[expect(dead_code)]
    Unspecified = 0,
    /// A loopback address configured by the kernel.
    KernelLoopback = 1,
    /// An address configured by the kernel from a Router Advertisement.
    #[expect(dead_code)]
    KernelRouterAdvertisement = 2,
    /// A link-local address configured by the kernel.
    #[expect(dead_code)]
    KernelLinkLocal = 3,
}

#[derive(Debug)]
pub enum AddrAttr {
    Address(IpAddr),
    Broadcast(Ipv4Addr),
    Flags(AddrMessageFlags),
    Label(CString),
    Local(IpAddr),
    Protocol(AddrProtocol),
}

impl AddrAttr {
    fn class(&self) -> AddrAttrClass {
        match self {
            AddrAttr::Address(_) => AddrAttrClass::ADDRESS,
            AddrAttr::Broadcast(_) => AddrAttrClass::BROADCAST,
            AddrAttr::Flags(_) => AddrAttrClass::FLAGS,
            AddrAttr::Label(_) => AddrAttrClass::LABEL,
            AddrAttr::Local(_) => AddrAttrClass::LOCAL,
            AddrAttr::Protocol(_) => AddrAttrClass::PROTO,
        }
    }
}

impl Attribute for AddrAttr {
    fn type_(&self) -> u16 {
        self.class() as u16
    }

    fn payload_as_bytes(&self) -> &[u8] {
        match self {
            AddrAttr::Address(address) => address.as_octets(),
            AddrAttr::Broadcast(address) => address.as_octets(),
            AddrAttr::Flags(flags) => flags.as_bytes(),
            AddrAttr::Label(label) => label.as_bytes_with_nul(),
            AddrAttr::Local(address) => address.as_octets(),
            AddrAttr::Protocol(protocol) => protocol.as_bytes(),
        }
    }

    fn read_from(header: &CAttrHeader, reader: &mut dyn MultiRead) -> Result<ContinueRead<Self>>
    where
        Self: Sized,
    {
        let payload_len = header.payload_len();

        // TODO: Currently, `IS_NET_BYTEORDER_MASK` and `IS_NESTED_MASK` are ignored.
        let Ok(class) = AddrAttrClass::try_from(header.type_()) else {
            // Unknown attributes should be ignored.
            // Reference: <https://docs.kernel.org/userspace-api/netlink/intro.html#unknown-attributes>.
            reader.skip_some(payload_len);
            return Ok(ContinueRead::Skipped);
        };

        let res = match (class, payload_len) {
            // IPv4 address attributes carry 4 bytes, IPv6 ones 16 bytes.
            (AddrAttrClass::ADDRESS, 4) => Self::Address(IpAddr::from(read_ipv4_addr(reader)?)),
            (AddrAttrClass::ADDRESS, 16) => Self::Address(read_ipv6_addr(reader)?),
            (AddrAttrClass::LOCAL, 4) => Self::Local(IpAddr::from(read_ipv4_addr(reader)?)),
            (AddrAttrClass::LOCAL, 16) => Self::Local(read_ipv6_addr(reader)?),

            (AddrAttrClass::ADDRESS | AddrAttrClass::LOCAL, _) => {
                warn!("addr attribute `{:?}` contains invalid payload", class);
                reader.skip_some(payload_len);
                return Ok(ContinueRead::skipped_with_error(
                    Errno::EINVAL,
                    "the addr attribute is invalid",
                ));
            }

            (_, _) => {
                // Known attributes that we do not use (e.g. IFA_FLAGS) are
                // ignored silently, like unknown ones.
                reader.skip_some(payload_len);
                return Ok(ContinueRead::Skipped);
            }
        };

        Ok(ContinueRead::Parsed(res))
    }
}

/// Reads an IPv4 address from the reader (in network byte order).
fn read_ipv4_addr(reader: &mut dyn MultiRead) -> Result<Ipv4Addr> {
    let bytes = reader.read_val_opt::<[u8; 4]>()?.unwrap();
    Ok(Ipv4Addr::from(bytes))
}

/// Reads an IPv6 address from the reader (in network byte order).
fn read_ipv6_addr(reader: &mut dyn MultiRead) -> Result<IpAddr> {
    let bytes = reader.read_val_opt::<[u8; 16]>()?.unwrap();
    Ok(IpAddr::from(bytes))
}
