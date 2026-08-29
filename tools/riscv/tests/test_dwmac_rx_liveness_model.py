from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_SOURCE = REPOSITORY_ROOT / "tools/riscv/dwmac_rx_liveness_model.rs"
TX_CACHELINE_MODEL_SOURCE = REPOSITORY_ROOT / "tools/riscv/dwmac_tx_cacheline_model.rs"
POLL_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/poll.rs"
QUEUE_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/queue.rs"
DEVICE_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/device.rs"
DESCRIPTOR_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/descriptor.rs"
DWMAC_DIAGNOSTICS_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/diagnostics.rs"
DWMAC_REGS_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/regs.rs"
DWMAC_PHY_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/phy.rs"
RISCV_PLATFORM_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/arch/riscv.rs"
BIGTCP_DIAGNOSTICS_SOURCE = (
    REPOSITORY_ROOT / "kernel/libs/aster-bigtcp/src/iface/tcp_diagnostics.rs"
)
BIGTCP_POLL_SOURCE = REPOSITORY_ROOT / "kernel/libs/aster-bigtcp/src/iface/poll.rs"
BIGTCP_TCP_CONN_SOURCE = (
    REPOSITORY_ROOT / "kernel/libs/aster-bigtcp/src/socket/bound/tcp_conn.rs"
)


class DwmacRxLivenessModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls._temporary_directory.name) / "dwmac-rx-model"
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                "-Dwarnings",
                str(MODEL_SOURCE),
                "-o",
                str(cls.binary),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    def run_model(self, protocol: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.binary), "--protocol", protocol, "--ring-size", "2", "--json"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_current_protocol_reports_starvation_counterexample(self) -> None:
        result = self.run_model("current")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["protocol"], "current")
        self.assertEqual(report["verdict"], "counterexample")
        self.assertEqual(report["property"], "bounded-rx-poll")
        self.assertGreater(len(report["prefix"]), 0)
        self.assertIn("raise-timer", report["prefix"])
        self.assertIn("raise-tx", report["prefix"])
        self.assertIn("dma-complete", report["cycle"])
        self.assertIn("poll-consume", report["cycle"])
        self.assertLessEqual(len(report["prefix"]) + len(report["cycle"]), 12)
        self.assertEqual(report["cycle"].count("dma-complete"), 2)
        self.assertEqual(report["cycle"].count("poll-consume"), 2)
        self.assertNotIn("service-timer", report["cycle"])
        self.assertNotIn("service-tx", report["cycle"])

    def test_cli_rejects_noncanonical_arguments(self) -> None:
        for arguments in (
            [],
            ["--protocol", "unknown", "--ring-size", "2", "--json"],
            ["--protocol", "current", "--ring-size", "1", "--json"],
            ["--protocol", "current", "--ring-size", "5", "--json"],
            ["--protocol", "current", "--ring-size", "02", "--json"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(self.binary), *arguments],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)

    def test_bounded_protocol_has_no_starvation_or_lost_wakeup(self) -> None:
        result = self.run_model("bounded")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["protocol"], "bounded")
        self.assertEqual(report["verdict"], "verified-within-model")
        self.assertEqual(
            report["properties"],
            [
                "descriptor-ownership",
                "bounded-rx-poll",
                "eventual-rearm-or-reschedule",
                "no-lost-rx-wakeup",
                "tx-timer-progress",
            ],
        )
        self.assertGreater(report["explored_states"], 0)

    def test_all_reduced_ring_sizes_are_verified(self) -> None:
        for ring_size in ("2", "3", "4"):
            with self.subTest(ring_size=ring_size):
                result = subprocess.run(
                    [
                        str(self.binary),
                        "--protocol",
                        "bounded",
                        "--ring-size",
                        ring_size,
                        "--json",
                    ],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_counterexample_is_deterministic(self) -> None:
        first = self.run_model("current")
        second = self.run_model("current")
        self.assertEqual(first.returncode, 1)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stderr, second.stderr)


class DwmacRxPollContractTests(unittest.TestCase):
    def test_pause_flow_control_follows_negotiation_and_fifo_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "pause-flow-control.rs"
            binary = Path(directory) / "pause-flow-control"
            harness.write_text(
                f'''#[allow(dead_code)]
#[path = r"{DWMAC_PHY_SOURCE}"]
mod phy;
#[allow(dead_code)]
#[path = r"{DWMAC_REGS_SOURCE}"]
mod regs;

use std::collections::VecDeque;

use phy::{{
    ADVERTISE_100_FULL, ADVERTISE_PAUSE_ASYM, ADVERTISE_PAUSE_CAP,
    BMCR_AUTONEG_ENABLE, BMSR_AUTONEG_COMPLETE, BMSR_LINK_STATUS, Deadline, MdioBus,
    MdioError, read_link_state,
}};
use regs::{{
    DMA_DEBUG_STATUS0, MAC_DEBUG, MTL_QUEUE0_INTERRUPT_CONTROL_STATUS,
    MTL_RX_QUEUE0_DEBUG, RegisterValueError, configure_flow_control,
    decode_flow_control_debug, mac_tx_flow_control_busy,
    validate_flow_control_readback, validate_rx_fifo_readback,
}};

struct FakeMdio {{
    reads: VecDeque<u16>,
}}

impl MdioBus for FakeMdio {{
    fn read(&mut self, _: u8, _: u8, _: Deadline) -> Result<u16, MdioError> {{
        Ok(self.reads.pop_front().unwrap())
    }}

    fn write(&mut self, _: u8, _: u8, _: u16, _: Deadline) -> Result<(), MdioError> {{
        Ok(())
    }}
}}

fn negotiated(local: u16, partner: u16, full_duplex: bool) -> phy::LinkState {{
    let mode = if full_duplex {{ ADVERTISE_100_FULL }} else {{ 1 << 7 }};
    let mut mdio = FakeMdio {{
        reads: [
            BMSR_LINK_STATUS,
            BMSR_LINK_STATUS | BMSR_AUTONEG_COMPLETE,
            BMCR_AUTONEG_ENABLE,
            0,
            0,
            mode | local,
            mode | partner,
        ]
        .into(),
    }};
    read_link_state(&mut mdio, 0, Deadline::from_nanoseconds(1))
        .unwrap()
        .unwrap()
}}

fn main() {{
    let symmetric = negotiated(ADVERTISE_PAUSE_CAP, ADVERTISE_PAUSE_CAP, true);
    assert!(symmetric.tx_pause());
    assert!(symmetric.rx_pause());

    let tx_only = negotiated(
        ADVERTISE_PAUSE_ASYM,
        ADVERTISE_PAUSE_CAP | ADVERTISE_PAUSE_ASYM,
        true,
    );
    assert!(tx_only.tx_pause());
    assert!(!tx_only.rx_pause());

    let rx_only = negotiated(
        ADVERTISE_PAUSE_CAP | ADVERTISE_PAUSE_ASYM,
        ADVERTISE_PAUSE_ASYM,
        true,
    );
    assert!(!rx_only.tx_pause());
    assert!(rx_only.rx_pause());

    let half_duplex = negotiated(ADVERTISE_PAUSE_CAP, ADVERTISE_PAUSE_CAP, false);
    assert!(!half_duplex.tx_pause());
    assert!(!half_duplex.rx_pause());

    assert_eq!(DMA_DEBUG_STATUS0.offset(), 0x100c);
    assert_eq!(MAC_DEBUG.offset(), 0x0114);
    assert_eq!(MTL_QUEUE0_INTERRUPT_CONTROL_STATUS.offset(), 0x0d2c);
    assert_eq!(MTL_RX_QUEUE0_DEBUG.offset(), 0x0d38);

    validate_rx_fifo_readback(5, 0x00f0_0020).unwrap();
    assert_eq!(
        validate_rx_fifo_readback(5, 0x0070_0020),
        Err(RegisterValueError::ReadbackMismatch),
    );

    let configured = configure_flow_control(0x40f0_0020, true, true).unwrap();
    assert_eq!(configured.rx_fifo_bytes, 4096);
    assert_eq!(configured.mtl_rx_operation_mode, 0x40f0_c1a0);
    assert_eq!(configured.mac_tx_flow_control_queue0, 0xffff_0002);
    assert_eq!(configured.mac_rx_flow_control, 1);
    validate_flow_control_readback(
        configured,
        configured.mtl_rx_operation_mode,
        configured.mac_tx_flow_control_queue0,
        configured.mac_rx_flow_control,
    )
    .unwrap();
    assert_eq!(
        validate_flow_control_readback(
            configured,
            configured.mtl_rx_operation_mode ^ (1 << 8),
            configured.mac_tx_flow_control_queue0,
            configured.mac_rx_flow_control,
        ),
        Err(RegisterValueError::ReadbackMismatch),
    );
    assert!(!mac_tx_flow_control_busy(configured.mac_tx_flow_control_queue0));
    assert!(mac_tx_flow_control_busy(configured.mac_tx_flow_control_queue0 | 1));

    let receive_only = configure_flow_control(0x00f0_0020, false, true).unwrap();
    assert_eq!(receive_only.rx_fifo_bytes, 4096);
    assert_eq!(receive_only.mtl_rx_operation_mode, 0x00f0_0020);
    assert_eq!(receive_only.mac_tx_flow_control_queue0, 0);
    assert_eq!(receive_only.mac_rx_flow_control, 1);

    let small_fifo = configure_flow_control(0x0070_0020, true, false).unwrap();
    assert_eq!(small_fifo.rx_fifo_bytes, 2048);
    assert_eq!(small_fifo.mtl_rx_operation_mode, 0x0070_0020);
    assert_eq!(small_fifo.mac_tx_flow_control_queue0, 0xffff_0002);
    assert_eq!(small_fifo.mac_rx_flow_control, 0);

    let debug = decode_flow_control_debug(0x0007_0005, 0x0008_0027, 0x0000_6400, 1 << 16);
    assert_eq!(debug.mac_tx_controller_state, 3);
    assert!(debug.mac_tx_engine_active);
    assert_eq!(debug.mac_rx_fifo_state, 2);
    assert!(debug.mac_rx_engine_active);
    assert_eq!(debug.mtl_rx_queued_packets, 8);
    assert_eq!(debug.mtl_rx_fill_level, 2);
    assert_eq!(debug.mtl_rx_read_state, 3);
    assert!(debug.mtl_rx_write_active);
    assert_eq!(debug.dma_tx_state, 6);
    assert_eq!(debug.dma_rx_state, 4);
    assert!(debug.mtl_rx_overflow);
}}
'''
            )
            compile_result = subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    "-Dwarnings",
                    str(harness),
                    "-o",
                    str(binary),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(binary)],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_packet_diagnostics_distinguish_rx_and_tx_tcp_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "packet-diagnostics.rs"
            binary = Path(directory) / "packet-diagnostics"
            harness.write_text(
                f'''#[allow(dead_code)]
#[path = r"{DWMAC_REGS_SOURCE}"]
mod regs;
#[path = r"{DWMAC_DIAGNOSTICS_SOURCE}"]
mod diagnostics;

use diagnostics::{{MtlRxLossDiagnostics, RxDescriptorDrop, RxDiagnostics, TxDiagnostics}};
use regs::{{decode_mtl_rx_loss, MTL_RX_QUEUE0_MISSED_PACKET_OVERFLOW_COUNTER}};

fn ethernet_frame(ethertype: u16, protocol: u8, tcp_flags: u8, payload_len: usize) -> Vec<u8> {{
    let mut frame = vec![0u8; 54 + payload_len];
    frame[12..14].copy_from_slice(&ethertype.to_be_bytes());
    frame[14] = 0x45;
    frame[16..18].copy_from_slice(&(40u16 + payload_len as u16).to_be_bytes());
    frame[23] = protocol;
    frame[46] = 0x50;
    frame[47] = tcp_flags;
    frame
}}

fn arp_frame(operation: u16, ethernet_dst: [u8; 6], target_hardware: [u8; 6]) -> Vec<u8> {{
    let mut frame = vec![0u8; 60];
    frame[..6].copy_from_slice(&ethernet_dst);
    frame[6..12].copy_from_slice(&[0x02, 0, 0, 0, 0, 1]);
    frame[12..14].copy_from_slice(&0x0806u16.to_be_bytes());
    frame[14..16].copy_from_slice(&1u16.to_be_bytes());
    frame[16..18].copy_from_slice(&0x0800u16.to_be_bytes());
    frame[18] = 6;
    frame[19] = 4;
    frame[20..22].copy_from_slice(&operation.to_be_bytes());
    frame[22..28].copy_from_slice(&[0x02, 0, 0, 0, 0, 1]);
    frame[28..32].copy_from_slice(&[10, 100, 16, 136]);
    frame[32..38].copy_from_slice(&target_hardware);
    frame[38..42].copy_from_slice(&[10, 100, 19, 200]);
    frame
}}

fn main() {{
    let local_hardware = [0x00, 0x48, 0x54, 0x71, 0x00, 0x48];
    let mut diagnostics = RxDiagnostics::default();
    diagnostics.record_frame(
        &arp_frame(1, [0xff; 6], [0xff; 6]),
        local_hardware,
    );
    diagnostics.record_frame(
        &arp_frame(2, local_hardware, local_hardware),
        local_hardware,
    );
    diagnostics.record_frame(
        &arp_frame(2, [0x02, 0, 0, 0, 0, 2], [0x02, 0, 0, 0, 0, 2]),
        local_hardware,
    );
    diagnostics.record_frame(&ethernet_frame(0x0800, 6, 0x02, 0), local_hardware);
    diagnostics.record_frame(&ethernet_frame(0x0800, 6, 0x12, 0), local_hardware);
    diagnostics.record_frame(&ethernet_frame(0x0800, 6, 0x10, 0), local_hardware);
    diagnostics.record_frame(&ethernet_frame(0x0800, 17, 0, 0), local_hardware);
    diagnostics.record_frame(&ethernet_frame(0x86dd, 0, 0, 0), local_hardware);
    diagnostics.record_frame(&[0u8; 13], local_hardware);
    let mut truncated_payload = ethernet_frame(0x0800, 6, 0x18, 40);
    truncated_payload[16..18].copy_from_slice(&1500u16.to_be_bytes());
    diagnostics.record_frame(&truncated_payload, local_hardware);
    diagnostics.record_descriptor_drop(RxDescriptorDrop::Fragmented);
    diagnostics.record_descriptor_drop(RxDescriptorDrop::ReceiveError);
    diagnostics.record_descriptor_drop(RxDescriptorDrop::FrameTooLong);
    diagnostics.record_descriptor_drop(RxDescriptorDrop::Other);

    let report = diagnostics.report();
    assert_eq!(report.observed, 10);
    assert_eq!(report.arp, 3);
    assert_eq!(report.arp_requests, 1);
    assert_eq!(report.arp_replies, 2);
    assert_eq!(report.arp_replies_to_us, 1);
    assert_eq!(report.ipv4_other, 1);
    assert_eq!(report.tcp_syn, 1);
    assert_eq!(report.tcp_syn_ack, 1);
    assert_eq!(report.tcp_other, 2);
    assert_eq!(report.other, 1);
    assert_eq!(report.malformed, 1);
    assert_eq!(report.descriptor_fragmented, 1);
    assert_eq!(report.descriptor_receive_error, 1);
    assert_eq!(report.descriptor_frame_too_long, 1);
    assert_eq!(report.descriptor_other, 1);

    let mut tx = TxDiagnostics::default();
    assert!(!tx.record_frame(&ethernet_frame(0x0806, 0, 0, 0)));
    assert!(!tx.record_frame(&ethernet_frame(0x0800, 6, 0x02, 0)));
    assert!(!tx.record_frame(&ethernet_frame(0x0800, 6, 0x10, 0)));
    assert!(tx.record_frame(&ethernet_frame(0x0800, 6, 0x18, 37)));
    assert!(!tx.record_frame(&ethernet_frame(0x0800, 6, 0x18, 11)));
    assert!(!tx.record_frame(&ethernet_frame(0x0800, 17, 0, 0)));
    assert!(!tx.record_frame(&[0u8; 13]));
    let tx_report = tx.report();
    assert_eq!(tx_report.observed, 7);
    assert_eq!(tx_report.arp, 1);
    assert_eq!(tx_report.ipv4_other, 1);
    assert_eq!(tx_report.tcp_syn, 1);
    assert_eq!(tx_report.tcp_ack_only, 1);
    assert_eq!(tx_report.tcp_data, 2);
    assert_eq!(tx_report.malformed, 1);

    assert_eq!(MTL_RX_QUEUE0_MISSED_PACKET_OVERFLOW_COUNTER.offset(), 0x0d34);
    let first = decode_mtl_rx_loss((1 << 27) | (23 << 16) | (1 << 11) | 19);
    assert_eq!(first.missed_packets, 23);
    assert!(first.missed_counter_overflow);
    assert_eq!(first.fifo_overflow_packets, 19);
    assert!(first.fifo_counter_overflow);

    let mut mtl = MtlRxLossDiagnostics::default();
    mtl.record(first);
    mtl.record(decode_mtl_rx_loss((7 << 16) | 5));
    mtl.record_read_failure();
    let mtl_report = mtl.report();
    assert_eq!(mtl_report.missed_packets, 30);
    assert_eq!(mtl_report.missed_counter_overflows, 1);
    assert_eq!(mtl_report.fifo_overflow_packets, 24);
    assert_eq!(mtl_report.fifo_counter_overflows, 1);
    assert_eq!(mtl_report.read_failures, 1);
}}
'''
            )
            compile_result = subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    "-Dwarnings",
                    str(harness),
                    "-o",
                    str(binary),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(binary)],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tcp_ingress_and_egress_traces_advance_monotonically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "tcp-diagnostics.rs"
            binary = Path(directory) / "tcp-diagnostics"
            harness.write_text(
                f'''#[path = r"{BIGTCP_DIAGNOSTICS_SOURCE}"]
mod tcp_diagnostics;

use tcp_diagnostics::{{TCP_EGRESS_TRACE, TcpEgressStage, SynAckStage, SynAckTrace}};

fn main() {{
    let trace = SynAckTrace::new();
    assert_eq!(SynAckStage::Parsed.as_str(), "parsed");
    assert_eq!(SynAckStage::ConnectionFound.as_str(), "connection-found");
    assert_eq!(SynAckStage::SocketAccepted.as_str(), "socket-accepted");
    assert!(trace.record(SynAckStage::Parsed));
    assert!(!trace.record(SynAckStage::Parsed));
    assert!(trace.record(SynAckStage::ConnectionFound));
    assert!(!trace.record(SynAckStage::Parsed));
    assert!(trace.record(SynAckStage::SocketAccepted));
    assert!(!trace.record(SynAckStage::ConnectionFound));

    assert_eq!(TcpEgressStage::Buffered.as_str(), "buffered");
    assert_eq!(TcpEgressStage::SegmentDispatched.as_str(), "segment-dispatched");
    assert!(TCP_EGRESS_TRACE.record(TcpEgressStage::Buffered));
    assert!(!TCP_EGRESS_TRACE.record(TcpEgressStage::Buffered));
    assert!(TCP_EGRESS_TRACE.record(TcpEgressStage::SegmentDispatched));
    assert!(!TCP_EGRESS_TRACE.record(TcpEgressStage::Buffered));
}}
'''
            )
            compile_result = subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    "-Dwarnings",
                    str(harness),
                    "-o",
                    str(binary),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(binary)],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_diagnostics_are_wired_to_bounded_markers(self) -> None:
        device = DEVICE_SOURCE.read_text()
        poll = BIGTCP_POLL_SOURCE.read_text()
        tcp_conn = BIGTCP_TCP_CONN_SOURCE.read_text()

        self.assertIn("rx_diagnostics: RxDiagnostics", device)
        self.assertIn("tx_diagnostics: TxDiagnostics", device)
        self.assertIn(
            ".record_frame(&prefix[..copied], self.selected.mac_address)", device
        )
        self.assertIn("self.tx_diagnostics.record_frame", device)
        self.assertIn("self.rx_diagnostics.record_descriptor_drop", device)
        self.assertIn("ASTERINAS_GMAC_RX_CLASS", device)
        self.assertIn("ASTERINAS_GMAC_TX_CLASS", device)
        self.assertIn("ASTERINAS_GMAC_TX stage=tcp-data-submitted", device)
        for field in (
            "arp_requests={}",
            "arp_replies={}",
            "arp_replies_to_us={}",
            "tcp_syn_ack={}",
            "descriptor_fragmented={}",
            "descriptor_receive_error={}",
            "descriptor_frame_too_long={}",
        ):
            with self.subTest(field=field):
                self.assertIn(field, device)

        self.assertIn("ASTERINAS_TCP_SYN_ACK stage={}", poll)
        self.assertIn("SynAckStage::Parsed", poll)
        self.assertIn("SynAckStage::ConnectionFound", poll)
        self.assertIn("SynAckStage::SocketAccepted", poll)
        self.assertIn("TcpEgressStage::SegmentDispatched", poll)
        self.assertIn("ASTERINAS_TCP_EGRESS stage={}", poll)
        self.assertIn("TcpEgressStage::Buffered", tcp_conn)
        self.assertIn("ASTERINAS_TCP_EGRESS stage={}", tcp_conn)

    def test_megrez_requires_documented_dwmac_5_20(self) -> None:
        source = RISCV_PLATFORM_SOURCE.read_text()

        self.assertIn("const EIC7700_DWMAC_VERSION: u8 = 0x52;", source)
        self.assertIn("version != EIC7700_DWMAC_VERSION", source)
        self.assertNotIn("GMAC4_MIN_VERSION", source)
        self.assertNotIn("GMAC5_MAX_VERSION", source)

    def test_tx_cacheline_model_exposes_packed_descriptor_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "dwmac-tx-cacheline-model"
            compile_result = subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    "-Dwarnings",
                    "--test",
                    str(TX_CACHELINE_MODEL_SOURCE),
                    "-o",
                    str(binary),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(binary), "--nocapture"],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("9 passed", result.stdout)

    def test_descriptor_handoff_matches_visibility_model(self) -> None:
        queue = QUEUE_SOURCE.read_text()
        descriptor = DESCRIPTOR_SOURCE.read_text()
        device = DEVICE_SOURCE.read_text()

        for step in (
            "self.write_descriptor_body(",
            "dma_write_barrier();",
            "self.write_descriptor_control(",
            "self.read_descriptor_control(",
            "dma_read_barrier();",
            "self.read_descriptor_body(",
        ):
            self.assertIn(step, queue)

        write_steps = (
            queue.index("self.write_descriptor_body("),
            queue.index("dma_write_barrier();"),
            queue.index("self.write_descriptor_control("),
        )
        self.assertEqual(write_steps, tuple(sorted(write_steps)))

        read_steps = (
            queue.index("self.read_descriptor_control("),
            queue.index("dma_read_barrier();"),
            queue.index("self.read_descriptor_body("),
        )
        self.assertEqual(read_steps, tuple(sorted(read_steps)))
        self.assertIn(".read_once(control_offset)", queue)
        self.assertIn(".write_once(control_offset, control)", queue)

        tail_writes = list(
            re.finditer(
                r"(?:self\.)?write\(\s*DMA_CHANNEL0_(?:RX|TX)_TAIL_POINTER",
                device,
            )
        )
        self.assertGreaterEqual(len(tail_writes), 4)
        for tail_write in tail_writes:
            with self.subTest(tail_write=tail_write.group(0)):
                preceding = device[
                    max(0, tail_write.start() - 240) : tail_write.start()
                ]
                self.assertIn("dma_write_barrier();", preceding)

        self.assertNotIn("fence(Ordering::", descriptor)

    def test_descriptor_ring_uses_uncached_coherent_memory(self) -> None:
        source = QUEUE_SOURCE.read_text()
        self.assertIn("ring: DmaCoherent", source)
        self.assertIn("DmaCoherent::alloc(1, false)", source)
        self.assertIn(".and_then(DmaCoherent::into_uncached)", source)
        self.assertLess(
            source.index(".and_then(DmaCoherent::into_uncached)"),
            source.index("queue.write_descriptor(false, slot, &descriptor)"),
        )
        self.assertNotIn("ring.sync_from_device", source)
        self.assertNotIn("ring.sync_to_device", source)

    def test_production_poll_budget_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "dwmac-rx-poll-tests"
            compile_result = subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    "-Dwarnings",
                    "--test",
                    str(POLL_SOURCE),
                    "-o",
                    str(binary),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(binary), "--nocapture"],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("11 passed", result.stdout)

    def test_device_uses_poll_budget_at_all_three_boundaries(self) -> None:
        source = DEVICE_SOURCE.read_text()
        self.assertIn("rx_poll: RxPollBudget", source)
        self.assertIn("self.rx_poll.can_receive()", source)
        self.assertIn("self.rx_poll.record_received()", source)
        self.assertIn("self.rx_poll.record_rx_buffer_unavailable()", source)
        self.assertIn("self.rx_poll.finish(self.fatal, more_rx)", source)
        self.assertIn("self.rx_poll.record_rearmed()", source)
        self.assertIn("ASTERINAS_GMAC_RX_POLL", source)
        self.assertIn("rx_buffer_unavailable={}", source)

    def test_queue_progress_snapshot_preserves_tx_accounting(self) -> None:
        source = QUEUE_SOURCE.read_text()
        for field in (
            "tx_submitted: u64",
            "tx_reclaimed: u64",
            "tx_outstanding: usize",
            "rx_head: usize",
            "rx_tail: usize",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)
        self.assertIn("pub(super) fn progress(&self) -> QueueProgress", source)
        self.assertIn("tx_progress_matches_outstanding_across_wrap", source)

    def test_device_emits_complete_datapath_marker(self) -> None:
        source = DEVICE_SOURCE.read_text()
        self.assertIn("ASTERINAS_GMAC_DATAPATH", source)
        for field in (
            "rx={}",
            "rx_budget={}",
            "rx_reschedules={}",
            "plic_rearms={}",
            "tx_submitted={}",
            "tx_reclaimed={}",
            "tx_outstanding={}",
            "rx_head={}",
            "rx_tail={:#018x}",
            "dma_status={:#010x}",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)
        self.assertIn("take_progress_report", source)

    def test_device_reports_documented_mtl_receive_loss_counters(self) -> None:
        source = DEVICE_SOURCE.read_text()
        self.assertIn("MTL_RX_QUEUE0_MISSED_PACKET_OVERFLOW_COUNTER", source)
        self.assertIn("decode_mtl_rx_loss", source)
        self.assertIn("ASTERINAS_GMAC_MTL_RX_LOSS", source)
        for field in (
            "missed_packets={}",
            "missed_counter_overflows={}",
            "fifo_overflow_packets={}",
            "fifo_counter_overflows={}",
            "read_failures={}",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)

    def test_device_reports_documented_mac_mtl_dma_state_machines(self) -> None:
        source = DEVICE_SOURCE.read_text()
        for register in (
            "MAC_DEBUG",
            "MTL_QUEUE0_INTERRUPT_CONTROL_STATUS",
            "MTL_RX_QUEUE0_DEBUG",
            "DMA_DEBUG_STATUS0",
        ):
            with self.subTest(register=register):
                self.assertIn(register, source)
        self.assertIn("ASTERINAS_GMAC_HW_STATE", source)
        for field in (
            "mac_tx_state={}",
            "mac_tx_active={}",
            "mac_rx_fifo_state={}",
            "mac_rx_active={}",
            "mtl_rx_packets={}",
            "mtl_rx_fill={}",
            "mtl_rx_read_state={}",
            "mtl_rx_write_active={}",
            "dma_tx_state={}",
            "dma_rx_state={}",
            "mtl_rx_overflow={}",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)

    def test_device_emits_dma_address_contract(self) -> None:
        queue = QUEUE_SOURCE.read_text()
        device = DEVICE_SOURCE.read_text()

        for field in (
            "ring_paddr: usize",
            "ring_daddr: usize",
            "ring_cpu_alias: Option<usize>",
        ):
            with self.subTest(field=field):
                self.assertIn(field, queue)
        self.assertIn("ASTERINAS_GMAC_DMA_CONTRACT", device)
        for field in (
            "version={:#04x}",
            "ring_paddr={:#018x}",
            "ring_daddr={:#018x}",
            "ring_cpu_alias={:#018x?}",
            "tx_ring={:#018x}",
            "rx_ring={:#018x}",
            "tx_tail={:#018x}",
            "rx_tail={:#018x}",
        ):
            with self.subTest(field=field):
                self.assertIn(field, device)


if __name__ == "__main__":
    unittest.main()
