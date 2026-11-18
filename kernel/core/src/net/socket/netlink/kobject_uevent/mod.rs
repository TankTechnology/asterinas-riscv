// SPDX-License-Identifier: MPL-2.0

pub(super) use message::UeventMessage;

use crate::{
    net::socket::netlink::{
        addr::GroupIdSet,
        common::NetlinkSocket,
        table::{NetlinkUeventProtocol, SupportedNetlinkProtocol},
    },
    prelude::*,
};

mod bound;
mod message;

pub(crate) type NetlinkUeventSocket = NetlinkSocket<NetlinkUeventProtocol>;

/// The group the kernel announces device events on.
const UDEV_MONITOR_KERNEL: u32 = 1;

/// Announces a device event to every listener in the kernel group.
///
/// This is the sending half of `NETLINK_KOBJECT_UEVENT`: user space binds to
/// the group, the kernel broadcasts, and a listener's `SO_ATTACH_FILTER`
/// decides which events it actually sees.
pub(crate) fn broadcast(
    action: &str,
    devpath: &str,
    subsystem: &str,
    envs: Vec<(String, String)>,
) -> Result<()> {
    let message = UeventMessage::new_kernel_event(action, devpath, subsystem, envs)?;
    <NetlinkUeventProtocol as SupportedNetlinkProtocol>::multicast(
        GroupIdSet::from_group_id(UDEV_MONITOR_KERNEL)?,
        message,
    )
}
