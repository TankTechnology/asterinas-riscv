// SPDX-License-Identifier: MPL-2.0

use core::marker::PhantomData;

use crate::{
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
    util::bpf::SockFilter,
};

pub(super) struct UnboundNetlink<P: SupportedNetlinkProtocol> {
    groups: GroupIdSet,
    filter: Option<Arc<Vec<SockFilter>>>,
    phantom: PhantomData<BoundNetlink<P::Message>>,
}

impl<P: SupportedNetlinkProtocol> UnboundNetlink<P> {
    pub(super) const fn new() -> Self {
        Self {
            groups: GroupIdSet::new_empty(),
            filter: None,
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

    pub(super) fn set_filter(&mut self, filter: Option<Arc<Vec<SockFilter>>>) {
        self.filter = filter;
    }

    fn bind_common(
        &mut self,
        endpoint: NetlinkSocketAddr,
        pollee: &Pollee,
    ) -> Result<BoundNetlink<P::Message>> {
        let (message_queue, message_receiver) =
            MessageQueue::<P::Message>::new_pair(pollee.clone());

        let bound_handle = {
            let mut endpoint = endpoint;
            endpoint.add_groups(self.groups);
            <P as SupportedNetlinkProtocol>::bind(&endpoint, message_receiver)?
        };

        Ok(BoundNetlink::new(
            bound_handle,
            message_queue,
            self.filter.take(),
        ))
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
        self.bind_common(*endpoint, pollee)
    }

    fn bind_ephemeral(
        &mut self,
        _remote_endpoint: &Self::Endpoint,
        pollee: &Pollee,
    ) -> Result<Self::Bound> {
        self.bind_common(NetlinkSocketAddr::new_unspecified(), pollee)
    }

    fn check_io_events(&self) -> IoEvents {
        IoEvents::OUT
    }
}
