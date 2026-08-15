// SPDX-License-Identifier: MPL-2.0

use alloc::vec;

use aster_bigtcp::{
    device::{self, NotifyDevice},
    time::Instant,
};
use ostd::mm::VmWriter;

use crate::{AnyNetworkDevice, NetError, buffer::RxBuffer};

impl device::Device for dyn AnyNetworkDevice {
    type RxToken<'a> = RxToken;
    type TxToken<'a> = TxToken<'a>;

    fn receive(&mut self, _timestamp: Instant) -> Option<(Self::RxToken<'_>, Self::TxToken<'_>)> {
        // Receiving must not be gated on the transmit queue being able to accept a
        // new packet. A full send queue would otherwise block *all* ingress and
        // leave received packets (SYN-ACKs, TLS/HTTP data, ACKs) stuck in the used
        // ring until the queue drains, which shows up as intermittent connect
        // failures and mid-transfer receive errors under load. Replies are sent on
        // a best-effort basis by `TxToken::consume` instead.
        if self.can_receive() {
            let rx_buffer = self.receive().unwrap();
            Some((RxToken(rx_buffer), TxToken(self)))
        } else {
            None
        }
    }

    fn transmit(&mut self, _timestamp: Instant) -> Option<Self::TxToken<'_>> {
        if self.can_send() {
            Some(TxToken(self))
        } else {
            None
        }
    }

    fn capabilities(&self) -> device::DeviceCapabilities {
        self.capabilities()
    }
}

impl NotifyDevice for dyn AnyNetworkDevice {
    fn notify_poll_end(&mut self) {
        self.notify_poll_end();
    }
}

pub struct RxToken(RxBuffer);

impl device::RxToken for RxToken {
    fn consume<R, F>(self, f: F) -> R
    where
        F: FnOnce(&[u8]) -> R,
    {
        let mut payload = self.0.payload();
        let mut buffer = vec![0u8; payload.remain()];
        payload.read(&mut VmWriter::from(&mut buffer as &mut [u8]));
        f(&buffer)
    }
}

pub struct TxToken<'a>(&'a mut dyn AnyNetworkDevice);

impl device::TxToken for TxToken<'_> {
    fn consume<R, F>(self, len: usize, f: F) -> R
    where
        F: FnOnce(&mut [u8]) -> R,
    {
        let mut buffer = vec![0u8; len];
        let res = f(&mut buffer);
        match self.0.send(&buffer) {
            Ok(()) => {}
            // A reply (ACK / ARP reply / RST) is best-effort: if the send queue is
            // momentarily full, drop it rather than panic. TCP retransmits, so a
            // dropped ACK only adds a little latency; it never breaks correctness.
            Err(NetError::Busy) => {}
            Err(err) => panic!("Send packet failed: {:?}", err),
        }
        res
    }
}
