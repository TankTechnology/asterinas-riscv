// SPDX-License-Identifier: MPL-2.0

use crate::{net::socket::options::macros::impl_socket_options, prelude::*};

impl_socket_options!(
    pub struct AddMembership(u32);
    pub struct DropMembership(u32);
    pub struct PktInfo(bool);
    pub struct ExtAck(bool);
    pub struct GetStrictChk(bool);
    pub struct ListMemberships(Vec<u32>);
);
