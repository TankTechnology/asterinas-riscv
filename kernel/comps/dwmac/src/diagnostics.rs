// SPDX-License-Identifier: MPL-2.0

//! Bounded receive-path diagnostics for Megrez DWMAC bring-up.

const ETHERNET_HEADER_LEN: usize = 14;
const ETHERNET_PROTOCOL_ARP: u16 = 0x0806;
const ETHERNET_PROTOCOL_IPV4: u16 = 0x0800;
const IPV4_MIN_HEADER_LEN: usize = 20;
const IPV4_PROTOCOL_TCP: u8 = 6;
const TCP_MIN_HEADER_LEN: usize = 20;
const TCP_FLAG_ACK: u8 = 0x10;
const TCP_FLAG_SYN: u8 = 0x02;
const TCP_FLAGS_SYN_ACK: u8 = TCP_FLAG_SYN | TCP_FLAG_ACK;

/// The maximum number of frames whose headers are sampled after boot.
pub(super) const FRAME_SAMPLE_LIMIT: u64 = 512;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RxFrameClass {
    Arp,
    Ipv4Other,
    Malformed,
    Other,
    TcpOther,
    TcpSyn,
    TcpSynAck,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum RxDescriptorDrop {
    Fragmented,
    FrameTooLong,
    Other,
    ReceiveError,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(super) struct RxDiagnosticsReport {
    pub(super) observed: u64,
    pub(super) arp: u64,
    pub(super) ipv4_other: u64,
    pub(super) tcp_syn: u64,
    pub(super) tcp_syn_ack: u64,
    pub(super) tcp_other: u64,
    pub(super) other: u64,
    pub(super) malformed: u64,
    pub(super) descriptor_fragmented: u64,
    pub(super) descriptor_receive_error: u64,
    pub(super) descriptor_frame_too_long: u64,
    pub(super) descriptor_other: u64,
}

#[derive(Debug, Default)]
pub(super) struct RxDiagnostics(RxDiagnosticsReport);

impl RxDiagnostics {
    pub(super) fn can_sample_frame(&self) -> bool {
        self.0.observed < FRAME_SAMPLE_LIMIT
    }

    pub(super) fn record_frame(&mut self, frame: &[u8]) {
        if !self.can_sample_frame() {
            return;
        }

        self.0.observed = self.0.observed.saturating_add(1);
        let counter = match classify_frame(frame) {
            RxFrameClass::Arp => &mut self.0.arp,
            RxFrameClass::Ipv4Other => &mut self.0.ipv4_other,
            RxFrameClass::Malformed => &mut self.0.malformed,
            RxFrameClass::Other => &mut self.0.other,
            RxFrameClass::TcpOther => &mut self.0.tcp_other,
            RxFrameClass::TcpSyn => &mut self.0.tcp_syn,
            RxFrameClass::TcpSynAck => &mut self.0.tcp_syn_ack,
        };
        *counter = counter.saturating_add(1);
    }

    pub(super) fn record_descriptor_drop(&mut self, drop: RxDescriptorDrop) {
        let counter = match drop {
            RxDescriptorDrop::Fragmented => &mut self.0.descriptor_fragmented,
            RxDescriptorDrop::FrameTooLong => &mut self.0.descriptor_frame_too_long,
            RxDescriptorDrop::Other => &mut self.0.descriptor_other,
            RxDescriptorDrop::ReceiveError => &mut self.0.descriptor_receive_error,
        };
        *counter = counter.saturating_add(1);
    }

    pub(super) const fn report(&self) -> RxDiagnosticsReport {
        self.0
    }
}

fn classify_frame(frame: &[u8]) -> RxFrameClass {
    let Some(ethernet_header) = frame.get(..ETHERNET_HEADER_LEN) else {
        return RxFrameClass::Malformed;
    };
    let ethernet_protocol = u16::from_be_bytes([ethernet_header[12], ethernet_header[13]]);
    match ethernet_protocol {
        ETHERNET_PROTOCOL_ARP => RxFrameClass::Arp,
        ETHERNET_PROTOCOL_IPV4 => classify_ipv4(&frame[ETHERNET_HEADER_LEN..]),
        _ => RxFrameClass::Other,
    }
}

fn classify_ipv4(packet: &[u8]) -> RxFrameClass {
    let Some(minimum_header) = packet.get(..IPV4_MIN_HEADER_LEN) else {
        return RxFrameClass::Malformed;
    };
    let version = minimum_header[0] >> 4;
    let header_len = usize::from(minimum_header[0] & 0x0f) * 4;
    let total_len = usize::from(u16::from_be_bytes([minimum_header[2], minimum_header[3]]));
    if version != 4
        || header_len < IPV4_MIN_HEADER_LEN
        || total_len < header_len
        || packet.get(..total_len).is_none()
    {
        return RxFrameClass::Malformed;
    }
    if minimum_header[9] != IPV4_PROTOCOL_TCP {
        return RxFrameClass::Ipv4Other;
    }

    let Some(tcp_header) = packet.get(header_len..header_len + TCP_MIN_HEADER_LEN) else {
        return RxFrameClass::Malformed;
    };
    match tcp_header[13] & (TCP_FLAG_SYN | TCP_FLAG_ACK) {
        TCP_FLAG_SYN => RxFrameClass::TcpSyn,
        TCP_FLAGS_SYN_ACK => RxFrameClass::TcpSynAck,
        _ => RxFrameClass::TcpOther,
    }
}
