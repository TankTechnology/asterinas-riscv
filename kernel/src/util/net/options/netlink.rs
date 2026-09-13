// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::{
    RawSocketOption, SocketOption, impl_raw_sock_option_get_only, impl_raw_sock_option_set_only,
    impl_raw_socket_option,
};
use crate::{
    context::current_userspace,
    net::socket::netlink::{
        AddMembership, DropMembership, ExtAck, GetStrictChk, ListMemberships, PktInfo,
    },
    prelude::*,
};

/// Socket options for netlink socket.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.0.9/source/include/uapi/linux/netlink.h#L149>.
#[expect(non_camel_case_types)]
#[expect(clippy::upper_case_acronyms)]
#[repr(i32)]
#[derive(Clone, Copy, Debug, TryFromInt)]
pub enum CNetlinkOptionName {
    ADD_MEMBERSHIP = 1,
    DROP_MEMBERSHIP = 2,
    PKTINFO = 3,
    LIST_MEMBERSHIPS = 9,
    EXT_ACK = 11,
    GET_STRICT_CHK = 12,
}

pub fn new_netlink_option(name: i32) -> Result<Box<dyn RawSocketOption>> {
    let name = CNetlinkOptionName::try_from(name).map_err(|_| Errno::ENOPROTOOPT)?;
    match name {
        CNetlinkOptionName::ADD_MEMBERSHIP => Ok(Box::new(AddMembership::new())),
        CNetlinkOptionName::DROP_MEMBERSHIP => Ok(Box::new(DropMembership::new())),
        CNetlinkOptionName::PKTINFO => Ok(Box::new(PktInfo::new())),
        CNetlinkOptionName::LIST_MEMBERSHIPS => Ok(Box::new(ListMemberships::new())),
        CNetlinkOptionName::EXT_ACK => Ok(Box::new(ExtAck::new())),
        CNetlinkOptionName::GET_STRICT_CHK => Ok(Box::new(GetStrictChk::new())),
    }
}

impl_raw_sock_option_set_only!(AddMembership);
impl_raw_sock_option_set_only!(DropMembership);
impl_raw_socket_option!(PktInfo);
impl_raw_socket_option!(ExtAck);
impl_raw_sock_option_get_only!(GetStrictChk);

// NETLINK_LIST_MEMBERSHIPS is get-only and supports the NULL-buffer size query
// (used by systemd's sd_netlink_open).
impl RawSocketOption for ListMemberships {
    fn read_from_user(&mut self, _addr: Vaddr, _max_len: u32) -> Result<()> {
        return_errno_with_message!(Errno::ENOPROTOOPT, "the option is getter-only");
    }

    fn write_to_user(&self, addr: Vaddr, max_len: &mut u32) -> Result<usize> {
        let groups: &[u32] = self.get().map(Vec::as_slice).unwrap_or(&[]);
        let needed = size_of_val(groups) as u32;

        if addr != 0 {
            let count = (*max_len as usize / size_of::<u32>()).min(groups.len());
            for (i, group) in groups.iter().take(count).enumerate() {
                current_userspace!().write_val(addr + i * size_of::<u32>(), group)?;
            }
        }

        *max_len = needed;
        Ok(needed as usize)
    }

    fn as_sock_option_mut(&mut self) -> &mut dyn SocketOption {
        self
    }

    fn as_sock_option(&self) -> &dyn SocketOption {
        self
    }
}
