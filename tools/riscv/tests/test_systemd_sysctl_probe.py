# SPDX-License-Identifier: MPL-2.0

import subprocess
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.dev_overlay import load_overlay_spec
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure


ROOTFS = Path(__file__).resolve().parents[1] / "debian" / "rootfs"


class ProbeArtifactTests(unittest.TestCase):
    def test_probe_script_syntax(self):
        script = ROOTFS / "sysctl_probe.sh"
        self.assertTrue(script.is_file())
        for shell in ("sh", "bash"):
            completed = subprocess.run(
                [shell, "-n", str(script)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_probe_script_contract(self):
        text = (ROOTFS / "sysctl_probe.sh").read_text(encoding="utf-8")
        for required in (
            "SYSCTL_PROBE begin",
            "SYSCTL_PROBE end",
            "timeout 15",
            "/proc/sys/kernel/pid_max",
            "/proc/sys/kernel/core_pattern",
            "/usr/lib/systemd/systemd-sysctl",
            "asterinas_probe_missing",
            "readback_pid_max",
            "readback_core_pattern",
        ):
            self.assertIn(required, text)

    def test_instrumented_unit_contract(self):
        text = (ROOTFS / "systemd-sysctl-probe.service").read_text(
            encoding="utf-8"
        )
        for required in (
            "ConditionPathIsReadWrite=/proc/sys/net/",
            "Before=sysinit.target shutdown.target",
            "ExecStart=/usr/lib/asterinas/sysctl-probe.sh",
            "StandardOutput=journal+console",
            "StandardError=journal+console",
            "TimeoutSec=90s",
            "ImportCredential=sysctl.*",
        ):
            self.assertIn(required, text)

    def test_overlay_spec_contract(self):
        spec = load_overlay_spec(
            ROOTFS / "systemd-sysctl-probe-overlay.json",
            expected_profile="desktop-m4-firefox",
        )
        self.assertEqual(len(spec.files), 2)
        by_destination = {entry.destination: entry for entry in spec.files}
        probe = by_destination["/usr/lib/asterinas/sysctl-probe.sh"]
        self.assertTrue(probe.create)
        self.assertEqual(probe.mode, 0o755)
        unit = by_destination[
            "/usr/lib/systemd/system/systemd-sysctl.service"
        ]
        self.assertFalse(unit.create)
        self.assertEqual(unit.mode, 0o644)


from tools.riscv.debian.rootfs.systemd_sysctl_probe import (
    SYSTEMD_SYSCTL_PROBE_BOOTARGS,
    classify_probe,
    parse_probe_args,
    parse_probe_markers,
)


def _passing_probe_transcript() -> bytes:
    return b"\n".join(
        (
            b"SYSCTL_PROBE begin",
            b"SYSCTL_PROBE step=read_pid_max rc=0 ms=1 value=2147483647",
            b"SYSCTL_PROBE step=read_core_pattern rc=0 ms=1 value=core",
            b"SYSCTL_PROBE step=write_pid_max rc=0 ms=1\r",
            b"SYSCTL_PROBE step=readback_pid_max rc=0 ms=1 value=4194304",
            b"SYSCTL_PROBE step=write_core_pattern rc=0 ms=1",
            b"SYSCTL_PROBE step=readback_core_pattern rc=0 ms=1 value=core",
            b"SYSCTL_PROBE step=binary_all rc=0 ms=2",
            b"sysctl-probe.sh[31]: SYSCTL_PROBE step=missing_control rc=1 ms=1 value=cat: /proc/sys/kernel/asterinas_probe_missing: No such file or directory",
            b"SYSCTL_PROBE end",
            b"[  OK  ] Started systemd-sysctl.service - Apply Kernel Variables.",
            b"[  OK  ] Reached target sysinit.target - System Initialization.",
        )
    )


class ProbeRunnerTests(unittest.TestCase):
    def test_bootargs_target_sysinit_directly(self):
        self.assertIn("systemd.unit=sysinit.target", SYSTEMD_SYSCTL_PROBE_BOOTARGS)
        self.assertIn("--root-init=systemd", SYSTEMD_SYSCTL_PROBE_BOOTARGS)
        self.assertNotIn("basic.target", SYSTEMD_SYSCTL_PROBE_BOOTARGS)

    def test_parse_probe_markers(self):
        steps = parse_probe_markers(_passing_probe_transcript())
        self.assertEqual(len(steps), 8)
        self.assertEqual(steps[0]["step"], "read_pid_max")
        self.assertEqual(steps[0]["rc"], 0)
        self.assertIn("value=2147483647", steps[0]["detail"])
        self.assertEqual(steps[3]["step"], "readback_pid_max")
        self.assertEqual(steps[7]["step"], "missing_control")
        self.assertIn("No such file or directory", steps[7]["detail"])

    def test_classify_probe_pass(self):
        passed, reason = classify_probe(_passing_probe_transcript(), "probe")
        self.assertTrue(passed, reason)

    def test_classify_service_pass(self):
        transcript = (
            b"[  OK  ] Started systemd-sysctl.service - Apply Kernel Variables.\n"
            b"[  OK  ] Reached target sysinit.target - System Initialization.\n"
        )
        passed, reason = classify_probe(transcript, "service")
        self.assertTrue(passed, reason)

    def test_classify_probe_rejects_timeout_step(self):
        transcript = _passing_probe_transcript().replace(
            b"step=write_pid_max rc=0", b"step=write_pid_max rc=124"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "step-timeout:write_pid_max")

    def test_classify_probe_rejects_failed_step(self):
        transcript = _passing_probe_transcript().replace(
            b"step=write_core_pattern rc=0", b"step=write_core_pattern rc=1"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "step-failed:write_core_pattern:1")

    def test_classify_probe_rejects_wrong_readback(self):
        transcript = _passing_probe_transcript().replace(
            b"value=4194304", b"value=2147483647"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "pid-max-readback")

    def test_classify_rejects_failed_unit(self):
        transcript = _passing_probe_transcript() + (
            b"\n[FAILED] Failed to start systemd-sysctl.service\n"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "systemd-sysctl-failed")

    def test_classify_rejects_missing_sysinit(self):
        transcript = _passing_probe_transcript().replace(
            b"[  OK  ] Reached target sysinit.target - System Initialization.", b""
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "sysinit-not-reached")

    def test_parse_probe_args_extracts_mode(self):
        arguments = [
            "--kernel", "kernel",
            "--uboot", "uboot",
            "--dtb", "dtb",
            "--stage1-initramfs", "stage1",
            "--root-image", "root",
            "--root-manifest", "manifest",
            "--packages-lock", "lock",
            "--package-checksums", "checksums",
            "--output-directory", "output",
            "--mode", "service",
        ]
        config, mode = parse_probe_args(arguments)
        self.assertEqual(mode, "service")
        self.assertEqual(str(config.kernel), "kernel")


from tools.riscv.debian.rootfs.systemd_sysctl_probe import orchestrate_probe


class _FakeOperations:
    mode = "probe"

    def __init__(self, transcript: bytes, protocol_error: Exception | None = None):
        self.transcript = transcript
        self.protocol_error = protocol_error
        self.states = []
        self.result = None

    def invalidate(self, config):
        self.invalidated = True

    def snapshot_inputs(self, config):
        return {"kernel": "k", "root_image": "r"}

    def validate_inputs(self, config, snapshots):
        return {"debian_release": "13.7", "profile": "desktop-m4-firefox"}

    def prepare(self, config, snapshots, identity):
        return {"root_disk_format": "qcow2"}

    def publish_state(self, config, state):
        self.states.append(state)

    def launch(self, config, prepared):
        return {"session": True}

    def run_protocol(self, session, config):
        if self.protocol_error is not None:
            raise self.protocol_error

    def request_quit(self, session, config):
        pass

    def close_monitor(self, session):
        pass

    def cleanup_process(self, session, config):
        pass

    def drain_serial(self, session, config):
        return self.transcript

    def publish(self, config, prepared, transcript, result):
        self.result = result


class ProbeOrchestrationTests(unittest.TestCase):
    def test_orchestrate_probe_passes_and_publishes_steps(self):
        operations = _FakeOperations(_passing_probe_transcript())
        result = orchestrate_probe(object(), operations)
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "pass")
        self.assertEqual(len(result["steps"]), 8)
        self.assertEqual(result["mode"], "probe")
        self.assertEqual(operations.result, result)

    def test_orchestrate_probe_records_protocol_failure(self):
        operations = _FakeOperations(
            _passing_probe_transcript(), GateFailure("protocol-timeout")
        )
        result = orchestrate_probe(object(), operations)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "protocol-timeout")
        self.assertEqual(len(result["steps"]), 8)


if __name__ == "__main__":
    unittest.main()
