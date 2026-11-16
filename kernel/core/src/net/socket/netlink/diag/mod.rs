// SPDX-License-Identifier: MPL-2.0

//! Netlink `SOCK_DIAG` socket: the `socket monitoring` family `ss` uses.

mod bound;
mod kernel;
mod message;

pub(crate) use message::{CDiagMsg, CDiagReqV2, DiagSegment, SOCK_DIAG_BY_FAMILY};

use crate::net::socket::netlink::{
    common::NetlinkSocket, message::Message, table::NetlinkDiagProtocol,
};

/// A netlink `SOCK_DIAG` socket.
pub(crate) type NetlinkDiagSocket = NetlinkSocket<NetlinkDiagProtocol>;

/// A netlink `SOCK_DIAG` message.
pub(crate) type DiagMessage = Message<DiagSegment>;
