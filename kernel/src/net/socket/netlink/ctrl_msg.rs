// SPDX-License-Identifier: MPL-2.0

//! Control messages for netlink sockets.

use super::addr::NetlinkProtocolId;
use crate::{
    net::socket::util::{CControlHeader, RecvFlags},
    prelude::*,
    util::net::CSocketOptionLevel,
};

/// Control messages for netlink sockets.
#[derive(Debug)]
pub enum NetlinkControlMessage {
    /// The `NETLINK_PKTINFO` control message (enabled via the
    /// `NETLINK_PKTINFO` socket option).
    PktInfo(NetlinkPktInfo),
}

impl NetlinkControlMessage {
    /// Creates a `NETLINK_PKTINFO` control message.
    pub const fn new_pktinfo(group: NetlinkProtocolId) -> Self {
        NetlinkControlMessage::PktInfo(NetlinkPktInfo { group })
    }

    pub(in crate::net) fn write_to(
        &self,
        writer: &mut VmWriter,
    ) -> Result<(CControlHeader, RecvFlags)> {
        match self {
            NetlinkControlMessage::PktInfo(pktinfo) => pktinfo.write_to(writer),
        }
    }
}

/// The payload of the `NETLINK_PKTINFO` control message
/// (`struct nl_pktinfo` in `linux/netlink.h`).
#[derive(Debug)]
pub struct NetlinkPktInfo {
    /// The multicast group the message was sent to, or 0 for unicast.
    group: NetlinkProtocolId,
}

impl NetlinkPktInfo {
    fn write_to(&self, writer: &mut VmWriter) -> Result<(CControlHeader, RecvFlags)> {
        let header = CControlHeader::new(
            CSocketOptionLevel::SOL_NETLINK,
            CControlType::PKTINFO as i32,
            size_of::<NetlinkProtocolId>(),
        );
        writer.write_val(&header)?;
        writer.write_val(&self.group)?;

        Ok((header, RecvFlags::empty()))
    }
}

/// Control message types for netlink sockets
/// (`linux/netlink.h`).
#[expect(clippy::upper_case_acronyms)]
#[repr(i32)]
#[derive(Clone, Copy, Debug, TryFromInt)]
enum CControlType {
    PKTINFO = 3,
}
