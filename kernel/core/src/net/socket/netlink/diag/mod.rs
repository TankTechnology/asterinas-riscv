// SPDX-License-Identifier: MPL-2.0

//! `NETLINK_SOCK_DIAG` socket support.
//!
//! `ss` and other monitoring tools dump socket tables through this protocol.
//! The socket machinery is shared with the route socket; only the message type
//! and the request handling differ.

mod bound;
mod kernel;
mod message;

pub(crate) use message::SockDiagMessage;

use crate::net::socket::netlink::{common::NetlinkSocket, table::NetlinkSockDiagProtocol};

pub(crate) type NetlinkSockDiagSocket = NetlinkSocket<NetlinkSockDiagProtocol>;
