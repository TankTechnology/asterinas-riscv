// SPDX-License-Identifier: MPL-2.0

use alloc::{
    boxed::Box,
    collections::{btree_map::BTreeMap, vec_deque::VecDeque},
    ffi::CString,
    sync::Arc,
    vec,
};

use aster_softirq::BottomHalfDisabled;
use ostd::sync::SpinLock;
use smoltcp::{
    iface::{Config, Context, packet::Packet},
    phy::{Device, DeviceCapabilities, TxToken},
    wire::{
        self, ArpOperation, ArpPacket, ArpRepr, EthernetAddress, EthernetFrame, EthernetProtocol,
        EthernetRepr, IpAddress, IpRepr, Ipv4Address, Ipv4AddressExt, Ipv4Cidr, Ipv4Packet,
        Ipv6Packet,
    },
};

use crate::{
    device::{NotifyDevice, WithDevice},
    ext::Ext,
    iface::{
        Iface, InterfaceFlags, ScheduleNextPoll,
        common::{IfaceCommon, InterfaceType, IpPacket},
        iface::internal::IfaceInternal,
        time::get_network_timestamp,
    },
};

pub struct EtherIface<D, E: Ext> {
    driver: D,
    common: IfaceCommon<E>,
    ether_addr: EthernetAddress,
    arp_table: SpinLock<BTreeMap<Ipv4Address, EthernetAddress>, BottomHalfDisabled>,
    /// Serialized IPv4 packets waiting for an ARP resolution.
    ///
    /// When the next-hop Ethernet address is not yet known, the outgoing
    /// packet used to be dropped and the upper layer had to notice the loss
    /// and retransmit. That breaks one-shot protocols such as UDP DNS
    /// queries, so we queue the packet here and flush the queue once the ARP
    /// reply arrives (which triggers another interface poll).
    pending_tx: SpinLock<PendingTxState, BottomHalfDisabled>,
}

/// The maximum number of packets queued for ARP resolution.
const MAX_PENDING_TX: usize = 64;
/// The maximum time an IPv4 packet waits for an ARP resolution.
const PENDING_TX_LIFETIME_MS: u64 = 5_000;
/// The minimum delay between ARP requests for one unresolved neighbor.
const ARP_RETRY_INTERVAL_MS: u64 = 1_000;

struct PendingTxPacket {
    bytes: Box<[u8]>,
    next_hop: Ipv4Address,
    expires_at_ms: u64,
}

enum PendingTxAction {
    Transmit {
        packet: PendingTxPacket,
        ether_addr: EthernetAddress,
    },
    RequestArp(Ipv4Address),
    Idle,
}

struct PendingTxState {
    packets: VecDeque<PendingTxPacket>,
    last_arp_request_ms: BTreeMap<Ipv4Address, u64>,
}

impl PendingTxState {
    fn new() -> Self {
        Self {
            packets: VecDeque::new(),
            last_arp_request_ms: BTreeMap::new(),
        }
    }

    fn enqueue(&mut self, bytes: Box<[u8]>, next_hop: Ipv4Address, now_ms: u64) -> bool {
        self.prune_expired(now_ms);
        if self.packets.len() >= MAX_PENDING_TX {
            return false;
        }

        self.packets.push_back(PendingTxPacket {
            bytes,
            next_hop,
            expires_at_ms: now_ms.saturating_add(PENDING_TX_LIFETIME_MS),
        });
        true
    }

    fn should_request_arp(&mut self, next_hop: Ipv4Address, now_ms: u64) -> bool {
        if !self.is_arp_request_due(next_hop, now_ms) {
            return false;
        }

        self.last_arp_request_ms.insert(next_hop, now_ms);
        true
    }

    fn is_arp_request_due(&self, next_hop: Ipv4Address, now_ms: u64) -> bool {
        self.last_arp_request_ms
            .get(&next_hop)
            .is_none_or(|last_ms| now_ms.saturating_sub(*last_ms) >= ARP_RETRY_INTERVAL_MS)
    }

    fn next_action(
        &mut self,
        now_ms: u64,
        mut resolve_fn: impl FnMut(Ipv4Address) -> Option<EthernetAddress>,
    ) -> PendingTxAction {
        self.prune_expired(now_ms);
        let mut arp_target = None;
        let packet_count = self.packets.len();

        for _ in 0..packet_count {
            let packet = self.packets.pop_front().unwrap();
            if let Some(ether_addr) = resolve_fn(packet.next_hop) {
                self.remove_request_if_unused(packet.next_hop);
                return PendingTxAction::Transmit { packet, ether_addr };
            }

            let next_hop = packet.next_hop;
            self.packets.push_back(packet);
            if arp_target.is_none() && self.is_arp_request_due(next_hop, now_ms) {
                arp_target = Some(next_hop);
            }
        }

        let Some(next_hop) = arp_target else {
            return PendingTxAction::Idle;
        };
        debug_assert!(self.should_request_arp(next_hop, now_ms));
        PendingTxAction::RequestArp(next_hop)
    }

    fn requeue_front(&mut self, packet: PendingTxPacket) {
        self.packets.push_front(packet);
    }

    fn cancel_arp_request(&mut self, next_hop: Ipv4Address) {
        self.last_arp_request_ms.remove(&next_hop);
    }

    fn next_poll_at_ms(&self) -> Option<u64> {
        let retry_at_ms = self
            .last_arp_request_ms
            .values()
            .map(|last_ms| last_ms.saturating_add(ARP_RETRY_INTERVAL_MS))
            .min();
        let expiry_at_ms = self.packets.iter().map(|packet| packet.expires_at_ms).min();

        match (retry_at_ms, expiry_at_ms) {
            (Some(retry), Some(expiry)) => Some(retry.min(expiry)),
            (Some(retry), None) => Some(retry),
            (None, Some(expiry)) => Some(expiry),
            (None, None) => None,
        }
    }

    fn neighbor_resolved(&mut self, next_hop: Ipv4Address) {
        self.last_arp_request_ms.remove(&next_hop);
    }

    fn prune_expired(&mut self, now_ms: u64) {
        self.packets.retain(|packet| packet.expires_at_ms > now_ms);
        let packets = &self.packets;
        self.last_arp_request_ms
            .retain(|next_hop, _| packets.iter().any(|packet| packet.next_hop == *next_hop));
    }

    fn remove_request_if_unused(&mut self, next_hop: Ipv4Address) {
        if !self
            .packets
            .iter()
            .any(|packet| packet.next_hop == next_hop)
        {
            self.last_arp_request_ms.remove(&next_hop);
        }
    }

    #[cfg(ktest)]
    fn len(&self) -> usize {
        self.packets.len()
    }
}

impl<D: WithDevice, E: Ext> EtherIface<D, E> {
    pub fn new(
        driver: D,
        ether_addr: EthernetAddress,
        ip_cidr: Option<Ipv4Cidr>,
        gateway: Option<Ipv4Address>,
        static_arp_entries: &[(Ipv4Address, EthernetAddress)],
        name: CString,
        sched_poll: E::ScheduleNextPoll,
        flags: InterfaceFlags,
    ) -> Arc<Self> {
        let interface = driver.with(|device| {
            let config = Config::new(wire::HardwareAddress::Ethernet(ether_addr));
            let now = get_network_timestamp();

            let mut interface = smoltcp::iface::Interface::new(config, device, now);
            if let Some(ip_cidr) = ip_cidr {
                interface.update_ip_addrs(|ip_addrs| {
                    debug_assert!(ip_addrs.is_empty());
                    ip_addrs.push(wire::IpCidr::Ipv4(ip_cidr)).unwrap();
                });
            }
            if let Some(gateway) = gateway {
                interface
                    .routes_mut()
                    .add_default_ipv4_route(gateway)
                    .unwrap();
            }
            interface
        });

        let common = IfaceCommon::new(name, InterfaceType::ETHER, flags, interface, sched_poll);

        Arc::new(Self {
            driver,
            common,
            ether_addr,
            arp_table: SpinLock::new(static_arp_entries.iter().copied().collect()),
            pending_tx: SpinLock::new(PendingTxState::new()),
        })
    }
}

impl<D, E: Ext> IfaceInternal<E> for EtherIface<D, E> {
    fn common(&self) -> &IfaceCommon<E> {
        &self.common
    }
}

impl<D: WithDevice + 'static, E: Ext> Iface<E> for EtherIface<D, E>
where
    D::Device: NotifyDevice,
{
    fn ethernet_addr(&self) -> Option<EthernetAddress> {
        Some(self.ether_addr)
    }

    fn poll(&self) {
        self.driver.with(|device| {
            let next_poll = self.common.poll(
                &mut *device,
                |data, iface_cx, tx_token| self.process(data, iface_cx, tx_token),
                |pkt, iface_cx, tx_token| self.dispatch(pkt, iface_cx, tx_token),
            );
            // The ingress poll above may have resolved ARP entries; retry the
            // packets that were queued waiting for a resolution.
            let pending_next_poll = self.flush_pending_tx(&mut *device);
            device.notify_poll_end();
            let next_poll = match (next_poll, pending_next_poll) {
                (Some(socket), Some(pending)) => Some(socket.min(pending)),
                (Some(socket), None) => Some(socket),
                (None, Some(pending)) => Some(pending),
                (None, None) => None,
            };
            self.common.sched_poll().schedule_next_poll(next_poll);
        });
    }

    fn mtu(&self) -> usize {
        self.driver
            .with(|device| device.capabilities().max_transmission_unit)
    }
}

impl<D, E: Ext> EtherIface<D, E> {
    fn process<'pkt, T: TxToken>(
        &self,
        data: &'pkt [u8],
        iface_cx: &mut Context,
        tx_token: T,
    ) -> Option<(IpPacket<'pkt>, T)> {
        match self.parse_ip_or_process_arp(data, iface_cx) {
            Ok(pkt) => Some((pkt, tx_token)),
            Err(Some(arp)) => {
                Self::emit_arp(&arp, tx_token);
                None
            }
            Err(None) => None,
        }
    }

    fn parse_ip_or_process_arp<'pkt>(
        &self,
        data: &'pkt [u8],
        iface_cx: &mut Context,
    ) -> Result<IpPacket<'pkt>, Option<ArpRepr>> {
        // Parse the Ethernet header. Ignore the packet if the header is ill-formed.
        let frame = EthernetFrame::new_checked(data).map_err(|_| None)?;
        let repr = EthernetRepr::parse(&frame).map_err(|_| None)?;

        // Ignore the Ethernet frame if it is not sent to us.
        if !repr.dst_addr.is_broadcast() && repr.dst_addr != self.ether_addr {
            return Err(None);
        }

        // Ignore the Ethernet frame if the protocol is not supported.
        match repr.ethertype {
            EthernetProtocol::Ipv4 => {
                let pkt = Ipv4Packet::new_checked(frame.payload()).map_err(|_| None)?;
                Ok(IpPacket::Ipv4(pkt))
            }
            EthernetProtocol::Ipv6 => {
                let pkt = Ipv6Packet::new_checked(frame.payload()).map_err(|_| None)?;
                Ok(IpPacket::Ipv6(pkt))
            }
            EthernetProtocol::Arp => {
                let pkt = ArpPacket::new_checked(frame.payload()).map_err(|_| None)?;
                let arp = ArpRepr::parse(&pkt).map_err(|_| None)?;
                Err(self.process_arp(&arp, iface_cx))
            }
            _ => Err(None),
        }
    }

    fn process_arp(&self, arp_repr: &ArpRepr, iface_cx: &mut Context) -> Option<ArpRepr> {
        match arp_repr {
            ArpRepr::EthernetIpv4 {
                operation: ArpOperation::Reply,
                source_hardware_addr,
                source_protocol_addr,
                ..
            } => {
                // Ignore the ARP packet if the source addresses are not unicast or not local.
                if !source_hardware_addr.is_unicast()
                    || !iface_cx.in_same_network(&IpAddress::Ipv4(*source_protocol_addr))
                {
                    return None;
                }

                // Insert the mapping between the Ethernet address and the IP address.
                //
                // TODO: Remove the mapping if it expires.
                self.arp_table
                    .lock()
                    .insert(*source_protocol_addr, *source_hardware_addr);
                self.pending_tx
                    .lock()
                    .neighbor_resolved(*source_protocol_addr);

                None
            }
            ArpRepr::EthernetIpv4 {
                operation: ArpOperation::Request,
                source_hardware_addr,
                source_protocol_addr,
                target_protocol_addr,
                ..
            } => {
                // Ignore the ARP packet if the source addresses are not unicast.
                if !source_hardware_addr.is_unicast() || !source_protocol_addr.x_is_unicast() {
                    return None;
                }

                // Ignore the ARP packet if we do not own the target address.
                if iface_cx
                    .ipv4_addr()
                    .is_none_or(|addr| addr != *target_protocol_addr)
                {
                    return None;
                }

                Some(ArpRepr::EthernetIpv4 {
                    operation: ArpOperation::Reply,
                    source_hardware_addr: self.ether_addr,
                    source_protocol_addr: *target_protocol_addr,
                    target_hardware_addr: *source_hardware_addr,
                    target_protocol_addr: *source_protocol_addr,
                })
            }
            _ => None,
        }
    }

    fn dispatch<T: TxToken>(&self, pkt: &Packet, iface_cx: &mut Context, tx_token: T) {
        match self.resolve_ether_or_generate_arp(&pkt.ip_repr().dst_addr(), iface_cx) {
            Ok(ether) => Self::emit_ip(&ether, pkt, &iface_cx.caps, tx_token),
            Err(Some(arp)) => {
                let ArpRepr::EthernetIpv4 {
                    target_protocol_addr: next_hop,
                    ..
                } = arp
                else {
                    return;
                };
                if self.enqueue_pending_tx(pkt, iface_cx, next_hop) {
                    Self::emit_arp(&arp, tx_token);
                }
            }
            Err(None) => (),
        }
    }

    /// Queues a serialized copy of an IPv4 packet for deferred transmission.
    fn enqueue_pending_tx(&self, pkt: &Packet, iface_cx: &Context, next_hop: Ipv4Address) -> bool {
        // Only IPv4 packets can be queued; IPv6 packets are dropped by the
        // resolver (no neighbor discovery yet).
        if !matches!(pkt.ip_repr(), IpRepr::Ipv4(_)) {
            return false;
        }

        let bytes = Self::serialize_ip(pkt, &iface_cx.caps);
        let now_ms = iface_cx.now().total_millis() as u64;
        let mut pending = self.pending_tx.lock();
        if !pending.enqueue(bytes, next_hop, now_ms) {
            ostd::warn!("net: dropping packet because the ARP pending queue is full");
            return false;
        }
        pending.should_request_arp(next_hop, now_ms)
    }

    /// Retries transmission of packets queued by [`Self::enqueue_pending_tx`].
    ///
    /// This is called at the end of each interface poll, after the ingress
    /// phase has had a chance to process ARP replies.
    fn flush_pending_tx<D2: Device + ?Sized>(&self, device: &mut D2) -> Option<u64> {
        loop {
            let mut interface = self.common.interface();
            let iface_cx = interface.context_mut();
            let now = iface_cx.now();
            let action = self
                .pending_tx
                .lock()
                .next_action(now.total_millis() as u64, |next_hop| {
                    self.resolve_known_neighbor(next_hop)
                });
            let arp_source_addr = iface_cx.ipv4_addr().unwrap_or(Ipv4Address::UNSPECIFIED);
            drop(interface);

            match action {
                PendingTxAction::Transmit { packet, ether_addr } => {
                    let Some(tx_token) = device.transmit(now) else {
                        self.pending_tx.lock().requeue_front(packet);
                        return self.pending_tx.lock().next_poll_at_ms();
                    };
                    let ether_repr = EthernetRepr {
                        src_addr: self.ether_addr,
                        dst_addr: ether_addr,
                        ethertype: EthernetProtocol::Ipv4,
                    };
                    Self::emit_frame(&ether_repr, &packet.bytes, tx_token);
                }
                PendingTxAction::RequestArp(next_hop) => {
                    let Some(tx_token) = device.transmit(now) else {
                        self.pending_tx.lock().cancel_arp_request(next_hop);
                        return self.pending_tx.lock().next_poll_at_ms();
                    };
                    let arp = ArpRepr::EthernetIpv4 {
                        operation: ArpOperation::Request,
                        source_hardware_addr: self.ether_addr,
                        source_protocol_addr: arp_source_addr,
                        target_hardware_addr: EthernetAddress::BROADCAST,
                        target_protocol_addr: next_hop,
                    };
                    Self::emit_arp(&arp, tx_token);
                }
                PendingTxAction::Idle => return self.pending_tx.lock().next_poll_at_ms(),
            };
        }
    }

    fn resolve_known_neighbor(&self, next_hop: Ipv4Address) -> Option<EthernetAddress> {
        if next_hop.is_broadcast() {
            Some(EthernetAddress::BROADCAST)
        } else {
            self.arp_table.lock().get(&next_hop).copied()
        }
    }

    fn resolve_ether_or_generate_arp(
        &self,
        dst_addr: &IpAddress,
        iface_cx: &mut Context,
    ) -> Result<EthernetRepr, Option<ArpRepr>> {
        // Resolve the next-hop IP address.
        let next_hop_ip = match iface_cx.route(dst_addr, iface_cx.now()) {
            Some(IpAddress::Ipv4(next_hop_ip)) => next_hop_ip,
            Some(IpAddress::Ipv6(_)) => {
                // FIXME: Currently, we drop outbound IPv6 packets because neighbor discovery is not
                // implemented and we have no way to resolve the next-hop link-layer address.
                ostd::debug!("IPv6 neighbor discovery is not implemented for Ethernet interfaces");
                return Err(None);
            }
            None => return Err(None),
        };

        // Resolve the next-hop Ethernet address.
        let next_hop_ether = if next_hop_ip.is_broadcast() {
            EthernetAddress::BROADCAST
        } else if let Some(next_hop_ether) = self.arp_table.lock().get(&next_hop_ip) {
            *next_hop_ether
        } else {
            // If the next-hop Ethernet address cannot be resolved, we drop the original packet and
            // send an ARP packet instead. The upper layer should be responsible for detecting the
            // packet loss and retrying later to see if the Ethernet address is ready.
            return Err(Some(ArpRepr::EthernetIpv4 {
                operation: ArpOperation::Request,
                source_hardware_addr: self.ether_addr,
                source_protocol_addr: iface_cx.ipv4_addr().unwrap_or(Ipv4Address::UNSPECIFIED),
                target_hardware_addr: EthernetAddress::BROADCAST,
                target_protocol_addr: next_hop_ip,
            }));
        };

        Ok(EthernetRepr {
            src_addr: self.ether_addr,
            dst_addr: next_hop_ether,
            ethertype: EthernetProtocol::Ipv4,
        })
    }

    /// Consumes the token and emits an IP packet.
    fn emit_ip<T: TxToken>(
        ether_repr: &EthernetRepr,
        ip_pkt: &Packet,
        caps: &DeviceCapabilities,
        tx_token: T,
    ) {
        let payload = Self::serialize_ip(ip_pkt, caps);
        Self::emit_frame(ether_repr, &payload, tx_token);
    }

    /// Serializes an IP packet into wire bytes (with checksums filled in).
    fn serialize_ip(ip_pkt: &Packet, caps: &DeviceCapabilities) -> Box<[u8]> {
        let ip_repr = ip_pkt.ip_repr();
        let mut data = vec![0; ip_repr.buffer_len()];
        ip_repr.emit(&mut data, &caps.checksum);
        ip_pkt.emit_payload(&ip_repr, &mut data[ip_repr.header_len()..], caps);
        data.into_boxed_slice()
    }

    /// Consumes the token and emits an Ethernet frame with the given payload.
    fn emit_frame<T: TxToken>(ether_repr: &EthernetRepr, payload: &[u8], tx_token: T) {
        tx_token.consume(ether_repr.buffer_len() + payload.len(), |buffer| {
            let mut frame = EthernetFrame::new_unchecked(buffer);
            ether_repr.emit(&mut frame);
            frame.payload_mut().copy_from_slice(payload);
        });
    }

    /// Consumes the token and emits an ARP packet.
    fn emit_arp<T: TxToken>(arp_repr: &ArpRepr, tx_token: T) {
        let ether_repr = match arp_repr {
            ArpRepr::EthernetIpv4 {
                source_hardware_addr,
                target_hardware_addr,
                ..
            } => EthernetRepr {
                src_addr: *source_hardware_addr,
                dst_addr: *target_hardware_addr,
                ethertype: EthernetProtocol::Arp,
            },
            _ => return,
        };

        tx_token.consume(ether_repr.buffer_len() + arp_repr.buffer_len(), |buffer| {
            let mut frame = EthernetFrame::new_unchecked(buffer);
            ether_repr.emit(&mut frame);

            let mut pkt = ArpPacket::new_unchecked(frame.payload_mut());
            arp_repr.emit(&mut pkt);
        });
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::vec;

    use ostd::prelude::*;

    use super::*;

    const UNRESOLVED: Ipv4Address = Ipv4Address::new(10, 0, 0, 2);
    const RESOLVED: Ipv4Address = Ipv4Address::new(10, 0, 0, 3);
    const RESOLVED_ETHER: EthernetAddress = EthernetAddress([2, 0, 0, 0, 0, 3]);

    fn packet(byte: u8) -> Box<[u8]> {
        vec![byte].into_boxed_slice()
    }

    #[ktest]
    fn pending_arp_packets_expire_and_release_capacity() {
        let mut state = PendingTxState::new();
        for byte in 0..MAX_PENDING_TX {
            assert!(state.enqueue(packet(byte as u8), UNRESOLVED, 0));
        }
        assert!(!state.enqueue(packet(0xff), UNRESOLVED, 0));

        assert!(state.enqueue(packet(0xff), UNRESOLVED, PENDING_TX_LIFETIME_MS));
        assert_eq!(state.len(), 1);
    }

    #[ktest]
    fn pending_arp_requests_are_rate_limited_per_neighbor() {
        let mut state = PendingTxState::new();
        assert!(state.enqueue(packet(1), UNRESOLVED, 0));
        assert!(state.should_request_arp(UNRESOLVED, 0));
        assert!(!state.should_request_arp(UNRESOLVED, ARP_RETRY_INTERVAL_MS - 1));
        assert_eq!(state.next_poll_at_ms(), Some(ARP_RETRY_INTERVAL_MS));
        assert!(state.should_request_arp(UNRESOLVED, ARP_RETRY_INTERVAL_MS));
        assert_eq!(state.next_poll_at_ms(), Some(ARP_RETRY_INTERVAL_MS * 2));
        assert!(state.enqueue(packet(2), RESOLVED, 0));
        assert!(state.should_request_arp(RESOLVED, 0));
    }

    #[ktest]
    fn resolved_neighbor_bypasses_unresolved_queue_head() {
        let mut state = PendingTxState::new();
        assert!(state.enqueue(packet(1), UNRESOLVED, 0));
        assert!(state.enqueue(packet(2), RESOLVED, 0));

        let action = state.next_action(0, |next_hop| {
            (next_hop == RESOLVED).then_some(RESOLVED_ETHER)
        });
        let PendingTxAction::Transmit { packet, ether_addr } = action else {
            panic!("resolved packet must bypass an unresolved queue head");
        };
        assert_eq!(&*packet.bytes, &[2]);
        assert_eq!(ether_addr, RESOLVED_ETHER);
        assert_eq!(state.len(), 1);
        assert!(state.should_request_arp(UNRESOLVED, 0));
    }
}
