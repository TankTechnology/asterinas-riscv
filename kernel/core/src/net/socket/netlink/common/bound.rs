// SPDX-License-Identifier: MPL-2.0

#![short_vis_path::add(netlink)]

use crate::{
    events::IoEvents,
    net::socket::netlink::{
        GroupIdSet, NetlinkSocketAddr, receiver::MessageQueue, table::BoundHandle,
    },
    prelude::*,
};

pub struct BoundNetlink<Message: 'static> {
    pub(in crate::net::socket::netlink) handle: BoundHandle<Message>,
    pub(in crate::net::socket::netlink) remote_addr: NetlinkSocketAddr,
    pub(in crate::net::socket::netlink) receive_queue: Arc<Mutex<MessageQueue<Message>>>,
    pub(in crate::net::socket::netlink) filter: Option<Arc<Vec<crate::util::bpf::SockFilter>>>,
    pub(in crate::net::socket::netlink) ebpf: Option<Arc<crate::bpf::Program>>,
}

impl<Message: 'static> BoundNetlink<Message> {
    pub(super) fn new(
        handle: BoundHandle<Message>,
        message_queue: Arc<Mutex<MessageQueue<Message>>>,
        filter: Option<Arc<Vec<crate::util::bpf::SockFilter>>>,
    ) -> Self {
        Self {
            handle,
            remote_addr: NetlinkSocketAddr::new_unspecified(),
            receive_queue: message_queue,
            filter,
            ebpf: None,
        }
    }

    /// Carries an eBPF filter attached before binding onto the bound socket.
    pub(super) fn with_ebpf(mut self, program: Option<Arc<crate::bpf::Program>>) -> Self {
        self.ebpf = program;
        self
    }

    /// Replaces the attached classic-BPF filter.
    pub(super) fn set_filter(&mut self, filter: Option<Arc<Vec<crate::util::bpf::SockFilter>>>) {
        self.filter = filter;
    }

    /// Replaces the attached eBPF filter (`SO_ATTACH_BPF`).
    pub(super) fn set_ebpf_filter(&mut self, program: Option<Arc<crate::bpf::Program>>) {
        self.ebpf = program;
    }

    /// Returns whether the attached filters accept `payload`.
    ///
    /// Both forms run: the classic BPF one (`SO_ATTACH_FILTER`) and the eBPF
    /// one (`SO_ATTACH_BPF`). A message is dropped when a filter returns zero
    /// or when a filter faults.
    pub(in netlink) fn filter_allows(&self, payload: &[u8]) -> bool {
        if let Some(program) = &self.ebpf {
            match program.run_on_packet(payload) {
                Some(verdict) if verdict != 0 => {}
                _ => return false,
            }
        }
        match &self.filter {
            Some(filter) => {
                crate::util::bpf::run_filter(filter, payload, true).is_some_and(|ret| ret != 0)
            }
            None => true,
        }
    }

    pub(in netlink) fn bind_common(&mut self, endpoint: &NetlinkSocketAddr) -> Result<()> {
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

    pub(in netlink) fn check_io_events_common(&self) -> IoEvents {
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
