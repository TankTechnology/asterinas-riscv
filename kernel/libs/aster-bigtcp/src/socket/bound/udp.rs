// SPDX-License-Identifier: MPL-2.0

use alloc::{boxed::Box, sync::Arc};
use core::{
    marker::PhantomData,
    sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering},
};

use aster_softirq::BottomHalfDisabled;
use ostd::{sync::SpinLock, timer::Jiffies};
use smoltcp::{
    iface::Context,
    socket::udp::UdpMetadata,
    wire::{IpAddress, IpListenEndpoint, IpRepr, Ipv4Address, UdpRepr},
};

use super::{
    ReceiveBehavior,
    common::{Inner, Socket, SocketBg},
};
use crate::{
    errors::udp::SendError,
    ext::Ext,
    iface::BoundUdpPort,
    socket::{RawUdpSocket, event::SocketEvents, unbound::new_udp_socket},
};

pub type UdpSocket<E> = Socket<UdpSocketInner<E>, E>;

// Diagnostic only: counts payload-bearing datagrams rejected by the RX queue.
static UDP_RX_QUEUE_DROPS: AtomicU64 = AtomicU64::new(0);
const RPC_ARRIVAL_SLOTS: usize = 64;
const RPC_TRACE_SLOTS: usize = 1024;
const RPC_REQUEST_ENQUEUE: u8 = 1;
const RPC_REQUEST_DISPATCH: u8 = 2;
const RPC_REQUEST_ARRIVE: u8 = 3;
const RPC_REQUEST_READ: u8 = 4;
const RPC_REPLY_ENQUEUE: u8 = 5;
const RPC_REPLY_DISPATCH: u8 = 6;
const RPC_REPLY_ARRIVE: u8 = 7;
const RPC_REPLY_READ: u8 = 8;
const RPC_REQUEST_NOTIFY_BEGIN: u8 = 9;
const RPC_REPLY_NOTIFY_BEGIN: u8 = 10;
const RPC_REQUEST_NOTIFY_END: u8 = 11;
const RPC_REPLY_NOTIFY_END: u8 = 12;

struct RpcTraceSlot {
    seq: AtomicU64,
    ns: AtomicU64,
    wakes: AtomicU64,
    packed: AtomicU64,
}

impl RpcTraceSlot {
    const fn new() -> Self {
        Self {
            seq: AtomicU64::new(0),
            ns: AtomicU64::new(0),
            wakes: AtomicU64::new(0),
            packed: AtomicU64::new(0),
        }
    }
}

static RPC_TRACE: [RpcTraceSlot; RPC_TRACE_SLOTS] =
    [const { RpcTraceSlot::new() }; RPC_TRACE_SLOTS];
static RPC_TRACE_NEXT: AtomicU64 = AtomicU64::new(0);
static FIRST_RETRY_XID: AtomicU32 = AtomicU32::new(0);
static FIRST_TRACE_DUMPED: AtomicBool = AtomicBool::new(false);
static SLOWEST_RTT_NS: AtomicU64 = AtomicU64::new(0);
static SLOWEST_TRACE: SpinLock<SlowTrace, BottomHalfDisabled> = SpinLock::new(SlowTrace::new());

#[derive(Clone, Copy)]
struct TraceEvent {
    seq: u64,
    stage: u8,
    port: u16,
    ns: u64,
    wakes: u64,
}

impl TraceEvent {
    const EMPTY: Self = Self {
        seq: 0,
        stage: 0,
        port: 0,
        ns: 0,
        wakes: 0,
    };
}

#[derive(Clone, Copy)]
struct SlowTrace {
    xid: u32,
    rtt_ns: u64,
    events: [TraceEvent; 16],
    len: usize,
}

impl SlowTrace {
    const fn new() -> Self {
        Self {
            xid: 0,
            rtt_ns: 0,
            events: [TraceEvent::EMPTY; 16],
            len: 0,
        }
    }
}

fn monotonic_ns() -> u64 {
    u64::try_from(aster_time::read_monotonic_time().as_nanos()).unwrap_or(u64::MAX)
}

fn trace_rpc(stage: u8, port: u16, xid: u32) {
    let seq = RPC_TRACE_NEXT.fetch_add(1, Ordering::Relaxed) + 1;
    let slot = &RPC_TRACE[seq as usize % RPC_TRACE_SLOTS];
    slot.seq.store(0, Ordering::Release);
    slot.ns.store(monotonic_ns(), Ordering::Relaxed);
    slot.wakes
        .store(ostd::sync::successful_wakeups(), Ordering::Relaxed);
    slot.packed.store(
        ((stage as u64) << 48) | ((port as u64) << 32) | xid as u64,
        Ordering::Relaxed,
    );
    slot.seq.store(seq, Ordering::Release);
}

fn capture_slowest_trace(xid: u32, rtt_ns: u64) {
    if rtt_ns <= SLOWEST_RTT_NS.fetch_max(rtt_ns, Ordering::Relaxed) {
        return;
    }
    let mut state = SLOWEST_TRACE.lock();
    if rtt_ns <= state.rtt_ns {
        return;
    }
    state.xid = xid;
    state.rtt_ns = rtt_ns;
    state.len = 0;
    let end = RPC_TRACE_NEXT.load(Ordering::Acquire);
    let begin = end.saturating_sub(RPC_TRACE_SLOTS as u64 - 1).max(1);
    for seq in begin..=end {
        let slot = &RPC_TRACE[seq as usize % RPC_TRACE_SLOTS];
        if slot.seq.load(Ordering::Acquire) != seq {
            continue;
        }
        let ns = slot.ns.load(Ordering::Relaxed);
        let wakes = slot.wakes.load(Ordering::Relaxed);
        let packed = slot.packed.load(Ordering::Relaxed);
        if slot.seq.load(Ordering::Acquire) == seq && packed as u32 == xid {
            if state.len == state.events.len() {
                break;
            }
            let index = state.len;
            state.events[index] = TraceEvent {
                seq,
                stage: (packed >> 48) as u8,
                port: (packed >> 32) as u16,
                ns,
                wakes,
            };
            state.len += 1;
        }
    }
}

fn dump_slowest_trace() {
    let state = *SLOWEST_TRACE.lock();
    ostd::early_println!(
        "UDP_RPC_SLOWEST_TRACE xid={} rtt_ns={} events={}",
        state.xid,
        state.rtt_ns,
        state.len,
    );
    for event in &state.events[..state.len] {
        ostd::early_println!(
            "UDP_RPC_SLOWEST_EVENT seq={} stage={} port={} xid={} ns={} wakes={}",
            event.seq,
            event.stage,
            event.port,
            state.xid,
            event.ns,
            event.wakes,
        );
    }
}

fn dump_first_retry_trace(xid: u32) {
    if FIRST_TRACE_DUMPED.swap(true, Ordering::Relaxed) {
        return;
    }
    let end = RPC_TRACE_NEXT.load(Ordering::Acquire);
    let begin = end.saturating_sub(RPC_TRACE_SLOTS as u64 - 1).max(1);
    ostd::early_println!(
        "UDP_RPC_FIRST_TRACE xid={} begin={} end={}",
        xid,
        begin,
        end
    );
    for seq in begin..=end {
        let slot = &RPC_TRACE[seq as usize % RPC_TRACE_SLOTS];
        if slot.seq.load(Ordering::Acquire) != seq {
            continue;
        }
        let ns = slot.ns.load(Ordering::Relaxed);
        let wakes = slot.wakes.load(Ordering::Relaxed);
        let packed = slot.packed.load(Ordering::Relaxed);
        if slot.seq.load(Ordering::Acquire) == seq && packed as u32 == xid {
            ostd::early_println!(
                "UDP_RPC_FIRST_EVENT seq={} stage={} port={} xid={} ns={} wakes={}",
                seq,
                packed >> 48,
                (packed >> 32) as u16,
                xid,
                ns,
                wakes,
            );
        }
    }
}

fn rpc_xid(data: &[u8]) -> u32 {
    if data.len() < 4 {
        return 0;
    }
    u32::from_be_bytes([data[0], data[1], data[2], data[3]])
}

fn rpc_message_type(data: &[u8]) -> Option<u32> {
    (data.len() >= 8).then(|| u32::from_be_bytes([data[4], data[5], data[6], data[7]]))
}

/// States needed by [`UdpSocketBg`].
pub struct UdpSocketInner<E: Ext> {
    socket: SpinLock<Box<RawUdpSocket>, BottomHalfDisabled>,
    need_dispatch: AtomicBool,
    rx_packets: AtomicU64,
    tx_packets: AtomicU64,
    rx_drops: AtomicU64,
    rx_max_queued_bytes: AtomicU64,
    last_tx_xid: AtomicU32,
    last_rx_xid: AtomicU32,
    last_read_xid: AtomicU32,
    rx_xid_mismatches: AtomicU64,
    read_xid_mismatches: AtomicU64,
    arrival_xids: [AtomicU32; RPC_ARRIVAL_SLOTS],
    arrival_ms: [AtomicU64; RPC_ARRIVAL_SLOTS],
    rx_read_delay_samples: AtomicU64,
    rx_read_delay_max_ms: AtomicU64,
    rx_read_delay_ge_2ms: AtomicU64,
    rx_read_delay_ge_5ms: AtomicU64,
    rx_read_delay_ge_25ms: AtomicU64,
    send_enqueue_xids: [AtomicU32; RPC_ARRIVAL_SLOTS],
    send_enqueue_ms: [AtomicU64; RPC_ARRIVAL_SLOTS],
    enqueue_dispatch_samples: AtomicU64,
    enqueue_dispatch_max_ms: AtomicU64,
    enqueue_dispatch_ge_2ms: AtomicU64,
    enqueue_dispatch_ge_5ms: AtomicU64,
    first_tx_xids: [AtomicU32; RPC_ARRIVAL_SLOTS],
    first_tx_ms: [AtomicU64; RPC_ARRIVAL_SLOTS],
    first_tx_ns: [AtomicU64; RPC_ARRIVAL_SLOTS],
    request_retransmits: AtomicU64,
    request_reply_samples: AtomicU64,
    request_reply_max_ms: AtomicU64,
    request_reply_ge_2ms: AtomicU64,
    request_reply_ge_5ms: AtomicU64,
    request_reply_ge_25ms: AtomicU64,
    request_read_xids: [AtomicU32; RPC_ARRIVAL_SLOTS],
    request_read_ms: [AtomicU64; RPC_ARRIVAL_SLOTS],
    server_processing_samples: AtomicU64,
    server_processing_max_ms: AtomicU64,
    server_processing_ge_2ms: AtomicU64,
    server_processing_ge_5ms: AtomicU64,
    server_processing_ge_25ms: AtomicU64,
    accepts_ipv4: bool,
    ext: PhantomData<fn() -> E>,
}

impl<E: Ext> Inner<E> for UdpSocketInner<E> {
    type BoundPort = BoundUdpPort<E>;
    type Observer = E::UdpEventObserver;

    fn on_drop(this: &Arc<SocketBg<Self, E>>) {
        let (queued_on_close, queued_xid) = {
            let mut socket = this.inner.socket.lock();
            let queued = socket.recv_queue();
            let queued_xid = socket.peek().ok().map(|(data, _)| rpc_xid(data));
            socket.close();
            (queued, queued_xid)
        };

        // Keep the socket-table -> UDP-registry lock order used by poll.
        // Once removed locally, a concurrent registry lookup can at most see a
        // closed socket through its weak reference.
        this.bound.iface().common().remove_udp_socket(this);

        this.bound
            .iface()
            .common()
            .udp_registry()
            .unregister_socket(this);

        let rx = this.inner.rx_packets.load(Ordering::Relaxed);
        let tx = this.inner.tx_packets.load(Ordering::Relaxed);
        let drops = this.inner.rx_drops.load(Ordering::Relaxed);
        let max_queued = this.inner.rx_max_queued_bytes.load(Ordering::Relaxed);
        if rx + tx >= 1000 {
            ostd::early_println!(
                "UDP_SOCKET_STATS port={} rx={} tx={} rx_drops={} max_queued={} queued_on_close={} last_tx_xid={} last_rx_xid={} last_read_xid={} queued_xid={:?} rx_xid_mismatches={} read_xid_mismatches={} rx_read_delay_samples={} rx_read_delay_max_ms={} rx_read_delay_ge_25ms={}",
                this.bound.port(),
                rx,
                tx,
                drops,
                max_queued,
                queued_on_close,
                this.inner.last_tx_xid.load(Ordering::Relaxed),
                this.inner.last_rx_xid.load(Ordering::Relaxed),
                this.inner.last_read_xid.load(Ordering::Relaxed),
                queued_xid,
                this.inner.rx_xid_mismatches.load(Ordering::Relaxed),
                this.inner.read_xid_mismatches.load(Ordering::Relaxed),
                this.inner.rx_read_delay_samples.load(Ordering::Relaxed),
                this.inner.rx_read_delay_max_ms.load(Ordering::Relaxed),
                this.inner.rx_read_delay_ge_25ms.load(Ordering::Relaxed),
            );
            ostd::early_println!(
                "UDP_RPC_TIMING port={} request_retransmits={} request_reply_samples={} request_reply_max_ms={} request_reply_ge_25ms={} server_processing_samples={} server_processing_max_ms={} server_processing_ge_25ms={}",
                this.bound.port(),
                this.inner.request_retransmits.load(Ordering::Relaxed),
                this.inner.request_reply_samples.load(Ordering::Relaxed),
                this.inner.request_reply_max_ms.load(Ordering::Relaxed),
                this.inner.request_reply_ge_25ms.load(Ordering::Relaxed),
                this.inner.server_processing_samples.load(Ordering::Relaxed),
                this.inner.server_processing_max_ms.load(Ordering::Relaxed),
                this.inner.server_processing_ge_25ms.load(Ordering::Relaxed),
            );
            ostd::early_println!(
                "UDP_RPC_SHORT_TAIL port={} enqueue_dispatch_samples={} enqueue_dispatch_max_ms={} enqueue_dispatch_ge_2ms={} enqueue_dispatch_ge_5ms={} rx_read_ge_2ms={} rx_read_ge_5ms={} request_reply_ge_2ms={} request_reply_ge_5ms={} server_processing_ge_2ms={} server_processing_ge_5ms={}",
                this.bound.port(),
                this.inner.enqueue_dispatch_samples.load(Ordering::Relaxed),
                this.inner.enqueue_dispatch_max_ms.load(Ordering::Relaxed),
                this.inner.enqueue_dispatch_ge_2ms.load(Ordering::Relaxed),
                this.inner.enqueue_dispatch_ge_5ms.load(Ordering::Relaxed),
                this.inner.rx_read_delay_ge_2ms.load(Ordering::Relaxed),
                this.inner.rx_read_delay_ge_5ms.load(Ordering::Relaxed),
                this.inner.request_reply_ge_2ms.load(Ordering::Relaxed),
                this.inner.request_reply_ge_5ms.load(Ordering::Relaxed),
                this.inner.server_processing_ge_2ms.load(Ordering::Relaxed),
                this.inner.server_processing_ge_5ms.load(Ordering::Relaxed),
            );
            if this.inner.request_reply_samples.load(Ordering::Relaxed) > 1000 {
                dump_slowest_trace();
            }
        }
    }
}

pub(crate) type UdpSocketBg<E> = SocketBg<UdpSocketInner<E>, E>;

impl<E: Ext> UdpSocketBg<E> {
    pub(crate) fn local_port(&self) -> u16 {
        self.bound.port()
    }

    /// Tries to process an incoming packet and returns whether the packet is processed.
    pub(crate) fn process(
        &self,
        cx: &mut Context,
        ip_repr: &IpRepr,
        udp_repr: &UdpRepr,
        udp_payload: &[u8],
    ) -> bool {
        let local_addr = self.bound.endpoint().addr;
        if !matches!(
            (local_addr, ip_repr.dst_addr()),
            (IpAddress::Ipv4(_), IpAddress::Ipv4(_)) | (IpAddress::Ipv6(_), IpAddress::Ipv6(_))
        ) {
            return false;
        }

        let mut socket = self.inner.socket.lock();

        if !socket.accepts(cx, ip_repr, udp_repr) {
            return false;
        }

        let queued_before = socket.recv_queue();
        socket.process(
            cx,
            smoltcp::phy::PacketMeta::default(),
            ip_repr,
            udp_repr,
            udp_payload,
        );
        let queued_after = socket.recv_queue();
        self.inner
            .rx_max_queued_bytes
            .fetch_max(queued_after as u64, Ordering::Relaxed);
        let rejected = !udp_payload.is_empty() && queued_after == queued_before;
        if !rejected && udp_payload.len() >= 4 {
            let xid = rpc_xid(udp_payload);
            match rpc_message_type(udp_payload) {
                Some(0) => trace_rpc(RPC_REQUEST_ARRIVE, self.bound.port(), xid),
                Some(1) => trace_rpc(RPC_REPLY_ARRIVE, self.bound.port(), xid),
                _ => {}
            }
            let slot = xid as usize % RPC_ARRIVAL_SLOTS;
            let now_ms = Jiffies::elapsed().as_u64();
            self.inner.arrival_ms[slot].store(now_ms, Ordering::Relaxed);
            self.inner.arrival_xids[slot].store(xid, Ordering::Relaxed);
            if rpc_message_type(udp_payload) == Some(1)
                && self.inner.first_tx_xids[slot].load(Ordering::Relaxed) == xid
            {
                let first_tx_ms = self.inner.first_tx_ms[slot].load(Ordering::Relaxed);
                let delay_ms = now_ms.saturating_sub(first_tx_ms);
                self.inner
                    .request_reply_samples
                    .fetch_add(1, Ordering::Relaxed);
                self.inner
                    .request_reply_max_ms
                    .fetch_max(delay_ms, Ordering::Relaxed);
                if delay_ms >= 2 {
                    self.inner
                        .request_reply_ge_2ms
                        .fetch_add(1, Ordering::Relaxed);
                }
                if delay_ms >= 5 {
                    self.inner
                        .request_reply_ge_5ms
                        .fetch_add(1, Ordering::Relaxed);
                }
                if delay_ms >= 25 {
                    self.inner
                        .request_reply_ge_25ms
                        .fetch_add(1, Ordering::Relaxed);
                }
            }
        }
        drop(socket);

        if rejected {
            self.inner.rx_drops.fetch_add(1, Ordering::Relaxed);
            let drops = UDP_RX_QUEUE_DROPS.fetch_add(1, Ordering::Relaxed) + 1;
            if drops.is_power_of_two() {
                ostd::early_println!(
                    "UDP_RX_QUEUE_DROP count={} port={} src_port={} payload={} queued_bytes={}",
                    drops,
                    udp_repr.dst_port,
                    udp_repr.src_port,
                    udp_payload.len(),
                    queued_after,
                );
            }
        } else {
            self.inner.rx_packets.fetch_add(1, Ordering::Relaxed);
            if udp_payload.len() >= 4 {
                let xid = rpc_xid(udp_payload);
                self.inner.last_rx_xid.store(xid, Ordering::Relaxed);
                if xid != self.inner.last_tx_xid.load(Ordering::Relaxed) {
                    self.inner.rx_xid_mismatches.fetch_add(1, Ordering::Relaxed);
                }
            }
        }

        if !rejected {
            match rpc_message_type(udp_payload) {
                Some(0) => trace_rpc(
                    RPC_REQUEST_NOTIFY_BEGIN,
                    self.bound.port(),
                    rpc_xid(udp_payload),
                ),
                Some(1) => trace_rpc(
                    RPC_REPLY_NOTIFY_BEGIN,
                    self.bound.port(),
                    rpc_xid(udp_payload),
                ),
                _ => {}
            }
        }
        self.notify_events(SocketEvents::CAN_RECV);
        if !rejected {
            match rpc_message_type(udp_payload) {
                Some(0) => trace_rpc(
                    RPC_REQUEST_NOTIFY_END,
                    self.bound.port(),
                    rpc_xid(udp_payload),
                ),
                Some(1) => trace_rpc(
                    RPC_REPLY_NOTIFY_END,
                    self.bound.port(),
                    rpc_xid(udp_payload),
                ),
                _ => {}
            }
        }

        true
    }

    /// Tries to generate an outgoing packet and dispatches the generated packet.
    pub(crate) fn dispatch<D>(&self, cx: &mut Context, dispatch: D)
    where
        D: FnOnce(&mut Context, &IpRepr, &UdpRepr, &[u8]),
    {
        let mut socket = self.inner.socket.lock();

        socket
            .dispatch(cx, |cx, _meta, (ip_repr, udp_repr, udp_payload)| {
                if udp_payload.len() >= 4 {
                    let xid = rpc_xid(udp_payload);
                    self.inner.last_tx_xid.store(xid, Ordering::Relaxed);
                    let slot = xid as usize % RPC_ARRIVAL_SLOTS;
                    let now_ms = Jiffies::elapsed().as_u64();
                    match rpc_message_type(udp_payload) {
                        Some(0) => {
                            trace_rpc(RPC_REQUEST_DISPATCH, self.bound.port(), xid);
                        }
                        Some(1) => {
                            trace_rpc(RPC_REPLY_DISPATCH, self.bound.port(), xid);
                        }
                        _ => {}
                    }
                    if rpc_message_type(udp_payload).is_some()
                        && self.inner.send_enqueue_xids[slot].load(Ordering::Relaxed) == xid
                    {
                        let enqueue_ms = self.inner.send_enqueue_ms[slot].load(Ordering::Relaxed);
                        let delay_ms = now_ms.saturating_sub(enqueue_ms);
                        self.inner
                            .enqueue_dispatch_samples
                            .fetch_add(1, Ordering::Relaxed);
                        self.inner
                            .enqueue_dispatch_max_ms
                            .fetch_max(delay_ms, Ordering::Relaxed);
                        if delay_ms >= 2 {
                            self.inner
                                .enqueue_dispatch_ge_2ms
                                .fetch_add(1, Ordering::Relaxed);
                        }
                        if delay_ms >= 5 {
                            self.inner
                                .enqueue_dispatch_ge_5ms
                                .fetch_add(1, Ordering::Relaxed);
                        }
                    }
                    match rpc_message_type(udp_payload) {
                        Some(0) => {
                            if self.inner.first_tx_xids[slot].load(Ordering::Relaxed) == xid {
                                if udp_repr.dst_port != 111 {
                                    let _ = FIRST_RETRY_XID.compare_exchange(
                                        0,
                                        xid,
                                        Ordering::Relaxed,
                                        Ordering::Relaxed,
                                    );
                                }
                                self.inner
                                    .request_retransmits
                                    .fetch_add(1, Ordering::Relaxed);
                            } else {
                                self.inner.first_tx_ms[slot].store(now_ms, Ordering::Relaxed);
                                self.inner.first_tx_ns[slot]
                                    .store(monotonic_ns(), Ordering::Relaxed);
                                self.inner.first_tx_xids[slot].store(xid, Ordering::Relaxed);
                            }
                        }
                        Some(1)
                            if self.inner.request_read_xids[slot].load(Ordering::Relaxed)
                                == xid =>
                        {
                            let read_ms = self.inner.request_read_ms[slot].load(Ordering::Relaxed);
                            let delay_ms = now_ms.saturating_sub(read_ms);
                            self.inner
                                .server_processing_samples
                                .fetch_add(1, Ordering::Relaxed);
                            self.inner
                                .server_processing_max_ms
                                .fetch_max(delay_ms, Ordering::Relaxed);
                            if delay_ms >= 2 {
                                self.inner
                                    .server_processing_ge_2ms
                                    .fetch_add(1, Ordering::Relaxed);
                            }
                            if delay_ms >= 5 {
                                self.inner
                                    .server_processing_ge_5ms
                                    .fetch_add(1, Ordering::Relaxed);
                            }
                            if delay_ms >= 25 {
                                self.inner
                                    .server_processing_ge_25ms
                                    .fetch_add(1, Ordering::Relaxed);
                            }
                        }
                        _ => {}
                    }
                }
                dispatch(cx, &ip_repr, &udp_repr, udp_payload);
                self.inner.tx_packets.fetch_add(1, Ordering::Relaxed);
                Ok::<(), ()>(())
            })
            .unwrap();

        // For UDP, dequeuing a packet means that we can queue more packets.
        self.notify_events(SocketEvents::CAN_SEND);

        self.inner
            .need_dispatch
            .store(socket.send_queue() > 0, Ordering::Relaxed);
    }

    /// Returns whether the socket _may_ generate an outgoing packet.
    ///
    /// The check is intended to be lock-free and fast, but may have false positives.
    pub(crate) fn need_dispatch(&self) -> bool {
        self.inner.need_dispatch.load(Ordering::Relaxed)
    }

    pub(crate) const fn accepts_ipv4(&self) -> bool {
        self.inner.accepts_ipv4
    }
}

impl<E: Ext> UdpSocket<E> {
    /// Binds to a specified endpoint.
    ///
    /// Polling the iface is _not_ required after this method succeeds.
    pub fn new_bind(
        bound: BoundUdpPort<E>,
        observer: E::UdpEventObserver,
        v6only: bool,
    ) -> Result<Self, (BoundUdpPort<E>, smoltcp::socket::udp::BindError)> {
        let local_endpoint = bound.endpoint();
        let accepts_ipv4 = matches!(
            local_endpoint.addr,
            IpAddress::Ipv6(addr) if addr.is_unspecified()
        ) && !v6only;

        let socket = {
            let mut socket = new_udp_socket();

            // Linux wildcard addresses accept packets for every local address
            // in their family. Represent both 0.0.0.0 and :: with smoltcp's
            // wildcard (`None`); `accepts_ipv4` separately controls whether
            // an IPv6 wildcard also receives translated IPv4 packets.
            let bind_endpoint = IpListenEndpoint {
                addr: if local_endpoint.addr.is_unspecified() {
                    None
                } else {
                    Some(local_endpoint.addr)
                },
                port: local_endpoint.port,
            };
            if let Err(err) = socket.bind(bind_endpoint) {
                return Err((bound, err));
            }

            socket
        };

        let inner = UdpSocketInner {
            socket: SpinLock::new(socket),
            need_dispatch: AtomicBool::new(false),
            rx_packets: AtomicU64::new(0),
            tx_packets: AtomicU64::new(0),
            rx_drops: AtomicU64::new(0),
            rx_max_queued_bytes: AtomicU64::new(0),
            last_tx_xid: AtomicU32::new(0),
            last_rx_xid: AtomicU32::new(0),
            last_read_xid: AtomicU32::new(0),
            rx_xid_mismatches: AtomicU64::new(0),
            read_xid_mismatches: AtomicU64::new(0),
            arrival_xids: core::array::from_fn(|_| AtomicU32::new(0)),
            arrival_ms: core::array::from_fn(|_| AtomicU64::new(0)),
            rx_read_delay_samples: AtomicU64::new(0),
            rx_read_delay_max_ms: AtomicU64::new(0),
            rx_read_delay_ge_2ms: AtomicU64::new(0),
            rx_read_delay_ge_5ms: AtomicU64::new(0),
            rx_read_delay_ge_25ms: AtomicU64::new(0),
            send_enqueue_xids: core::array::from_fn(|_| AtomicU32::new(0)),
            send_enqueue_ms: core::array::from_fn(|_| AtomicU64::new(0)),
            enqueue_dispatch_samples: AtomicU64::new(0),
            enqueue_dispatch_max_ms: AtomicU64::new(0),
            enqueue_dispatch_ge_2ms: AtomicU64::new(0),
            enqueue_dispatch_ge_5ms: AtomicU64::new(0),
            first_tx_xids: core::array::from_fn(|_| AtomicU32::new(0)),
            first_tx_ms: core::array::from_fn(|_| AtomicU64::new(0)),
            first_tx_ns: core::array::from_fn(|_| AtomicU64::new(0)),
            request_retransmits: AtomicU64::new(0),
            request_reply_samples: AtomicU64::new(0),
            request_reply_max_ms: AtomicU64::new(0),
            request_reply_ge_2ms: AtomicU64::new(0),
            request_reply_ge_5ms: AtomicU64::new(0),
            request_reply_ge_25ms: AtomicU64::new(0),
            request_read_xids: core::array::from_fn(|_| AtomicU32::new(0)),
            request_read_ms: core::array::from_fn(|_| AtomicU64::new(0)),
            server_processing_samples: AtomicU64::new(0),
            server_processing_max_ms: AtomicU64::new(0),
            server_processing_ge_2ms: AtomicU64::new(0),
            server_processing_ge_5ms: AtomicU64::new(0),
            server_processing_ge_25ms: AtomicU64::new(0),
            accepts_ipv4,
            ext: PhantomData,
        };

        let socket = Self::new(bound, inner);
        socket.init_observer(observer);
        socket
            .iface()
            .common()
            .register_udp_socket(socket.inner().clone());
        socket
            .iface()
            .common()
            .udp_registry()
            .register_socket(socket.inner());

        Ok(socket)
    }

    /// Sends some data.
    ///
    /// Polling the iface is _always_ required after this method succeeds.
    pub fn send<F, R>(
        &self,
        size: usize,
        meta: impl Into<UdpMetadata>,
        f: F,
    ) -> Result<R, SendError>
    where
        F: FnOnce(&mut [u8]) -> R,
    {
        let mut socket = self.0.inner.socket.lock();

        if size > socket.payload_send_capacity() {
            return Err(SendError::TooLarge);
        }

        let mut meta = meta.into();
        // Wildcard sockets belong to the default interface, but local
        // delivery can cross interfaces. Choose a loopback source instead of
        // its Ethernet address, preserving explicit binds and per-packet
        // source addresses supplied by callers.
        if meta.local_address.is_none()
            && self.0.bound.endpoint().addr.is_unspecified()
            && matches!(meta.endpoint.addr, IpAddress::Ipv4(addr) if addr.is_loopback())
        {
            meta.local_address = Some(IpAddress::Ipv4(Ipv4Address::LOCALHOST));
        }
        let buffer = socket.send(size, meta)?;
        let result = f(&mut *buffer);
        if rpc_message_type(buffer).is_some() {
            let xid = rpc_xid(buffer);
            match rpc_message_type(buffer) {
                Some(0) => trace_rpc(RPC_REQUEST_ENQUEUE, self.0.bound.port(), xid),
                Some(1) => trace_rpc(RPC_REPLY_ENQUEUE, self.0.bound.port(), xid),
                _ => {}
            }
            let slot = xid as usize % RPC_ARRIVAL_SLOTS;
            self.0.inner.send_enqueue_ms[slot]
                .store(Jiffies::elapsed().as_u64(), Ordering::Relaxed);
            self.0.inner.send_enqueue_xids[slot].store(xid, Ordering::Relaxed);
        }

        self.0
            .inner
            .need_dispatch
            .store(socket.send_queue() > 0, Ordering::Relaxed);

        Ok(result)
    }

    /// Receives some data.
    ///
    /// Polling the iface is _not_ required after this method succeeds.
    pub fn recv<CopyFn, R>(
        &self,
        behavior: ReceiveBehavior,
        copy_fn: CopyFn,
    ) -> Result<R, smoltcp::socket::udp::RecvError>
    where
        CopyFn: FnOnce(&[u8], UdpMetadata) -> R,
    {
        let mut socket = self.0.inner.socket.lock();

        let (data, meta) = match behavior {
            ReceiveBehavior::Recv => socket.recv()?,
            ReceiveBehavior::Peek => {
                let (data, meta) = socket.peek()?;
                (data, *meta)
            }
        };
        let mut first_retry_reply = None;
        let mut slow_candidate = None;
        if data.len() >= 4 && behavior == ReceiveBehavior::Recv {
            let xid = rpc_xid(data);
            match rpc_message_type(data) {
                Some(0) => trace_rpc(RPC_REQUEST_READ, self.0.bound.port(), xid),
                Some(1) => {
                    trace_rpc(RPC_REPLY_READ, self.0.bound.port(), xid);
                    if FIRST_RETRY_XID.load(Ordering::Relaxed) == xid {
                        first_retry_reply = Some(xid);
                    }
                    let slot = xid as usize % RPC_ARRIVAL_SLOTS;
                    // The setup probe talks to rpcbind on port 111. Its
                    // latency is unrelated to the LMBench RPC measurement.
                    if meta.endpoint.port != 111
                        && self.0.inner.first_tx_xids[slot].load(Ordering::Relaxed) == xid
                        && self.0.inner.last_tx_xid.load(Ordering::Relaxed) == xid
                    {
                        let first_ns = self.0.inner.first_tx_ns[slot].load(Ordering::Relaxed);
                        if first_ns != 0 {
                            slow_candidate = Some((xid, monotonic_ns().saturating_sub(first_ns)));
                        }
                    }
                }
                _ => {}
            }
            self.0.inner.last_read_xid.store(xid, Ordering::Relaxed);
            let slot = xid as usize % RPC_ARRIVAL_SLOTS;
            if rpc_message_type(data) == Some(0) {
                self.0.inner.request_read_ms[slot]
                    .store(Jiffies::elapsed().as_u64(), Ordering::Relaxed);
                self.0.inner.request_read_xids[slot].store(xid, Ordering::Relaxed);
            }
            if self.0.inner.arrival_xids[slot].load(Ordering::Relaxed) == xid {
                let arrival_ms = self.0.inner.arrival_ms[slot].load(Ordering::Relaxed);
                let delay_ms = Jiffies::elapsed().as_u64().saturating_sub(arrival_ms);
                self.0
                    .inner
                    .rx_read_delay_samples
                    .fetch_add(1, Ordering::Relaxed);
                self.0
                    .inner
                    .rx_read_delay_max_ms
                    .fetch_max(delay_ms, Ordering::Relaxed);
                if delay_ms >= 2 {
                    self.0
                        .inner
                        .rx_read_delay_ge_2ms
                        .fetch_add(1, Ordering::Relaxed);
                }
                if delay_ms >= 5 {
                    self.0
                        .inner
                        .rx_read_delay_ge_5ms
                        .fetch_add(1, Ordering::Relaxed);
                }
                if delay_ms >= 25 {
                    self.0
                        .inner
                        .rx_read_delay_ge_25ms
                        .fetch_add(1, Ordering::Relaxed);
                }
            }
            if xid != self.0.inner.last_tx_xid.load(Ordering::Relaxed) {
                self.0
                    .inner
                    .read_xid_mismatches
                    .fetch_add(1, Ordering::Relaxed);
            }
        }
        let result = copy_fn(data, meta);
        drop(socket);
        if let Some((xid, rtt_ns)) = slow_candidate {
            capture_slowest_trace(xid, rtt_ns);
        }
        if let Some(xid) = first_retry_reply {
            dump_first_retry_trace(xid);
        }

        Ok(result)
    }

    /// Calls `f` with an immutable reference to the associated [`RawUdpSocket`].
    //
    // NOTE: If a mutable reference is required, add a method above that correctly updates the next
    // polling time.
    pub fn raw_with<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&RawUdpSocket) -> R,
    {
        let socket = self.0.inner.socket.lock();
        f(&socket)
    }
}
