#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the two-host diskless RISC-V probe."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


class DualHostQemuCommandTests(unittest.TestCase):
    def probe_module(self):
        try:
            return importlib.import_module("tools.riscv.dual_host_qemu_probe")
        except ModuleNotFoundError as error:
            self.fail(f"dual-host QEMU probe module is missing: {error}")

    def test_regular_artifact_identity_is_exact(self) -> None:
        probe = self.probe_module()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "kernel.Image"
            artifact.write_bytes(b"asterinas-kernel")
            identity = probe.read_identity(artifact)
            self.assertEqual(identity.size, len(b"asterinas-kernel"))
            self.assertEqual(
                identity.sha256, hashlib.sha256(b"asterinas-kernel").hexdigest()
            )

    def test_symlink_is_not_an_artifact(self) -> None:
        probe = self.probe_module()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "kernel.Image"
            source.write_bytes(b"kernel")
            link = Path(directory) / "kernel-link.Image"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                probe.read_identity(link)

    def test_probe_bootargs_exclude_disk_and_network_workload(self) -> None:
        probe = self.probe_module()
        bootargs = probe.qemu_bootargs()
        self.assertIn("--root-init=probe", bootargs)
        self.assertIn("asterinas.reboot_after=180", bootargs)
        self.assertNotIn("mmc_write_partition2", bootargs)
        self.assertNotIn("asterinas.net=", bootargs)

    def test_developer_command_is_bounded_and_device_free(self) -> None:
        probe = self.probe_module()
        argv = probe.developer_argv(
            "asterinas-dev-v1-test", Path("/root/asterinas/kernel.Image"),
            Path("/root/asterinas/initramfs.cpio"), 210,
        )
        self.assertEqual(argv[:3], ("docker", "exec", "-it"))
        self.assertIn("timeout", argv)
        self.assertIn("-nic", argv)
        self.assertIn("none", argv)
        self.assertIn("-no-reboot", argv)
        self.assertIn("-accel", argv)
        self.assertIn("tcg", argv)

    def test_rockos_command_is_bounded_and_device_free(self) -> None:
        probe = self.probe_module()
        argv = probe.rockos_argv(
            "debian@10.100.19.200", Path("/tmp/asterinas-qemu-probe/a/kernel.Image"),
            Path("/tmp/asterinas-qemu-probe/a/initramfs.cpio"), 210,
        )
        self.assertEqual(argv[:2], ("ssh", "-tt"))
        command = argv[-1]
        self.assertIn("timeout -k 5 210", command)
        self.assertIn("nice -n 10", command)
        self.assertIn("-nic none", command)
        self.assertIn("-no-reboot", command)
        self.assertIn("-accel tcg", command)


class RockOsArtifactCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = importlib.import_module("tools.riscv.dual_host_qemu_probe")

    def fake_transport(self, initial: dict[Path, object]):
        probe = self.probe

        class FakeTransport:
            def __init__(self):
                self.files = dict(initial)
                self.calls = []

            def ensure_directory(self, path):
                self.calls.append(("mkdir", path))

            def identity(self, path):
                self.calls.append(("identity", path))
                return self.files.get(path)

            def copy(self, source, destination):
                self.calls.append(("copy", source, destination))
                self.files[destination] = probe.read_identity(source)

            def promote(self, partial, destination):
                self.calls.append(("promote", partial, destination))
                self.files[destination] = self.files[partial]
                del self.files[partial]

            def remove_partial(self, path):
                self.calls.append(("remove", path))
                self.files.pop(path, None)

        return FakeTransport()

    def test_cached_correct_bytes_skip_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "kernel.Image"
            source.write_bytes(b"kernel")
            expected = self.probe.read_identity(source)
            destination = self.probe.cache_directory(expected, expected) / "kernel.Image"
            transport = self.fake_transport({destination: expected})
            actual = self.probe.stage_artifact(
                transport, source, expected, destination, nonce="0123456789abcdef"
            )
            self.assertEqual(actual, destination)
            self.assertFalse(any(call[0] == "copy" for call in transport.calls))

    def test_missing_bytes_copy_partial_verify_and_promote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "initramfs.cpio"
            source.write_bytes(b"stage1")
            expected = self.probe.read_identity(source)
            destination = self.probe.cache_directory(expected, expected) / "initramfs.cpio"
            transport = self.fake_transport({})
            actual = self.probe.stage_artifact(
                transport, source, expected, destination, nonce="0123456789abcdef"
            )
            self.assertEqual(actual, destination)
            self.assertEqual(transport.files[destination], expected)
            self.assertEqual([call[0] for call in transport.calls].count("copy"), 1)
            self.assertEqual([call[0] for call in transport.calls].count("promote"), 1)

    def test_wrong_cached_bytes_fail_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "kernel.Image"
            source.write_bytes(b"kernel")
            expected = self.probe.read_identity(source)
            wrong = self.probe.ArtifactIdentity(6, "0" * 64)
            destination = self.probe.cache_directory(expected, expected) / "kernel.Image"
            transport = self.fake_transport({destination: wrong})
            with self.assertRaisesRegex(ValueError, "cached artifact identity mismatch"):
                self.probe.stage_artifact(
                    transport, source, expected, destination,
                    nonce="0123456789abcdef",
                )
            self.assertEqual(transport.files[destination], wrong)
            self.assertFalse(any(call[0] == "copy" for call in transport.calls))

    def test_ssh_identity_command_rejects_symlinks(self) -> None:
        argv = self.probe.remote_identity_argv(
            "debian@10.100.19.200",
            Path("/tmp/asterinas-qemu-probe/a/kernel.Image"),
        )
        self.assertIn("test -L", argv[-1])
        self.assertIn("sha256sum", argv[-1])
        self.assertIn("stat -c", argv[-1])

    def test_remote_cache_path_rejects_parent_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe RockOS probe cache path"):
            self.probe.remote_identity_argv(
                "debian@10.100.19.200",
                Path("/tmp/asterinas-qemu-probe/a/../../boot/kernel.Image"),
            )


FAKE_PROBE_GUEST = r'''
import re
import sys
print("ASTERINAS_PROBE_READY v=1 pid=1", flush=True)
request = sys.stdin.readline()
match = re.fullmatch(r"ASTERINAS_PROBE_RUN v=1 nonce=([0-9a-f]{32}) probes=boot,syscall213 shell=0\n", request)
if match is None:
    sys.exit(3)
nonce = match.group(1)
print(f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq=0 name=boot", flush=True)
print(f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq=0 name=boot detail=boot-ok", flush=True)
print(f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq=1 name=syscall213", flush=True)
print(f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq=1 name=syscall213 detail=syscall-ok", flush=True)
print(f"ASTERINAS_PROBE_DONE v=1 nonce={nonce} count=2 status=pass", flush=True)
print(f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}", flush=True)
reboot = sys.stdin.readline()
if reboot != f"ASTERINAS_PROBE_REBOOT v=1 nonce={nonce}\n":
    sys.exit(4)
'''


class DualHostQemuSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = importlib.import_module("tools.riscv.dual_host_qemu_probe")

    def test_nonce_bound_probe_and_reboot_require_clean_exit(self) -> None:
        result = self.probe.run_session(
            (sys.executable, "-u", "-c", FAKE_PROBE_GUEST),
            nonce="00112233445566778899aabbccddeeff", seconds=5,
        )
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.phase, "qemu-exited")
        self.assertEqual(result.exit_status, 0)
        self.assertIn(b"ASTERINAS_PROBE_DONE", result.serial)

    def test_guest_that_never_reaches_ready_fails_with_phase(self) -> None:
        result = self.probe.run_session(
            (sys.executable, "-u", "-c", "import time; time.sleep(10)"),
            nonce="00112233445566778899aabbccddeeff", seconds=1,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.phase, "waiting-ready")
        self.assertIn("not seen", result.reason)

    def test_developer_and_rockos_evidence_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = self.probe.ArtifactIdentity(6, hashlib.sha256(b"kernel").hexdigest())
            developer = self.probe.SessionEvidence(
                True, "probe-pass", "qemu-exited", b"developer serial\n", 1.2, 0
            )
            rockos = self.probe.SessionEvidence(
                False, "QEMU timeout", "waiting-ready", b"rockos serial\n", 3.4, -15
            )
            self.probe.publish_evidence(
                root, "developer-qemu", developer, identity, identity,
                "00112233445566778899aabbccddeeff",
            )
            self.probe.publish_evidence(
                root, "rockos-qemu-tcg", rockos, identity, identity,
                "ffeeddccbbaa99887766554433221100",
            )
            self.assertEqual(
                (root / "developer-qemu" / "serial.log").read_bytes(),
                b"developer serial\n",
            )
            self.assertEqual(
                (root / "rockos-qemu-tcg" / "serial.log").read_bytes(),
                b"rockos serial\n",
            )
            result = json.loads((root / "rockos-qemu-tcg" / "result.json").read_text())
            self.assertFalse(result["passed"])
            self.assertEqual(result["host"], "rockos-qemu-tcg")
            self.assertEqual(result["kernel_sha256"], identity.sha256)
            self.assertEqual(result["selected_probes"], ["boot", "syscall213"])


if __name__ == "__main__":
    unittest.main()
