// SPDX-License-Identifier: MPL-2.0

use crate::{
    net::socket::netlink::{
        message::{SegmentBody, SegmentCommon},
        route::message::attr::route::RouteAttr,
    },
    prelude::*,
};

pub type RouteSegment = SegmentCommon<RouteSegmentBody, RouteAttr>;

impl SegmentBody for RouteSegmentBody {
    type CType = CRtMsg;
}

#[derive(Clone, Copy, Debug)]
pub struct RouteSegmentBody {
    pub family: u8,
    pub dst_len: u8,
    pub src_len: u8,
    pub tos: u8,
    pub table: u8,
    pub protocol: u8,
    pub scope: u8,
    pub type_: u8,
    pub flags: u32,
}

/// `rtmsg` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.13/source/include/uapi/linux/rtnetlink.h#L237>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct CRtMsg {
    family: u8,
    dst_len: u8,
    src_len: u8,
    tos: u8,
    table: u8,
    protocol: u8,
    scope: u8,
    type_: u8,
    flags: u32,
}

impl TryFrom<CRtMsg> for RouteSegmentBody {
    type Error = Error;

    fn try_from(value: CRtMsg) -> Result<Self> {
        Ok(Self {
            family: value.family,
            dst_len: value.dst_len,
            src_len: value.src_len,
            tos: value.tos,
            table: value.table,
            protocol: value.protocol,
            scope: value.scope,
            type_: value.type_,
            flags: value.flags,
        })
    }
}

impl From<RouteSegmentBody> for CRtMsg {
    fn from(value: RouteSegmentBody) -> Self {
        Self {
            family: value.family,
            dst_len: value.dst_len,
            src_len: value.src_len,
            tos: value.tos,
            table: value.table,
            protocol: value.protocol,
            scope: value.scope,
            type_: value.type_,
            flags: value.flags,
        }
    }
}
