// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicU32, AtomicU64, Ordering};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TcpDiagnosticStage {
    SendBuffered,
    PollScheduled,
    PendingPop,
    SegmentGenerated,
    PeerProcess,
    SocketEvents,
    PolleeNotify,
}

impl TcpDiagnosticStage {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::SendBuffered => "send-buffered",
            Self::PollScheduled => "poll-scheduled",
            Self::PendingPop => "pending-pop",
            Self::SegmentGenerated => "segment-generated",
            Self::PeerProcess => "peer-process",
            Self::SocketEvents => "socket-events",
            Self::PolleeNotify => "pollee-notify",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TraceDecision {
    Ignore,
    Record { sequence: u64 },
    SuppressionSummary { suppressed: u64 },
}

pub struct TcpTraceState {
    port: AtomicU32,
    records: AtomicU64,
    suppressed: AtomicU64,
    limit: u64,
}

impl TcpTraceState {
    pub const fn new(limit: u64) -> Self {
        Self {
            port: AtomicU32::new(0),
            records: AtomicU64::new(0),
            suppressed: AtomicU64::new(0),
            limit,
        }
    }

    pub fn configure(&self, port: u16) {
        self.port.store(u32::from(port), Ordering::Relaxed);
    }

    pub fn record(
        &self,
        _stage: TcpDiagnosticStage,
        _connection: u32,
        local_port: u16,
        remote_port: u16,
        _values: [u64; 3],
    ) -> TraceDecision {
        let selected_port = self.port.load(Ordering::Relaxed);
        if selected_port == 0
            || (u32::from(local_port) != selected_port && u32::from(remote_port) != selected_port)
        {
            return TraceDecision::Ignore;
        }

        let record = self.records.fetch_add(1, Ordering::Relaxed);
        if record < self.limit {
            return TraceDecision::Record {
                sequence: record + 1,
            };
        }

        let suppressed = self.suppressed.fetch_add(1, Ordering::Relaxed) + 1;
        if suppressed == 1 {
            TraceDecision::SuppressionSummary { suppressed }
        } else {
            TraceDecision::Ignore
        }
    }

    #[cfg(test)]
    pub fn suppressed(&self) -> u64 {
        self.suppressed.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod tests {
    use super::{TcpDiagnosticStage, TcpTraceState, TraceDecision};

    #[test]
    fn disabled_and_unrelated_ports_do_not_consume_the_budget() {
        let trace = TcpTraceState::new(2);
        assert_eq!(
            trace.record(TcpDiagnosticStage::SendBuffered, 7, 2828, 40000, [1, 2, 3]),
            TraceDecision::Ignore
        );

        trace.configure(2828);
        assert_eq!(
            trace.record(TcpDiagnosticStage::SendBuffered, 7, 3000, 40000, [1, 2, 3]),
            TraceDecision::Ignore
        );
        assert_eq!(
            trace.record(TcpDiagnosticStage::SendBuffered, 7, 40000, 2828, [1, 2, 3]),
            TraceDecision::Record { sequence: 1 }
        );
    }

    #[test]
    fn trace_is_bounded_and_emits_one_suppression_summary() {
        let trace = TcpTraceState::new(2);
        trace.configure(2828);

        assert_eq!(
            trace.record(TcpDiagnosticStage::PendingPop, 9, 2828, 40000, [0; 3]),
            TraceDecision::Record { sequence: 1 }
        );
        assert_eq!(
            trace.record(
                TcpDiagnosticStage::SegmentGenerated,
                9,
                2828,
                40000,
                [1176, 0, 0]
            ),
            TraceDecision::Record { sequence: 2 }
        );
        assert_eq!(
            trace.record(TcpDiagnosticStage::SocketEvents, 9, 2828, 40000, [1, 0, 0]),
            TraceDecision::SuppressionSummary { suppressed: 1 }
        );
        assert_eq!(
            trace.record(TcpDiagnosticStage::PolleeNotify, 0, 2828, 40000, [1, 0, 0]),
            TraceDecision::Ignore
        );
        assert_eq!(trace.suppressed(), 2);
    }
}
