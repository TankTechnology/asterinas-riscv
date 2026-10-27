// SPDX-License-Identifier: MPL-2.0

use crate::net::socket::options::macros::impl_socket_options;

impl_socket_options!(
    pub(crate) struct AddMembership(u32);
    pub(crate) struct DropMembership(u32);
    /// `NETLINK_EXT_ACK`. Stored only: extended ACK messages are not generated,
    /// so the flag is accepted and read back but does not change replies.
    pub(crate) struct ExtAck(bool);
);
