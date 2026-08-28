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
RISCV_PLATFORM_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/arch/riscv.rs"


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
            self.assertIn("5 passed", result.stdout)

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
            self.assertIn("9 passed", result.stdout)

    def test_device_uses_poll_budget_at_all_three_boundaries(self) -> None:
        source = DEVICE_SOURCE.read_text()
        self.assertIn("rx_poll: RxPollBudget", source)
        self.assertIn("self.rx_poll.can_receive()", source)
        self.assertIn("self.rx_poll.record_received()", source)
        self.assertIn("self.rx_poll.finish(self.fatal, more_rx)", source)
        self.assertIn("self.rx_poll.record_rearmed()", source)
        self.assertIn("ASTERINAS_GMAC_RX_POLL", source)

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


if __name__ == "__main__":
    unittest.main()
