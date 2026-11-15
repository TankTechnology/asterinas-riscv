// SPDX-License-Identifier: MPL-2.0

//! Bounded TCP diagnostics and monotonic bring-up probes.

use core::sync::atomic::{AtomicU8, Ordering};

#[path = "tcp_diagnostics/policy.rs"]
#[cfg_attr(not(target_os = "none"), allow(dead_code))]
mod policy;

pub use policy::TcpDiagnosticStage;
#[cfg(target_os = "none")]
use policy::{TcpTraceState, TraceDecision};

#[cfg(target_os = "none")]
const TCP_DIAGNOSTIC_LIMIT: u64 = 128;
#[cfg(target_os = "none")]
static TCP_TRACE: TcpTraceState = TcpTraceState::new(TCP_DIAGNOSTIC_LIMIT);

/// Enables bounded scalar-only tracing for connections involving `port`.
///
/// A zero port keeps the facility disabled. This is intended to be configured
/// once from the kernel command line during boot.
#[cfg(target_os = "none")]
pub fn configure_tcp_diagnostics(port: u16) {
    TCP_TRACE.configure(port);
    if port != 0 {
        ostd::info!(
            "ASTERINAS_TCP_TRACE stage=configured port={} limit={}",
            port,
            TCP_DIAGNOSTIC_LIMIT
        );
    }
}

#[cfg(not(target_os = "none"))]
#[allow(dead_code)]
pub fn configure_tcp_diagnostics(_port: u16) {}

/// Records one trace boundary without formatting packet contents or pointers.
#[cfg(target_os = "none")]
pub fn record_tcp_diagnostic(
    stage: TcpDiagnosticStage,
    connection: u32,
    local_port: u16,
    remote_port: u16,
    values: [u64; 3],
) {
    match TCP_TRACE.record(stage, connection, local_port, remote_port, values) {
        TraceDecision::Ignore => {}
        TraceDecision::Record { sequence } => ostd::info!(
            "ASTERINAS_TCP_TRACE seq={} stage={} connection={} local_port={} remote_port={} value0={} value1={} value2={}",
            sequence,
            stage.as_str(),
            connection,
            local_port,
            remote_port,
            values[0],
            values[1],
            values[2]
        ),
        TraceDecision::SuppressionSummary { suppressed } => ostd::info!(
            "ASTERINAS_TCP_TRACE stage=suppressed suppressed={} limit={}",
            suppressed,
            TCP_DIAGNOSTIC_LIMIT
        ),
    }
}

#[cfg(not(target_os = "none"))]
#[allow(dead_code)]
pub fn record_tcp_diagnostic(
    _stage: TcpDiagnosticStage,
    _connection: u32,
    _local_port: u16,
    _remote_port: u16,
    _values: [u64; 3],
) {
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub(crate) enum TcpEgressStage {
    Buffered = 1,
    SegmentDispatched = 2,
}

pub(crate) struct TcpEgressTrace(AtomicU8);

pub(crate) static TCP_EGRESS_TRACE: TcpEgressTrace = TcpEgressTrace::new();

impl TcpEgressTrace {
    pub(crate) const fn new() -> Self {
        Self(AtomicU8::new(0))
    }

    /// Records a stage and returns whether it advanced the global observation.
    pub(crate) fn record(&self, stage: TcpEgressStage) -> bool {
        self.0.fetch_max(stage as u8, Ordering::Relaxed) < stage as u8
    }
}

impl TcpEgressStage {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Buffered => "buffered",
            Self::SegmentDispatched => "segment-dispatched",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub(super) enum SynAckStage {
    Parsed = 1,
    ConnectionFound = 2,
    SocketAccepted = 3,
}

pub(super) struct SynAckTrace(AtomicU8);

impl SynAckTrace {
    pub(super) const fn new() -> Self {
        Self(AtomicU8::new(0))
    }

    /// Records a stage and returns whether it advanced the global observation.
    pub(super) fn record(&self, stage: SynAckStage) -> bool {
        self.0.fetch_max(stage as u8, Ordering::Relaxed) < stage as u8
    }
}

impl SynAckStage {
    pub(super) const fn as_str(self) -> &'static str {
        match self {
            Self::Parsed => "parsed",
            Self::ConnectionFound => "connection-found",
            Self::SocketAccepted => "socket-accepted",
        }
    }
}
