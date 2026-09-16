#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the two-host diskless RISC-V probe."""

from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


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

    def test_artifact_over_64_mib_is_rejected_before_reading(self) -> None:
        probe = self.probe_module()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "oversize.Image"
            with artifact.open("wb") as stream:
                stream.seek(probe.MAX_ARTIFACT_BYTES)
                stream.write(b"x")
            with self.assertRaisesRegex(ValueError, "below 64 MiB"):
                probe.read_identity(artifact)

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
            "asterinas-dev-v1-test",
            Path("/root/asterinas/kernel.Image"),
            Path("/root/asterinas/initramfs.cpio"),
            210,
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
            "debian@10.100.19.200",
            Path("/tmp/asterinas-qemu-probe/a/kernel.Image"),
            Path("/tmp/asterinas-qemu-probe/a/initramfs.cpio"),
            210,
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
            destination = (
                self.probe.cache_directory(expected, expected) / "kernel.Image"
            )
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
            destination = (
                self.probe.cache_directory(expected, expected) / "initramfs.cpio"
            )
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
            destination = (
                self.probe.cache_directory(expected, expected) / "kernel.Image"
            )
            transport = self.fake_transport({destination: wrong})
            with self.assertRaisesRegex(
                ValueError, "cached artifact identity mismatch"
            ):
                self.probe.stage_artifact(
                    transport,
                    source,
                    expected,
                    destination,
                    nonce="0123456789abcdef",
                )
            self.assertEqual(transport.files[destination], wrong)
            self.assertFalse(any(call[0] == "copy" for call in transport.calls))

    def test_bad_partial_transfer_is_cleaned_without_promoting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "kernel.Image"
            source.write_bytes(b"kernel")
            expected = self.probe.read_identity(source)
            destination = (
                self.probe.cache_directory(expected, expected) / "kernel.Image"
            )
            transport = self.fake_transport({})

            def wrong_copy(_source, partial):
                transport.calls.append(("copy", _source, partial))
                transport.files[partial] = self.probe.ArtifactIdentity(6, "0" * 64)

            transport.copy = wrong_copy
            with self.assertRaisesRegex(
                ValueError, "transferred artifact identity mismatch"
            ):
                self.probe.stage_artifact(
                    transport, source, expected, destination, nonce="0123456789abcdef"
                )
            self.assertNotIn(destination, transport.files)
            self.assertFalse(any(call[0] == "promote" for call in transport.calls))
            removals = [call for call in transport.calls if call[0] == "remove"]
            self.assertEqual(len(removals), 1)
            self.assertEqual(removals[0][1].parent, destination.parent)

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


FAKE_PROBE_GUEST = r"""
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
"""


class DualHostQemuSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = importlib.import_module("tools.riscv.dual_host_qemu_probe")

    def test_nonce_bound_probe_and_reboot_require_clean_exit(self) -> None:
        result = self.probe.run_session(
            (sys.executable, "-u", "-c", FAKE_PROBE_GUEST),
            nonce="00112233445566778899aabbccddeeff",
            seconds=5,
        )
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.phase, "qemu-exited")
        self.assertEqual(result.exit_status, 0)
        self.assertIn(b"ASTERINAS_PROBE_DONE", result.serial)

    def test_guest_that_never_reaches_ready_fails_with_phase(self) -> None:
        result = self.probe.run_session(
            (sys.executable, "-u", "-c", "import time; time.sleep(10)"),
            nonce="00112233445566778899aabbccddeeff",
            seconds=1,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.phase, "waiting-ready")
        self.assertIn("not seen", result.reason)

    def test_panic_noise_cannot_be_published_as_probe_pass(self) -> None:
        result = self.probe.run_session(
            (
                sys.executable,
                "-u",
                "-c",
                "print('kernel panic', flush=True)\n" + FAKE_PROBE_GUEST,
            ),
            nonce="00112233445566778899aabbccddeeff",
            seconds=5,
        )
        self.assertFalse(result.passed)
        self.assertIn("panic", result.reason)

    def test_panic_after_reboot_request_cannot_be_published_as_pass(self) -> None:
        result = self.probe.run_session(
            (
                sys.executable,
                "-u",
                "-c",
                FAKE_PROBE_GUEST
                + "\nprint('kernel panic after reboot request', flush=True)\n",
            ),
            nonce="00112233445566778899aabbccddeeff",
            seconds=5,
        )
        self.assertFalse(result.passed)
        self.assertIn("panic", result.reason)

    def test_protocol_replay_after_reboot_request_cannot_pass(self) -> None:
        result = self.probe.run_session(
            (
                sys.executable,
                "-u",
                "-c",
                FAKE_PROBE_GUEST
                + "\nprint(f'ASTERINAS_PROBE_DONE v=1 nonce={nonce} count=2 status=pass', flush=True)\n",
            ),
            nonce="00112233445566778899aabbccddeeff",
            seconds=5,
        )
        self.assertFalse(result.passed)
        self.assertIn("replayed terminal", result.reason)

    def test_developer_and_rockos_evidence_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = self.probe.ArtifactIdentity(
                6, hashlib.sha256(b"kernel").hexdigest()
            )
            developer = self.probe.SessionEvidence(
                True, "probe-pass", "qemu-exited", b"developer serial\n", 1.2, 0
            )
            rockos = self.probe.SessionEvidence(
                False, "QEMU timeout", "waiting-ready", b"rockos serial\n", 3.4, -15
            )
            self.probe.publish_evidence(
                root,
                "developer-qemu",
                developer,
                identity,
                identity,
                "00112233445566778899aabbccddeeff",
            )
            self.probe.publish_evidence(
                root,
                "rockos-qemu-tcg",
                rockos,
                identity,
                identity,
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


class DualHostQemuCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = importlib.import_module("tools.riscv.dual_host_qemu_probe")

    def test_cli_requires_explicit_artifacts_and_rockos_target(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as captured:
                self.probe.parse_args(())
        self.assertEqual(captured.exception.code, 2)

    def test_cli_rejects_unsafe_rockos_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kernel = Path(directory) / "kernel.Image"
            initramfs = Path(directory) / "initramfs.cpio"
            kernel.write_bytes(b"kernel")
            initramfs.write_bytes(b"stage1")
            with self.assertRaisesRegex(ValueError, "unsafe RockOS SSH target"):
                self.probe.parse_args(
                    (
                        "--kernel",
                        str(kernel),
                        "--initramfs",
                        str(initramfs),
                        "--rockos",
                        "debian@10.100.19.200;reboot",
                    )
                )

    def test_host_artifact_must_be_in_the_mounted_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            outside = Path(directory) / "other.Image"
            outside.write_bytes(b"kernel")
            with self.assertRaisesRegex(ValueError, "mounted worktree"):
                self.probe.container_path(outside, workspace)

    def test_host_evidence_defaults_to_ignored_user_owned_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = self.probe.default_output_root(
                workspace, timestamp="20260915T221800", suffix="abcdef12"
            )
            self.assertEqual(
                root,
                workspace
                / "target-ubuntu/dual-host-qemu-probe/runs/20260915T221800-abcdef12",
            )

    def test_snapshot_rejects_source_replacement_after_initial_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            kernel = workspace / "kernel.Image"
            initramfs = workspace / "initramfs.cpio"
            kernel.write_bytes(b"first-kernel")
            initramfs.write_bytes(b"stage1")
            kernel_identity = self.probe.read_identity(kernel)
            initramfs_identity = self.probe.read_identity(initramfs)
            kernel.write_bytes(b"other-kernel")
            with self.assertRaisesRegex(ValueError, "snapshot identity mismatch"):
                self.probe.snapshot_artifacts(
                    workspace,
                    kernel,
                    kernel_identity,
                    initramfs,
                    initramfs_identity,
                    nonce="0123456789abcdef",
                )

    def test_snapshot_exposes_verified_worktree_bytes_to_both_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            kernel = workspace / "kernel.Image"
            initramfs = workspace / "initramfs.cpio"
            kernel.write_bytes(b"first-kernel")
            initramfs.write_bytes(b"stage1")
            kernel_identity = self.probe.read_identity(kernel)
            initramfs_identity = self.probe.read_identity(initramfs)
            snap_kernel, snap_initramfs = self.probe.snapshot_artifacts(
                workspace,
                kernel,
                kernel_identity,
                initramfs,
                initramfs_identity,
                nonce="0123456789abcdef",
            )
            kernel.write_bytes(b"other-kernel")
            self.assertEqual(self.probe.read_identity(snap_kernel), kernel_identity)
            self.assertEqual(
                self.probe.read_identity(snap_initramfs), initramfs_identity
            )
            self.assertEqual(
                self.probe.container_path(snap_kernel, workspace),
                Path(
                    "/root/asterinas/target-ubuntu/dual-host-qemu-probe/snapshots/0123456789abcdef/kernel.Image"
                ),
            )

    def test_one_failed_host_makes_dual_run_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kernel = root / "kernel.Image"
            initramfs = root / "initramfs.cpio"
            kernel.write_bytes(b"kernel")
            initramfs.write_bytes(b"stage1")
            developer = self.probe.SessionEvidence(
                True, "probe-pass", "qemu-exited", b"ok\n", 1.0, 0
            )
            rockos = self.probe.SessionEvidence(
                False, "timeout", "waiting-ready", b"slow\n", 2.0, 124
            )
            with (
                patch.object(
                    self.probe,
                    "container_path",
                    return_value=Path("/root/asterinas/artifact"),
                ),
                patch.object(
                    self.probe,
                    "discover_container",
                    return_value="asterinas-dev-v1-test",
                ),
                patch.object(self.probe, "RockOsArtifactTransport"),
                patch.object(
                    self.probe,
                    "stage_artifact",
                    return_value=Path("/tmp/asterinas-qemu-probe/a/kernel.Image"),
                ),
                patch.object(
                    self.probe, "run_session", side_effect=(developer, rockos)
                ),
            ):
                status = self.probe.main(
                    (
                        "--kernel",
                        str(kernel),
                        "--initramfs",
                        str(initramfs),
                        "--rockos",
                        "debian@10.100.19.200",
                        "--output-directory",
                        str(root / "evidence"),
                    )
                )
            self.assertEqual(status, 1)
            self.assertTrue((root / "evidence/developer-qemu/result.json").is_file())
            self.assertTrue((root / "evidence/rockos-qemu-tcg/result.json").is_file())


if __name__ == "__main__":
    unittest.main()
