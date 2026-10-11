// SPDX-License-Identifier: MPL-2.0

#![cfg_attr(not(ktest), expect(dead_code))]

use uevent::Uevent;

use crate::{
    net::socket::netlink::{
        NetlinkSocketAddr, receiver::QueueableMessage, table::MulticastMessage,
    },
    prelude::*,
    util::{
        MultiWrite,
        bpf::{self, SockFilter},
    },
};

mod syn_uevent;
#[cfg(ktest)]
mod test;
mod uevent;

/// A uevent message.
///
/// Note that uevent messages are not the same as common netlink messages.
/// It does not have a netlink header.
#[derive(Clone, Debug)]
pub(crate) struct UeventMessage {
    uevent: Vec<u8>,
    src_addr: NetlinkSocketAddr,
}

impl UeventMessage {
    /// Creates a new uevent message.
    fn new(uevent: Uevent, src_addr: NetlinkSocketAddr) -> Self {
        Self {
            uevent: uevent.to_string().into_bytes(),
            src_addr,
        }
    }

    /// Returns the source address of the uevent message.
    pub(super) fn src_addr(&self) -> &NetlinkSocketAddr {
        &self.src_addr
    }

    /// Writes the uevent to the given `writer`.
    pub(super) fn write_to(&self, writer: &mut dyn MultiWrite) -> Result<()> {
        let _nbytes = writer.write(&mut VmReader::from(self.uevent.as_slice()))?;
        // `_nbytes` may be smaller than the message size. We ignore it to truncate the message.

        Ok(())
    }
}

impl QueueableMessage for UeventMessage {
    fn total_len(&self) -> usize {
        self.uevent.len()
    }

    fn apply_filter(&mut self, program: &[SockFilter]) -> bool {
        let retained = bpf::run(program, &self.uevent) as usize;
        if retained == 0 {
            return false;
        }
        self.uevent.truncate(retained);
        true
    }
}

impl MulticastMessage for UeventMessage {}
