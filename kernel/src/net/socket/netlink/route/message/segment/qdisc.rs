// SPDX-License-Identifier: MPL-2.0

use crate::{
    net::socket::netlink::message::{SegmentBody, SegmentCommon},
    prelude::*,
};

pub type QdiscSegment =
    SegmentCommon<QdiscSegmentBody, crate::net::socket::netlink::message::NoAttr>;

/// `tcmsg` from rtnetlink.  The fields are intentionally opaque until qdisc
/// objects are modelled; networkd only needs an empty dump terminator today.
#[repr(C)]
#[derive(Clone, Copy, Debug, Pod)]
pub struct CQdiscMsg {
    pub family: u8,
    pub _pad: [u8; 3],
    pub ifindex: i32,
    pub handle: u32,
    pub parent: u32,
    pub info: u32,
}

#[derive(Clone, Copy, Debug)]
pub struct QdiscSegmentBody {
    pub raw: CQdiscMsg,
}

impl SegmentBody for QdiscSegmentBody {
    type CLegacyType = CQdiscMsg;
    type CType = CQdiscMsg;
}

impl TryFrom<CQdiscMsg> for QdiscSegmentBody {
    type Error = Error;

    fn try_from(value: CQdiscMsg) -> Result<Self> {
        Ok(Self { raw: value })
    }
}

impl From<QdiscSegmentBody> for CQdiscMsg {
    fn from(value: QdiscSegmentBody) -> Self {
        value.raw
    }
}
