// SPDX-License-Identifier: MPL-2.0

//! Netlink `SOCK_DIAG` socket: the `socket monitoring` family `ss` uses.

mod message;

pub(crate) use message::{CDiagMsg, CDiagReqV2, DiagSegment, SOCK_DIAG_BY_FAMILY};

use crate::net::socket::netlink::message::Message;

/// A netlink `SOCK_DIAG` message.
pub(crate) type DiagMessage = Message<DiagSegment>;
