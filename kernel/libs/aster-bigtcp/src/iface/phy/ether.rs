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
    phy::{ChecksumCapabilities, Device, DeviceCapabilities, TxToken},
    wire::{
        self, ArpOperation, ArpPacket, ArpRepr, EthernetAddress, EthernetFrame, EthernetProtocol,
        EthernetRepr, IpAddress, IpRepr, Ipv4Address, Ipv4AddressExt, Ipv4Cidr, Ipv4Packet,
        Ipv4Repr, Ipv6Packet,
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
    pending_tx: SpinLock<VecDeque<Box<[u8]>>, BottomHalfDisabled>,
}

/// The maximum number of packets queued for ARP resolution.
const MAX_PENDING_TX: usize = 64;

impl<D: WithDevice, E: Ext> EtherIface<D, E> {
    pub fn new(
        driver: D,
        ether_addr: EthernetAddress,
        ip_cidr: Option<Ipv4Cidr>,
        gateway: Option<Ipv4Address>,
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
            arp_table: SpinLock::new(BTreeMap::new()),
            pending_tx: SpinLock::new(VecDeque::new()),
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
            self.flush_pending_tx(&mut *device);
            device.notify_poll_end();
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
                // The next hop is not resolved yet. Instead of dropping the
                // packet, queue it so that it can be sent once the ARP reply
                // arrives (see `flush_pending_tx`).
                self.enqueue_pending_tx(pkt, iface_cx);
                Self::emit_arp(&arp, tx_token);
            }
            Err(None) => (),
        }
    }

    /// Queues a serialized copy of an IPv4 packet for deferred transmission.
    fn enqueue_pending_tx(&self, pkt: &Packet, iface_cx: &Context) {
        // Only IPv4 packets can be queued; IPv6 packets are dropped by the
        // resolver (no neighbor discovery yet).
        if !matches!(pkt.ip_repr(), IpRepr::Ipv4(_)) {
            return;
        }

        let mut pending = self.pending_tx.lock();
        if pending.len() >= MAX_PENDING_TX {
            ostd::warn!("net: dropping packet because the ARP pending queue is full");
            return;
        }
        pending.push_back(Self::serialize_ip(pkt, &iface_cx.caps));
    }

    /// Retries transmission of packets queued by [`Self::enqueue_pending_tx`].
    ///
    /// This is called at the end of each interface poll, after the ingress
    /// phase has had a chance to process ARP replies.
    fn flush_pending_tx<D2: Device + ?Sized>(&self, device: &mut D2) {
        loop {
            let Some(pkt_bytes) = self.pending_tx.lock().front().cloned() else {
                return;
            };

            let Ok(pkt) = Ipv4Packet::new_checked(&pkt_bytes[..]) else {
                self.pending_tx.lock().pop_front();
                continue;
            };
            let Ok(ip_repr) = Ipv4Repr::parse(&pkt, &ChecksumCapabilities::ignored()) else {
                self.pending_tx.lock().pop_front();
                continue;
            };

            let mut interface = self.common.interface();
            let iface_cx = interface.context_mut();

            let Some(tx_token) = device.transmit(iface_cx.now()) else {
                return;
            };

            match self.resolve_ether_or_generate_arp(&IpAddress::Ipv4(ip_repr.dst_addr), iface_cx) {
                Ok(ether) => {
                    Self::emit_frame(&ether, &pkt_bytes, tx_token);
                    self.pending_tx.lock().pop_front();
                }
                Err(Some(arp)) => {
                    // Still unresolved. Re-send the ARP request and wait for
                    // the reply to trigger another poll.
                    Self::emit_arp(&arp, tx_token);
                    return;
                }
                Err(None) => {
                    // The packet is unroutable; drop it.
                    self.pending_tx.lock().pop_front();
                }
            }
            drop(interface);
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
