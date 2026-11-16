// SPDX-License-Identifier: MPL-2.0

use core::marker::PhantomData;

use crate::{
    bpf::Program,
    events::IoEvents,
    net::socket::{
        netlink::{
            GroupIdSet, NetlinkSocketAddr, common::bound::BoundNetlink, receiver::MessageQueue,
            table::SupportedNetlinkProtocol,
        },
        util::datagram_common,
    },
    prelude::*,
    process::signal::Pollee,
};

pub(super) struct UnboundNetlink<P: SupportedNetlinkProtocol> {
    groups: GroupIdSet,
    /// An eBPF filter attached before binding; it moves to the bound socket.
    ebpf: Option<Arc<Program>>,
    phantom: PhantomData<BoundNetlink<P::Message>>,
}

impl<P: SupportedNetlinkProtocol> UnboundNetlink<P> {
    pub(super) const fn new() -> Self {
        Self {
            groups: GroupIdSet::new_empty(),
            ebpf: None,
            phantom: PhantomData,
        }
    }

    pub(super) fn addr(&self) -> NetlinkSocketAddr {
        NetlinkSocketAddr::new(0, self.groups)
    }

    pub(super) fn add_groups(&mut self, groups: GroupIdSet) {
        self.groups.add_groups(groups);
    }

    pub(super) fn drop_groups(&mut self, groups: GroupIdSet) {
        self.groups.drop_groups(groups);
    }

    pub(super) fn set_ebpf_filter(&mut self, program: Option<Arc<Program>>) {
        self.ebpf = program;
    }
}

impl<P: SupportedNetlinkProtocol> datagram_common::Unbound for UnboundNetlink<P> {
    type Endpoint = NetlinkSocketAddr;
    type BindOptions = ();

    type Bound = BoundNetlink<P::Message>;

    fn bind(
        &mut self,
        endpoint: &Self::Endpoint,
        pollee: &Pollee,
        _options: Self::BindOptions,
    ) -> Result<Self::Bound> {
        let (message_queue, message_receiver) =
            MessageQueue::<P::Message>::new_pair(pollee.clone());

        let bound_handle = {
            let endpoint = {
                let mut endpoint = *endpoint;
                endpoint.add_groups(self.groups);
                endpoint
            };
            <P as SupportedNetlinkProtocol>::bind(&endpoint, message_receiver)?
        };

        Ok(BoundNetlink::new(bound_handle, message_queue, None).with_ebpf(self.ebpf.take()))
    }

    fn bind_ephemeral(
        &mut self,
        _remote_endpoint: &Self::Endpoint,
        pollee: &Pollee,
        _options: Self::BindOptions,
    ) -> Result<Self::Bound> {
        let (message_queue, message_receiver) =
            MessageQueue::<P::Message>::new_pair(pollee.clone());

        let bound_handle = {
            let endpoint = {
                let mut endpoint = NetlinkSocketAddr::new_unspecified();
                endpoint.add_groups(self.groups);
                endpoint
            };
            <P as SupportedNetlinkProtocol>::bind(&endpoint, message_receiver)?
        };

        Ok(BoundNetlink::new(bound_handle, message_queue, None).with_ebpf(self.ebpf.take()))
    }

    fn check_io_events(&self) -> IoEvents {
        IoEvents::OUT
    }
}
