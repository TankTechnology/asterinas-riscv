// SPDX-License-Identifier: MPL-2.0

//! Attributes carried by IPv4 route messages.

use crate::{
    net::socket::netlink::message::{Attribute, CAttrHeader, ContinueRead},
    prelude::*,
    util::MultiRead,
};

/// `RTA_*` in Linux's `rtnetlink.h`.
#[repr(u16)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, TryFromInt)]
enum RouteAttrClass {
    Destination = 1,
    OutputInterface = 4,
    Gateway = 5,
    PreferredSource = 7,
    Table = 15,
}

#[derive(Debug)]
pub enum RouteAttr {
    Destination([u8; 4]),
    OutputInterface(u32),
    Gateway([u8; 4]),
    PreferredSource([u8; 4]),
    Table(u32),
}

impl RouteAttr {
    fn class(&self) -> RouteAttrClass {
        match self {
            Self::Destination(_) => RouteAttrClass::Destination,
            Self::OutputInterface(_) => RouteAttrClass::OutputInterface,
            Self::Gateway(_) => RouteAttrClass::Gateway,
            Self::PreferredSource(_) => RouteAttrClass::PreferredSource,
            Self::Table(_) => RouteAttrClass::Table,
        }
    }
}

impl Attribute for RouteAttr {
    fn type_(&self) -> u16 {
        self.class() as u16
    }

    fn payload_as_bytes(&self) -> &[u8] {
        match self {
            Self::Destination(address)
            | Self::Gateway(address)
            | Self::PreferredSource(address) => address,
            Self::OutputInterface(index) | Self::Table(index) => index.as_bytes(),
        }
    }

    fn read_from(header: &CAttrHeader, reader: &mut dyn MultiRead) -> Result<ContinueRead<Self>> {
        let payload_len = header.payload_len();
        let Ok(class) = RouteAttrClass::try_from(header.type_()) else {
            reader.skip_some(payload_len);
            return Ok(ContinueRead::Skipped);
        };

        let attr = match (class, payload_len) {
            (RouteAttrClass::Destination, 4) => {
                Self::Destination(reader.read_val_opt::<[u8; 4]>()?.unwrap())
            }
            (RouteAttrClass::OutputInterface, 4) => {
                Self::OutputInterface(reader.read_val_opt::<u32>()?.unwrap())
            }
            (RouteAttrClass::Gateway, 4) => {
                Self::Gateway(reader.read_val_opt::<[u8; 4]>()?.unwrap())
            }
            (RouteAttrClass::PreferredSource, 4) => {
                Self::PreferredSource(reader.read_val_opt::<[u8; 4]>()?.unwrap())
            }
            (RouteAttrClass::Table, 4) => Self::Table(reader.read_val_opt::<u32>()?.unwrap()),
            _ => {
                reader.skip_some(payload_len);
                return Ok(ContinueRead::skipped_with_error(
                    Errno::EINVAL,
                    "the route attribute is invalid",
                ));
            }
        };
        Ok(ContinueRead::Parsed(attr))
    }
}
