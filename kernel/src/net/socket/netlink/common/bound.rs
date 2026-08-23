// SPDX-License-Identifier: MPL-2.0

use crate::{
    events::IoEvents,
    net::socket::netlink::{
        GroupIdSet, NetlinkSocketAddr, receiver::MessageQueue, table::BoundHandle,
    },
    prelude::*,
    util::bpf::{self, SockFilter},
};

pub struct BoundNetlink<Message: 'static> {
    pub(in crate::net::socket::netlink) handle: BoundHandle<Message>,
    pub(in crate::net::socket::netlink) remote_addr: NetlinkSocketAddr,
    pub(in crate::net::socket::netlink) receive_queue: Arc<Mutex<MessageQueue<Message>>>,
    pub(in crate::net::socket::netlink) filter: Option<Arc<Vec<SockFilter>>>,
}

impl<Message: 'static> BoundNetlink<Message> {
    pub(super) fn new(
        handle: BoundHandle<Message>,
        message_queue: Arc<Mutex<MessageQueue<Message>>>,
        filter: Option<Arc<Vec<SockFilter>>>,
    ) -> Self {
        Self {
            handle,
            remote_addr: NetlinkSocketAddr::new_unspecified(),
            receive_queue: message_queue,
            filter,
        }
    }

    pub(super) fn set_filter(&mut self, filter: Option<Arc<Vec<SockFilter>>>) {
        self.filter = filter;
    }

    /// Returns whether a message passes the attached classic-BPF socket filter
    /// (`SO_ATTACH_FILTER`). Messages are dropped when the filter returns 0 or
    /// when the filter program is malformed.
    pub(in crate::net::socket::netlink) fn filter_allows(&self, message_bytes: &[u8]) -> bool {
        let Some(filter) = &self.filter else {
            return true;
        };
        matches!(bpf::run_filter(filter, message_bytes, true), Some(verdict) if verdict != 0)
    }

    pub(in crate::net::socket::netlink) fn bind_common(
        &mut self,
        endpoint: &NetlinkSocketAddr,
    ) -> Result<()> {
        if endpoint.port() != self.handle.port() {
            return_errno_with_message!(
                Errno::EINVAL,
                "the socket cannot be bound to a different port"
            );
        }

        let groups = endpoint.groups();
        self.handle.bind_groups(groups);

        Ok(())
    }

    pub(in crate::net::socket::netlink) fn check_io_events_common(&self) -> IoEvents {
        let mut events = IoEvents::OUT;

        let receive_queue = self.receive_queue.lock();
        if !receive_queue.is_empty() {
            events |= IoEvents::IN;
        }
        if receive_queue.has_errors() {
            events |= IoEvents::ERR;
        }

        events
    }

    pub(super) fn add_groups(&mut self, groups: GroupIdSet) {
        self.handle.add_groups(groups);
    }

    pub(super) fn drop_groups(&mut self, groups: GroupIdSet) {
        self.handle.drop_groups(groups);
    }
}
