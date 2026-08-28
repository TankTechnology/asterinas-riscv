"""Fast contracts for the simulation-first Megrez debug workflow."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv import megrez_debug as debug_module
from tools.riscv.megrez_debug_contract import (
    DEBIAN_BROWSER_ARTIFACT_ORDER,
    DEBIAN_BROWSER_MARKERS,
    MAX_ARTIFACT_BYTES,
    ROOT_IMAGE_BYTES,
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debug_simulation import SimulationError, simulate_fast
from tools.riscv.megrez_debug_probe import (
    PROBE_STRESS_BYTES,
    PROBE_STRESS_SIZES,
    ProbeServer,
)
from tools.riscv.megrez_debug_board import (
    BoardRunConfig,
    BoardRunFailure,
    BoardTermination,
    BoardTransport,
    BoardTransportError,
    KERNEL_COMPRESSED_ADDRESS,
    RealBoardOperations,
    ensure_board_artifacts,
    run_board,
)

REPOSITORY_ROOT = Path(__file__).parents[3]


class MegrezDebugProbeServerTests(unittest.TestCase):
    def test_server_returns_exact_probe_response_and_releases_port(self) -> None:
        with ProbeServer(host="127.0.0.1", port=0, payload_bytes=None) as server:
            address = server.address
            with socket.create_connection(address, timeout=1.0) as connection:
                connection.sendall(
                    b"GET /asterinas-probe HTTP/1.0\r\n"
                    b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
                )
                response = bytearray()
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
            with socket.create_connection(address, timeout=1.0) as connection:
                connection.sendall(b"GET /wrong HTTP/1.0\r\n\r\n")
                not_found = connection.recv(4096)

        self.assertEqual(
            bytes(response),
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 23\r\n"
            b"Connection: close\r\n\r\n"
            b"ASTERINAS_TCP_PROBE_OK\n",
        )
        self.assertEqual(
            not_found,
            b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rebound:
            rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rebound.bind(address)

    def test_server_streams_a_deterministic_payload_beyond_one_rx_ring(self) -> None:
        payload_sizes = (16 * 1024, 64 * 1024, 128 * 1024)
        with ProbeServer(
            host="127.0.0.1", port=0, payload_sizes=payload_sizes
        ) as server:
            with socket.create_connection(server.address, timeout=1.0) as connection:
                connection.sendall(b"GET /asterinas-probe/65536 HTTP/1.0\r\n\r\n")
                self.assertTrue(connection.recv(4096).startswith(b"HTTP/1.1 404"))
            for payload_bytes in payload_sizes:
                with socket.create_connection(
                    server.address, timeout=1.0
                ) as connection:
                    connection.sendall(
                        f"GET /asterinas-probe/{payload_bytes} HTTP/1.0\r\n".encode()
                        + b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
                    )
                    response = bytearray()
                    while True:
                        chunk = connection.recv(64 * 1024)
                        if not chunk:
                            break
                        response.extend(chunk)

                header, payload = bytes(response).split(b"\r\n\r\n", 1)
                self.assertIn(f"Content-Length: {payload_bytes}".encode(), header)
                self.assertEqual(len(payload), payload_bytes)
                self.assertEqual(
                    payload, bytes(index % 251 for index in range(payload_bytes))
                )

        self.assertEqual(PROBE_STRESS_BYTES, 16 * 1024 * 1024)
        self.assertEqual(
            PROBE_STRESS_SIZES,
            (16 * 1024, 64 * 1024, 1024 * 1024, 16 * 1024 * 1024),
        )

    def test_server_records_bounded_tcp_info_for_a_stalled_reader(self) -> None:
        payload_bytes = 16 * 1024 * 1024
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        try:
            with ProbeServer(
                host="127.0.0.1",
                port=0,
                payload_sizes=(payload_bytes,),
            ) as server:
                client.settimeout(1.0)
                client.connect(server.address)
                client.sendall(
                    f"GET /asterinas-probe/{payload_bytes} HTTP/1.0\r\n\r\n".encode()
                )
                deadline = time.monotonic() + 1.0
                trace = server.trace_snapshot(plan_sha256="0" * 64)
                while (
                    not trace["connections"]
                    or len(trace["connections"][0]["samples"]) < 3
                    or trace["connections"][0]["payload_bytes_accepted"] < 4096
                    or not any(
                        sample["snd_wnd"] == 0
                        for sample in trace["connections"][0]["samples"]
                    )
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                    trace = server.trace_snapshot(plan_sha256="0" * 64)
        finally:
            client.close()

        trace = server.trace_snapshot(plan_sha256="0" * 64)
        encoded = server.canonical_trace(plan_sha256="0" * 64)

        self.assertEqual(json.loads(encoded), trace)
        self.assertEqual(trace["schema_version"], 1)
        self.assertEqual(trace["plan_sha256"], "0" * 64)
        self.assertEqual(len(trace["connections"]), 1)
        connection = trace["connections"][0]
        self.assertEqual(connection["requested_bytes"], payload_bytes)
        self.assertLessEqual(len(connection["samples"]), 4096)
        self.assertGreater(len(connection["samples"]), 0)
        timestamps = [sample["monotonic_us"] for sample in connection["samples"]]
        bytes_acked = [sample["bytes_acked"] for sample in connection["samples"]]
        bytes_sent = [sample["bytes_sent"] for sample in connection["samples"]]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(bytes_acked, sorted(bytes_acked))
        self.assertEqual(bytes_sent, sorted(bytes_sent))
        self.assertIn("unacked", connection["samples"][-1])
        self.assertIn("snd_cwnd", connection["samples"][-1])
        self.assertIn("snd_wnd", connection["samples"][-1])
        self.assertTrue(
            any(
                sample["unacked"] > 0
                or sample["bytes_sent"] > sample["bytes_acked"]
                or sample["snd_wnd"] == 0
                for sample in connection["samples"]
            )
        )

        with self.assertRaisesRegex(ValueError, "plan SHA-256"):
            server.trace_snapshot(plan_sha256="not-a-hash")


class MegrezDebugArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_identity_hashes_one_held_regular_file(self) -> None:
        artifact = self.directory / "kernel"
        replacement = self.directory / "replacement"
        payload = b"asterinas-megrez-kernel"
        artifact.write_bytes(payload)
        replacement.write_bytes(b"different-path-bytes")
        original_open = Path.open
        open_count = 0

        def replace_after_open(path: Path, *args: object, **kwargs: object):
            nonlocal open_count
            stream = original_open(path, *args, **kwargs)
            open_count += 1
            os.replace(replacement, artifact)
            return stream

        with mock.patch.object(Path, "open", new=replace_after_open):
            identity = ArtifactIdentity.from_path("kernel", artifact, 0x80200000)

        self.assertEqual(open_count, 1)
        self.assertEqual(identity.name, "kernel")
        self.assertEqual(identity.path, str(artifact.absolute()))
        self.assertEqual(identity.load_address, 0x80200000)
        self.assertEqual(identity.size, len(payload))
        self.assertEqual(identity.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(identity.crc32, f"{zlib.crc32(payload):08x}")
        self.assertEqual(artifact.read_bytes(), b"different-path-bytes")

    def test_identity_rejects_non_regular_and_out_of_bounds_inputs(self) -> None:
        empty = self.directory / "empty"
        empty.touch()
        directory = self.directory / "directory"
        directory.mkdir()
        target = self.directory / "target"
        target.write_bytes(b"target")
        symlink = self.directory / "symlink"
        symlink.symlink_to(target)
        oversized = self.directory / "oversized"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_ARTIFACT_BYTES + 1)

        for path, message in (
            (empty, "empty"),
            (directory, "regular non-symlink"),
            (symlink, "regular non-symlink"),
            (oversized, "64 MiB"),
        ):
            with (
                self.subTest(path=path.name),
                self.assertRaisesRegex(DebugContractError, message),
            ):
                ArtifactIdentity.from_path("kernel", path, 0x80200000)

    def test_identity_rejects_invalid_name_and_address(self) -> None:
        artifact = self.directory / "artifact"
        artifact.write_bytes(b"data")

        for name, address in (
            ("other", 0x80200000),
            ("kernel", 0),
            ("kernel", 0x80200001),
            ("kernel", True),
        ):
            with (
                self.subTest(name=name, address=address),
                self.assertRaises(DebugContractError),
            ):
                ArtifactIdentity.from_path(name, artifact, address)

    def test_identity_rejects_a_different_inode_opened_after_lstat(self) -> None:
        artifact = self.directory / "artifact"
        other = self.directory / "other"
        artifact.write_bytes(b"original")
        other.write_bytes(b"other")
        original_open = Path.open

        def open_other(_path: Path, *args: object, **kwargs: object):
            return original_open(other, *args, **kwargs)

        with (
            mock.patch.object(Path, "open", new=open_other),
            self.assertRaisesRegex(DebugContractError, "identity changed"),
        ):
            ArtifactIdentity.from_path("kernel", artifact, 0x80200000)

    def test_root_image_identity_requires_one_exact_gibibyte_and_zero_address(
        self,
    ) -> None:
        root_image = self.directory / "debian-root.ext2"
        with root_image.open("wb") as stream:
            stream.truncate(ROOT_IMAGE_BYTES)

        identity = ArtifactIdentity.from_path("root_image", root_image, 0)

        self.assertEqual(identity.size, ROOT_IMAGE_BYTES)
        self.assertEqual(identity.load_address, 0)
        for invalid in (
            replace(identity, size=ROOT_IMAGE_BYTES - 4096),
            replace(identity, size=ROOT_IMAGE_BYTES + 4096),
            replace(identity, load_address=0x80200000),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(DebugContractError):
                invalid.validate()


class MegrezDebugPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        self.artifacts = tuple(
            self._artifact(name, addresses[name])
            for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb")
        )

    def _artifact(self, name: str, address: int) -> ArtifactIdentity:
        path = self.directory / name
        path.write_bytes(f"{name}-bytes".encode())
        return ArtifactIdentity.from_path(name, path, address)

    def _plan(self) -> DebugPlan:
        return DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=self.artifacts,
            bootargs=(
                "cpu_no_boost_1_6ghz loglevel=info init=/init "
                "asterinas.reboot_after=180"
            ),
            smp=4,
            sv39=True,
            markers=(
                "Enter riscv_boot",
                "Presented by the Asterinas developers",
                "ASTERINAS_GMAC_TCP_PROBE_READY",
            ),
            reboot_after=180,
        )

    def test_plan_round_trip_is_canonical_and_hash_bound(self) -> None:
        plan = self._plan()
        encoded = plan.canonical_bytes()

        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded, DebugPlan.from_bytes(encoded).canonical_bytes())
        self.assertEqual(plan.plan_sha256, hashlib.sha256(encoded).hexdigest())
        self.assertEqual(
            tuple(artifact.name for artifact in plan.artifacts),
            ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"),
        )
        self.assertEqual(plan.smp, 4)
        self.assertIs(plan.sv39, True)

    def test_plan_rejects_duplicate_keys_at_every_depth(self) -> None:
        valid = json.loads(self._plan().canonical_bytes())
        artifact = json.dumps(valid["artifacts"][0], separators=(",", ":"))
        duplicate_top = (
            self._plan()
            .canonical_bytes()
            .decode()
            .replace('{"artifacts":', '{"schema_version":1,"artifacts":', 1)
        )
        duplicate_nested_artifact = artifact.replace(
            '{"crc32":', '{"name":"kernel","crc32":', 1
        )
        nested = (
            self._plan()
            .canonical_bytes()
            .decode()
            .replace(artifact, duplicate_nested_artifact, 1)
        )

        for encoded in (duplicate_top.encode(), nested.encode()):
            with self.assertRaisesRegex(DebugContractError, "duplicate JSON key"):
                DebugPlan.from_bytes(encoded)

    def test_plan_rejects_wrong_architecture_and_unsafe_values(self) -> None:
        plan = self._plan()
        invalid = (
            replace(plan, schema_version=True),
            replace(plan, smp=2),
            replace(plan, sv39=False),
            replace(plan, reboot_after=True),
            replace(plan, reboot_after=0),
            replace(plan, bootargs="init=/init; saveenv"),
            replace(plan, markers=()),
            replace(plan, markers=("same", "same")),
            replace(plan, artifacts=tuple(reversed(plan.artifacts))),
            replace(
                plan,
                artifacts=(replace(plan.artifacts[0], sha256="0" * 63),)
                + plan.artifacts[1:],
            ),
            replace(
                plan,
                artifacts=(replace(plan.artifacts[0], crc32="xyzxyzxy"),)
                + plan.artifacts[1:],
            ),
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(DebugContractError):
                value.validate()

    def test_plan_loader_rejects_unknown_missing_and_wrongly_typed_fields(self) -> None:
        payload = json.loads(self._plan().canonical_bytes())
        variants: list[dict[str, object]] = []
        unknown = dict(payload)
        unknown["unknown"] = 1
        variants.append(unknown)
        missing = dict(payload)
        del missing["profile"]
        variants.append(missing)
        wrong_type = dict(payload)
        wrong_type["markers"] = "marker"
        variants.append(wrong_type)

        for value in variants:
            with self.assertRaises(DebugContractError):
                DebugPlan.from_bytes(json.dumps(value).encode())

    def test_stage_result_round_trip_binds_the_plan_hash(self) -> None:
        plan = self._plan()
        result = StageResult(
            schema_version=1,
            stage="fast",
            passed=True,
            reason="pass",
            plan_sha256=plan.plan_sha256,
            evidence=("serial.log", "result.json"),
        )

        encoded = result.canonical_bytes()

        self.assertEqual(result, StageResult.from_bytes(encoded))
        self.assertTrue(encoded.endswith(b"\n"))
        with self.assertRaises(DebugContractError):
            replace(result, plan_sha256="f" * 63).validate()


class MegrezDebugDebianPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        sizes = {
            "root_image": ROOT_IMAGE_BYTES,
        }
        self.artifacts = tuple(
            ArtifactIdentity(
                name=name,
                path=str((self.directory / name).absolute()),
                load_address=addresses.get(name, 0),
                size=sizes.get(name, 4096),
                sha256=hashlib.sha256(name.encode()).hexdigest(),
                crc32=f"{zlib.crc32(name.encode()):08x}",
            )
            for name in DEBIAN_BROWSER_ARTIFACT_ORDER
        )

    def _plan(self) -> DebugPlan:
        return DebugPlan(
            schema_version=2,
            profile="debian-browser",
            artifacts=self.artifacts,
            bootargs=(
                "console=tty0 console=ttyS0 cpu_no_boost_1_6ghz "
                "loglevel=info init=/init "
                "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
                "asterinas.reboot_after=600 -- --root-init=systemd"
            ),
            smp=4,
            sv39=True,
            markers=DEBIAN_BROWSER_MARKERS,
            reboot_after=600,
        )

    def test_schema_two_round_trip_binds_every_debian_browser_input(self) -> None:
        plan = self._plan()
        encoded = plan.canonical_bytes()

        self.assertEqual(plan, DebugPlan.from_bytes(encoded))
        self.assertEqual(
            tuple(identity.name for identity in plan.artifacts),
            DEBIAN_BROWSER_ARTIFACT_ORDER,
        )
        self.assertEqual(plan.markers, DEBIAN_BROWSER_MARKERS)
        self.assertEqual(plan.plan_sha256, hashlib.sha256(encoded).hexdigest())

    def test_schema_two_rejects_a_narrower_or_reinterpreted_contract(self) -> None:
        plan = self._plan()
        root_index = DEBIAN_BROWSER_ARTIFACT_ORDER.index("root_image")
        invalid_artifacts = list(plan.artifacts)
        invalid_artifacts[root_index] = replace(
            invalid_artifacts[root_index], size=ROOT_IMAGE_BYTES - 4096
        )

        for invalid in (
            replace(plan, schema_version=1),
            replace(plan, profile="tcp-probe"),
            replace(plan, artifacts=plan.artifacts[:-1]),
            replace(plan, artifacts=tuple(reversed(plan.artifacts))),
            replace(plan, artifacts=tuple(invalid_artifacts)),
            replace(plan, markers=plan.markers[:-1]),
            replace(plan, smp=1),
            replace(plan, sv39=False),
            replace(plan, reboot_after=0),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(DebugContractError):
                invalid.validate()

    def test_schema_one_canonical_contract_remains_unchanged(self) -> None:
        legacy = MegrezDebugPlanTests()
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        plan = legacy._plan()

        self.assertEqual(plan.schema_version, 1)
        self.assertEqual(plan.profile, "tcp-probe")
        self.assertEqual(
            tuple(identity.name for identity in plan.artifacts),
            ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"),
        )
        self.assertEqual(plan, DebugPlan.from_bytes(plan.canonical_bytes()))


class MegrezDebugDebianPlanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.paths: dict[str, Path] = {}
        for name in DEBIAN_BROWSER_ARTIFACT_ORDER:
            path = self.directory / name
            if name == "root_image":
                with path.open("wb") as stream:
                    stream.truncate(ROOT_IMAGE_BYTES)
            else:
                path.write_bytes(f"{name}-payload\n".encode())
            self.paths[name] = path
        self.root_identity = ArtifactIdentity.from_path(
            "root_image", self.paths["root_image"], 0
        )
        self.package_rows = (
            ("bash", "riscv64", "5.2", hashlib.sha256(b"bash").hexdigest()),
        )
        self.paths["package_checksums"].write_text(
            "\t".join(self.package_rows[0]) + "\n",
            encoding="utf-8",
        )
        self.manifest = SimpleNamespace(
            profile="desktop-m5-network",
            root_image_sha256=self.root_identity.sha256,
            packages_lock_sha256=hashlib.sha256(
                self.paths["packages_lock"].read_bytes()
            ).hexdigest(),
            signed_metadata_sha256=hashlib.sha256(
                self.paths["in_release"].read_bytes()
            ).hexdigest(),
            downloaded_packages=self.package_rows,
        )

    def _arguments(self) -> SimpleNamespace:
        return SimpleNamespace(
            profile="debian-browser",
            kernel=self.paths["kernel"],
            initramfs=self.paths["initramfs"],
            qemu_dtb=self.paths["qemu_dtb"],
            megrez_dtb=self.paths["megrez_dtb"],
            u_boot=self.paths["u_boot"],
            root_image=self.paths["root_image"],
            root_manifest=self.paths["root_manifest"],
            packages_lock=self.paths["packages_lock"],
            package_checksums=self.paths["package_checksums"],
            in_release=self.paths["in_release"],
            bootargs=(
                "console=tty0 console=ttyS0 loglevel=info init=/init "
                "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
                "asterinas.reboot_after=600 -- --root-init=systemd"
            ),
            marker=None,
            reboot_after=600,
        )

    def test_create_plan_reuses_the_full_signed_rootfs_contract(self) -> None:
        with (
            mock.patch.object(
                debug_module, "load_manifest", return_value=self.manifest, create=True
            ) as load_manifest,
            mock.patch.object(
                debug_module,
                "validate_frozen_root",
                return_value=self.manifest,
                create=True,
            ) as validate_root,
            mock.patch.object(
                debug_module,
                "load_package_checksums",
                return_value=self.package_rows,
                create=True,
            ) as load_checksums,
        ):
            plan = debug_module._create_plan(self._arguments())

        self.assertEqual(plan.schema_version, 2)
        self.assertEqual(plan.profile, "debian-browser")
        self.assertEqual(plan.markers, DEBIAN_BROWSER_MARKERS)
        self.assertEqual(
            tuple(identity.name for identity in plan.artifacts),
            DEBIAN_BROWSER_ARTIFACT_ORDER,
        )
        load_manifest.assert_called_once_with(self.paths["root_manifest"])
        validate_root.assert_called_once_with(
            self.paths["root_image"], self.manifest, self.paths["packages_lock"]
        )
        load_checksums.assert_called_once_with(self.paths["package_checksums"])

    def test_create_plan_rejects_unbound_metadata_and_download_rows(self) -> None:
        mismatched_rows = (
            ("bash", "riscv64", "different", hashlib.sha256(b"bash").hexdigest()),
        )
        variants = (
            SimpleNamespace(
                **{
                    **vars(self.manifest),
                    "signed_metadata_sha256": "0" * 64,
                }
            ),
            SimpleNamespace(
                **{
                    **vars(self.manifest),
                    "packages_lock_sha256": "0" * 64,
                }
            ),
        )
        for manifest in variants:
            with (
                self.subTest(manifest=manifest),
                mock.patch.object(
                    debug_module, "load_manifest", return_value=manifest, create=True
                ),
                mock.patch.object(
                    debug_module,
                    "validate_frozen_root",
                    return_value=manifest,
                    create=True,
                ),
                mock.patch.object(
                    debug_module,
                    "load_package_checksums",
                    return_value=manifest.downloaded_packages,
                    create=True,
                ),
                self.assertRaises(debug_module.WorkflowError),
            ):
                debug_module._create_plan(self._arguments())

        with (
            mock.patch.object(
                debug_module, "load_manifest", return_value=self.manifest, create=True
            ),
            mock.patch.object(
                debug_module,
                "validate_frozen_root",
                return_value=self.manifest,
                create=True,
            ),
            mock.patch.object(
                debug_module,
                "load_package_checksums",
                return_value=mismatched_rows,
                create=True,
            ),
            self.assertRaises(debug_module.WorkflowError),
        ):
            debug_module._create_plan(self._arguments())


class MegrezDebugSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        self.output = self.repository / "target/qemu-uboot/megrez-debug/fast"
        self.build = self.repository / "target/qemu-uboot/megrez-debug/build"
        self.output.mkdir(parents=True)
        self.build.mkdir(parents=True)
        artifact_directory = self.repository / "artifacts"
        artifact_directory.mkdir()
        paths = {
            "kernel": artifact_directory / "kernel",
            "initramfs": artifact_directory / "initramfs",
            "qemu_dtb": artifact_directory / "qemu-virt.dtb",
            "megrez_dtb": artifact_directory / "megrez.dtb",
        }
        for name, path in paths.items():
            path.write_bytes(f"{name}-payload".encode())
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        self.plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(
                ArtifactIdentity.from_path(name, paths[name], addresses[name])
                for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb")
            ),
            bootargs=(
                "console=ttyS0 loglevel=info init=/init asterinas.reboot_after=180"
            ),
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=180,
        )

    def _runner(
        self,
        calls: list[tuple[tuple[str, ...], dict[str, object]]],
        *,
        generated_dtb: bytes = b"qemu_dtb-payload",
        passed: bool = True,
    ):
        def run(
            arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((tuple(arguments), kwargs))
            if arguments[-1] == "prepare":
                (self.output / "qemu-virt.dtb").write_bytes(generated_dtb)
                (self.output / "boot.ext4").write_bytes(b"boot-disk")
                (self.output / "artifacts.json").write_text("{}\n")
                (self.output / "qemu-dtb-audit.json").write_text("{}\n")
                (self.build / "u-boot").write_bytes(b"u-boot")
            else:
                result_path = Path(arguments[arguments.index("--result") + 1])
                result_path.write_text(
                    json.dumps(
                        {
                            "passed": passed,
                            "profile": "generic-sv39-smp4-tcp-probe",
                            "device_set": "virtio-net-slirp",
                            "status": "PASS" if passed else "FAIL",
                            "terminal_classification": "BOOT_COMPLETED",
                            "effective_bootargs": self.plan.bootargs,
                            "qemu_argv": [
                                "qemu-system-riscv64",
                                "-smp",
                                "4",
                                "-cpu",
                                (
                                    "rv64,sv48=false,svpbmt=true,zkr=true,"
                                    "svadu=false,svade=true"
                                ),
                                "-netdev",
                                "user,id=net0",
                                "-device",
                                "virtio-net-device,netdev=net0",
                            ],
                        }
                    )
                )
                (self.output / "serial.log").write_text("serial\n")
                (self.output / "marker-event.txt").write_text("marker\n")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        return run

    def test_fast_simulation_reuses_prepare_and_runner_then_binds_plan(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        result = simulate_fast(
            self.plan,
            self.output,
            self.build,
            run_command=self._runner(calls),
            repository_root=self.repository,
        )

        self.assertEqual(result.stage, "fast")
        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "fast-pass")
        self.assertEqual(result.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(
            result.evidence,
            (
                "serial.log",
                "marker-event.txt",
                "qemu-result.json",
                "qemu-dtb-audit.json",
            ),
        )
        self.assertEqual(len(calls), 2)
        prepare, prepare_options = calls[0]
        self.assertEqual(prepare[-1], "prepare")
        environment = prepare_options["env"]
        self.assertIsInstance(environment, dict)
        assert isinstance(environment, dict)
        self.assertEqual(
            environment["QEMU_UBOOT_PROFILE"],
            "generic-sv39-smp4-tcp-probe",
        )
        self.assertEqual(environment["QEMU_UBOOT_OUT_DIR"], str(self.output))
        self.assertEqual(environment["QEMU_UBOOT_BUILD_DIR"], str(self.build))
        self.assertIs(prepare_options["capture_output"], False)
        run, _run_options = calls[1]
        self.assertIn("generic-sv39-smp4-tcp-probe", run)
        self.assertEqual(
            run[run.index("--device-set") + 1],
            "virtio-net-slirp",
        )
        self.assertNotIn("--bootargs-override", run)
        self.assertEqual(run.count("--serial-log"), 1)
        self.assertEqual(Path(run[run.index("--result") + 1]).name, "qemu-result.json")

    def test_fast_simulation_invalidates_stale_results_before_prepare(self) -> None:
        for name in ("result.json", "qemu-result.json"):
            (self.output / name).write_text('{"passed":true}\n')

        def fail(
            arguments: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 7, "", "prepare failed")

        with self.assertRaisesRegex(SimulationError, "prepare-failed"):
            simulate_fast(
                self.plan,
                self.output,
                self.build,
                run_command=fail,
                repository_root=self.repository,
            )

        self.assertFalse((self.output / "result.json").exists())
        self.assertFalse((self.output / "qemu-result.json").exists())

    def test_fast_simulation_rejects_generated_dtb_drift_before_qemu(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        with self.assertRaisesRegex(SimulationError, "qemu-dtb-drift"):
            simulate_fast(
                self.plan,
                self.output,
                self.build,
                run_command=self._runner(calls, generated_dtb=b"changed-dtb"),
                repository_root=self.repository,
            )

        self.assertEqual(len(calls), 1)

    def test_fast_simulation_rejects_a_false_guarded_result(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        with self.assertRaisesRegex(SimulationError, "qemu-gate-failed"):
            simulate_fast(
                self.plan,
                self.output,
                self.build,
                run_command=self._runner(calls, passed=False),
                repository_root=self.repository,
            )

    def test_fast_simulation_rejects_malformed_result_and_output_symlink(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def malformed(
            arguments: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            completed = self._runner(calls)(arguments, **kwargs)
            if arguments[-1] != "prepare":
                (self.output / "qemu-result.json").write_text("not-json")
            return completed

        with self.assertRaisesRegex(SimulationError, "qemu-result-invalid"):
            simulate_fast(
                self.plan,
                self.output,
                self.build,
                run_command=malformed,
                repository_root=self.repository,
            )

        outside = self.repository / "outside"
        outside.mkdir()
        unsafe = self.repository / "target/qemu-uboot/unsafe"
        unsafe.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SimulationError, "simulation-output-unsafe"):
            simulate_fast(
                self.plan,
                unsafe,
                self.build,
                run_command=self._runner([]),
                repository_root=self.repository,
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_fast_simulation_propagates_interrupt_after_stale_invalidation(
        self,
    ) -> None:
        stale = self.output / "result.json"
        stale.write_text('{"passed":true}\n')

        def interrupted(
            _arguments: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            simulate_fast(
                self.plan,
                self.output,
                self.build,
                run_command=interrupted,
                repository_root=self.repository,
            )
        self.assertFalse(stale.exists())

    def test_simulate_cli_atomically_publishes_the_stage_result(self) -> None:
        plan_path = self.repository / "plan.json"
        plan_path.write_bytes(self.plan.canonical_bytes())
        expected = StageResult(
            schema_version=1,
            stage="fast",
            passed=True,
            reason="fast-pass",
            plan_sha256=self.plan.plan_sha256,
            evidence=("serial.log",),
        )
        from tools.riscv import megrez_debug

        events: list[str] = []

        class Probe:
            def __enter__(self):
                events.append("probe-enter")
                return self

            def __exit__(self, *_args: object) -> None:
                events.append("probe-exit")

        def simulate(*_args: object, **_kwargs: object) -> StageResult:
            events.append("simulate")
            return expected

        with mock.patch.object(megrez_debug, "simulate_fast", side_effect=simulate):
            status = megrez_debug.main(
                (
                    "simulate",
                    str(plan_path),
                    "--tier",
                    "fast",
                    "--output-directory",
                    str(self.output),
                    "--uboot-build-directory",
                    str(self.build),
                ),
                probe_server_factory=Probe,
            )

        self.assertEqual(status, 0)
        self.assertEqual(events, ["probe-enter", "simulate", "probe-exit"])
        self.assertEqual(
            StageResult.from_bytes((self.output / "result.json").read_bytes()),
            expected,
        )


class MegrezDebugBoardTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        artifacts = []
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = self.directory / name
            path.write_bytes(f"{name}-payload".encode())
            artifacts.append(ArtifactIdentity.from_path(name, path, addresses[name]))
        self.plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(artifacts),
            bootargs="loglevel=info init=/init asterinas.reboot_after=180",
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=180,
        )

    def test_all_ram_cache_hits_skip_every_xmodem_transfer(self) -> None:
        commands: list[tuple[str, float]] = []
        transfers: list[tuple[int, bytes, int]] = []
        identities = {item.name: item for item in self.plan.artifacts}
        by_address = {
            identities[name].load_address: identities[name]
            for name in ("kernel", "initramfs", "megrez_dtb")
        }

        def command(text: str, timeout: float) -> str:
            commands.append((text, timeout))
            address = int(text.split()[1], 16)
            identity = by_address[address]
            return f"{text}\r\nCRC32 for 0x{address:x} ... ==> {identity.crc32}\r\n=> "

        transport = BoardTransport(
            fd=7,
            command=command,
            transfer_payload=lambda fd, payload, address: transfers.append(
                (fd, payload, address)
            ),
        )

        outcomes = ensure_board_artifacts(self.plan, transport, timeout=2.5)

        self.assertEqual(tuple(item.status for item in outcomes), ("cache-hit",) * 3)
        self.assertEqual(transfers, [])
        self.assertEqual(len(commands), 3)
        for name, (text, timeout) in zip(
            ("kernel", "initramfs", "megrez_dtb"), commands
        ):
            identity = identities[name]
            self.assertEqual(
                text,
                f"crc32 0x{identity.load_address:x} 0x{identity.size:x}",
            )
            self.assertEqual(timeout, 2.5)

    def test_cache_miss_transfers_once_then_requires_matching_crc(self) -> None:
        identity = next(item for item in self.plan.artifacts if item.name == "kernel")
        resident_crc = "00000000"
        transfers: list[tuple[int, bytes, int]] = []
        commands: list[str] = []

        def command(text: str, _timeout: float) -> str:
            nonlocal resident_crc
            commands.append(text)
            if text.startswith("unzip "):
                resident_crc = identity.crc32
                return f"{text}\r\nUncompressed size: {identity.size}\r\n=> "
            return (
                f"{text}\r\nCRC32 for 0x{identity.load_address:x} ... "
                f"==> {resident_crc}\r\n=> "
            )

        def transfer(fd: int, payload: bytes, address: int) -> None:
            transfers.append((fd, payload, address))

        outcome = BoardTransport(
            fd=9, command=command, transfer_payload=transfer
        ).ensure(identity, timeout=3.0)

        self.assertEqual(outcome.status, "transferred-compressed")
        self.assertEqual(len(transfers), 1)
        fd, payload, address = transfers[0]
        self.assertEqual((fd, address), (9, KERNEL_COMPRESSED_ADDRESS))
        self.assertEqual(gzip.decompress(payload), Path(identity.path).read_bytes())
        self.assertEqual(
            commands[1],
            f"unzip 0x{KERNEL_COMPRESSED_ADDRESS:x} "
            f"0x{identity.load_address:x} 0x{identity.size:x}",
        )

    def test_malformed_or_still_mismatched_crc_fails_closed(self) -> None:
        identity = next(item for item in self.plan.artifacts if item.name == "kernel")

        for output, message in (
            ("=> ", "crc-result"),
            (
                f"CRC32 for 0x{identity.load_address:x} ... ==> 00000000\r\n=> ",
                "post-transfer-crc",
            ),
        ):
            with self.subTest(message=message):
                transport = BoardTransport(
                    fd=11,
                    command=lambda _text, _timeout, value=output: value,
                    transfer_payload=lambda _fd, _payload, _address: None,
                )
                with self.assertRaisesRegex(BoardTransportError, message):
                    transport.ensure(identity, timeout=1.0)


class MegrezDebugBoardStateTests(unittest.TestCase):
    class Operations:
        def __init__(self, chunks: list[str | BaseException]) -> None:
            self.chunks = list(chunks)
            self.calls: list[tuple[str, float | None]] = []
            self.booti_count = 0
            self.published: StageResult | None = None
            self.outcomes = (
                "kernel:cache-hit",
                "initramfs:cache-hit",
                "megrez_dtb:cache-hit",
            )

        def invalidate(self) -> None:
            self.calls.append(("invalidate", None))

        def open(self, timeout: float) -> None:
            self.calls.append(("open", timeout))

        def ensure_artifacts(self, _plan: DebugPlan, timeout: float) -> tuple[str, ...]:
            self.calls.append(("ensure", timeout))
            return self.outcomes

        def prepare_boot(self, _plan: DebugPlan, timeout: float) -> None:
            self.calls.append(("prepare", timeout))

        def booti(self, _plan: DebugPlan, timeout: float) -> None:
            self.calls.append(("booti", timeout))
            self.booti_count += 1

        def read_chunk(self, timeout: float) -> str:
            self.calls.append(("read", timeout))
            if not self.chunks:
                raise TimeoutError("no more serial data")
            value = self.chunks.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        def close(self) -> None:
            self.calls.append(("close", None))

        def evidence_names(self) -> tuple[str, ...]:
            return ("serial.log", "transport.json")

        def publish(
            self,
            result: StageResult,
            _transcript: str,
            _outcomes: tuple[str, ...],
        ) -> None:
            self.calls.append(("publish", None))
            self.published = result

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        directory = Path(self.temporary_directory.name)
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        artifacts = []
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = directory / name
            path.write_bytes(name.encode())
            artifacts.append(ArtifactIdentity.from_path(name, path, addresses[name]))
        self.plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(artifacts),
            bootargs="loglevel=info init=/init asterinas.reboot_after=180",
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=180,
        )

    @staticmethod
    def _clock():
        value = 100.0

        def monotonic() -> float:
            nonlocal value
            current = value
            value += 1.0
            return current

        return monotonic

    def test_success_uses_one_declining_budget_one_booti_and_recovery(self) -> None:
        operations = self.Operations(
            [
                "old text Enter ris",
                "cv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                "U-Boot recovered\n=> ",
            ]
        )

        result = run_board(
            self.plan,
            BoardRunConfig(timeout=300.0),
            operations,
            clock=self._clock(),
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "board-pass")
        self.assertEqual(result.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(operations.booti_count, 1)
        self.assertEqual(operations.published, result)
        self.assertEqual(operations.calls[-2:], [("close", None), ("publish", None)])
        budgets = [value for _name, value in operations.calls if value is not None]
        self.assertTrue(all(0 < value < 300 for value in budgets))
        self.assertEqual(budgets, sorted(budgets, reverse=True))

    def test_guest_failure_waits_for_a_fresh_uboot_recovery(self) -> None:
        operations = self.Operations(
            [
                "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_FAIL rea",
                "son=receive-poll errno=110 attempts=1 current_bytes=14600 ",
                "completed_bytes=0\npll config ok\nFirmware version:1.4\n",
                "U-Boot 2020.01\n=> ",
            ]
        )

        result = run_board(
            self.plan,
            BoardRunConfig(timeout=120.0),
            operations,
            clock=self._clock(),
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "guest-failure-recovered:receive-poll")
        self.assertEqual(operations.booti_count, 1)
        self.assertEqual(operations.published, result)

    def test_terminal_requires_post_terminal_recovery_and_is_unique(self) -> None:
        failure = (
            "ASTERINAS_GMAC_TCP_PROBE_FAIL reason=receive-poll errno=110 "
            "attempts=1 current_bytes=14600 completed_bytes=0\n"
        )
        cases = (
            (
                ["=> \nEnter riscv_boot\n" + failure],
                "recovery-not-observed",
            ),
            (
                ["Enter riscv_boot\n" + failure + failure],
                "guest-terminal-duplicate",
            ),
            (
                [
                    "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                    "ASTERINAS_GMAC_TCP_PROBE_READY\n=> ",
                ],
                "guest-terminal-duplicate",
            ),
        )
        for chunks, reason in cases:
            with self.subTest(reason=reason):
                operations = self.Operations(chunks)
                result = run_board(
                    self.plan,
                    BoardRunConfig(timeout=120.0),
                    operations,
                    clock=lambda: 10.0,
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, reason)
                self.assertEqual(operations.booti_count, 1)

    def test_marker_and_timeout_failures_never_retry_booti(self) -> None:
        cases = (
            (["ASTERINAS_GMAC_TCP_PROBE_READY\n"], "guest-marker-order"),
            ([], "kernel-timeout"),
            (["Enter riscv_boot\n"], "guest-timeout"),
            (
                ["Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n"],
                "recovery-not-observed",
            ),
        )
        for chunks, reason in cases:
            with self.subTest(reason=reason):
                operations = self.Operations(chunks)
                result = run_board(
                    self.plan,
                    BoardRunConfig(timeout=300.0),
                    operations,
                    clock=lambda: 10.0,
                )
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, reason)
                self.assertEqual(operations.booti_count, 1)
                self.assertEqual(operations.published, result)

    def test_first_termination_defers_through_close_and_publication(self) -> None:
        operations = self.Operations([BoardTermination(15)])

        with self.assertRaisesRegex(BoardTermination, "15"):
            run_board(
                self.plan,
                BoardRunConfig(timeout=300.0),
                operations,
                clock=lambda: 20.0,
            )

        self.assertEqual(operations.booti_count, 1)
        assert operations.published is not None
        self.assertFalse(operations.published.passed)
        self.assertEqual(operations.published.reason, "board-terminated-15")
        self.assertEqual(operations.calls[-2:], [("close", None), ("publish", None)])

    def test_termination_during_close_still_publishes_false(self) -> None:
        class TerminatingClose(self.Operations):
            def close(self) -> None:
                self.calls.append(("close", None))
                raise BoardTermination(15)

        operations = TerminatingClose(
            [
                "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                "U-Boot recovered\n=> ",
            ]
        )
        with self.assertRaisesRegex(BoardTermination, "15"):
            run_board(
                self.plan,
                BoardRunConfig(timeout=300.0),
                operations,
                clock=lambda: 20.0,
            )

        assert operations.published is not None
        self.assertFalse(operations.published.passed)
        self.assertEqual(operations.published.reason, "board-terminated-15")

    def test_termination_during_publication_retries_false_evidence(self) -> None:
        class TerminatingPublish(self.Operations):
            def __init__(self, chunks: list[str | BaseException]) -> None:
                super().__init__(chunks)
                self.publish_count = 0

            def publish(
                self,
                result: StageResult,
                transcript: str,
                outcomes: tuple[str, ...],
            ) -> None:
                self.publish_count += 1
                if self.publish_count == 1:
                    raise BoardTermination(15)
                super().publish(result, transcript, outcomes)

        operations = TerminatingPublish(
            [
                "Enter riscv_boot\nASTERINAS_GMAC_TCP_PROBE_READY\n",
                "U-Boot recovered\n=> ",
            ]
        )
        with self.assertRaisesRegex(BoardTermination, "15"):
            run_board(
                self.plan,
                BoardRunConfig(timeout=300.0),
                operations,
                clock=lambda: 20.0,
            )

        self.assertEqual(operations.publish_count, 2)
        assert operations.published is not None
        self.assertFalse(operations.published.passed)
        self.assertEqual(operations.published.reason, "board-terminated-15")

    def test_absolute_deadline_expires_without_starting_an_extra_read(self) -> None:
        operations = self.Operations(["unreachable"])
        times = iter((0.0, 0.0, 0.0, 0.0, 0.0, 301.0))

        result = run_board(
            self.plan,
            BoardRunConfig(timeout=300.0),
            operations,
            clock=lambda: next(times),
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "kernel-timeout")
        self.assertFalse(any(name == "read" for name, _value in operations.calls))

    def test_configuration_and_preboot_failure_forbid_booti(self) -> None:
        with self.assertRaisesRegex(ValueError, "300"):
            BoardRunConfig(timeout=301.0)

        class FailingOperations(self.Operations):
            def prepare_boot(self, _plan: DebugPlan, timeout: float) -> None:
                self.calls.append(("prepare", timeout))
                raise BoardRunFailure("uboot-prepare-failed")

        operations = FailingOperations([])
        result = run_board(
            self.plan,
            BoardRunConfig(timeout=300.0),
            operations,
            clock=lambda: 30.0,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "uboot-prepare-failed")
        self.assertEqual(operations.booti_count, 0)


class MegrezDebugBoardCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        artifacts = []
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = self.directory / name
            path.write_bytes(name.encode())
            artifacts.append(ArtifactIdentity.from_path(name, path, addresses[name]))
        self.plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(artifacts),
            bootargs="loglevel=info init=/init asterinas.reboot_after=180",
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=180,
        )
        self.plan_path = self.directory / "plan.json"
        self.plan_path.write_bytes(self.plan.canonical_bytes())
        self.simulation = self.directory / "fast-result.json"
        self.simulation.write_bytes(
            StageResult(
                schema_version=1,
                stage="fast",
                passed=True,
                reason="fast-pass",
                plan_sha256=self.plan.plan_sha256,
                evidence=("serial.log",),
            ).canonical_bytes()
        )
        self.output = self.directory / "board-output"

    def _arguments(self, *extra: str) -> tuple[str, ...]:
        return (
            "board",
            str(self.plan_path),
            "/dev/ttyUSB-test",
            "--simulation-result",
            str(self.simulation),
            *extra,
        )

    def test_board_cli_runs_one_physical_adapter_after_all_prechecks(self) -> None:
        from tools.riscv import megrez_debug

        expected = StageResult(
            schema_version=1,
            stage="board",
            passed=True,
            reason="board-pass",
            plan_sha256=self.plan.plan_sha256,
            evidence=("serial.log", "transport.json"),
        )
        events: list[str] = []

        class Probe:
            def __enter__(self):
                events.append("probe-enter")
                return self

            def __exit__(self, *_args: object) -> None:
                events.append("probe-exit")

            def canonical_trace(self, *, plan_sha256: str) -> bytes:
                return json.dumps(
                    {"schema_version": 1, "plan_sha256": plan_sha256}
                ).encode()

        def board(
            *_args: object,
            probe_trace_provider=None,
            **_kwargs: object,
        ) -> StageResult:
            events.append("board")
            self.assertIsNotNone(probe_trace_provider)
            trace = json.loads(probe_trace_provider(self.plan.plan_sha256))
            self.assertEqual(trace["plan_sha256"], self.plan.plan_sha256)
            return expected

        with mock.patch.object(
            megrez_debug, "run_physical_board", side_effect=board
        ) as run:
            status = megrez_debug.main(
                self._arguments(
                    "--output-directory",
                    str(self.output),
                    "--timeout",
                    "240",
                ),
                probe_server_factory=Probe,
            )

        self.assertEqual(status, 0)
        self.assertEqual(events, ["probe-enter", "board", "probe-exit"])
        run.assert_called_once_with(
            self.plan,
            "/dev/ttyUSB-test",
            self.output,
            timeout=240.0,
            probe_trace_provider=mock.ANY,
        )

    def test_board_cli_rejects_missing_output_and_artifact_drift_pre_serial(
        self,
    ) -> None:
        from tools.riscv import megrez_debug

        with mock.patch.object(megrez_debug, "run_physical_board") as run:
            missing_output = megrez_debug.main(self._arguments())
            Path(self.plan.artifacts[0].path).write_bytes(b"drift")
            drift = megrez_debug.main(
                self._arguments("--output-directory", str(self.output))
            )

        self.assertEqual(missing_output, 2)
        self.assertEqual(drift, 2)
        run.assert_not_called()

    def test_board_cli_maps_termination_to_signal_exit_status(self) -> None:
        from tools.riscv import megrez_debug

        with mock.patch.object(
            megrez_debug,
            "run_physical_board",
            side_effect=BoardTermination(15),
        ):
            status = megrez_debug.main(
                self._arguments("--output-directory", str(self.output)),
                probe_server_factory=nullcontext,
            )

        self.assertEqual(status, 143)


class MegrezDebugRealBoardOperationsTests(unittest.TestCase):
    def test_real_adapter_reuses_one_fd_and_publishes_result_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = repository / "target/megrez-debug/board"
            artifact_directory = repository / "artifacts"
            artifact_directory.mkdir()
            addresses = {
                "kernel": 0x80200000,
                "initramfs": 0x83000000,
                "qemu_dtb": 0xF0000000,
                "megrez_dtb": 0xF0000000,
            }
            artifacts = []
            for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
                path = artifact_directory / name
                path.write_bytes(name.encode())
                artifacts.append(
                    ArtifactIdentity.from_path(name, path, addresses[name])
                )
            plan = DebugPlan(
                schema_version=1,
                profile="tcp-probe",
                artifacts=tuple(artifacts),
                bootargs="loglevel=info init=/init asterinas.reboot_after=180",
                smp=4,
                sv39=True,
                markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
                reboot_after=180,
            )
            identities = {item.load_address: item for item in artifacts}
            identities[0xF0000000] = next(
                item for item in artifacts if item.name == "megrez_dtb"
            )
            commands: list[str] = []
            sends: list[str] = []
            closed: list[int] = []

            class Session:
                fd = 23

                def send(self, command: str) -> None:
                    sends.append(command)

                def wait_for_uboot_prompt(self, timeout: float) -> str:
                    self.log.write(f"prompt timeout={timeout}\n")
                    return "U-Boot\n=> "

                def command(self, command: str, timeout: float) -> str:
                    commands.append(command)
                    self.log.write(f"{command}\n")
                    if command.startswith("crc32 "):
                        address = int(command.split()[1], 16)
                        return (
                            f"{command}\r\nCRC32 for 0x{address:x} ... "
                            f"==> {identities[address].crc32}\r\n=> "
                        )
                    return f"{command}\r\n=> "

            def session_factory(
                fd: int,
                _log_path: str | None,
                *,
                confirm: bool,
                final_marker: str,
                log_stream,
            ):
                self.assertEqual(fd, 23)
                self.assertFalse(confirm)
                self.assertEqual(final_marker, plan.markers[-1])
                session = Session()
                session.log = log_stream
                return session

            operations = RealBoardOperations(
                plan,
                "/dev/fake",
                output,
                repository_root=repository,
                open_device=lambda _device: 23,
                lock_device=lambda _fd: None,
                close_device=closed.append,
                session_factory=session_factory,
                probe_trace_provider=lambda plan_sha256: (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "plan_sha256": plan_sha256,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
            )
            operations.invalidate()
            operations.open(10.0)
            outcomes = operations.ensure_artifacts(plan, 10.0)
            operations.prepare_boot(plan, 10.0)
            operations.booti(plan, 10.0)
            operations.close()
            result = StageResult(
                schema_version=1,
                stage="board",
                passed=True,
                reason="board-pass",
                plan_sha256=plan.plan_sha256,
                evidence=(
                    "serial.log",
                    "transport.json",
                    "probe-tcp-info.json",
                ),
            )
            operations.publish(result, "post-boot\n", outcomes)
            operations.finish()

            self.assertEqual(closed, [23])
            self.assertEqual(sends.count(""), 1)
            initramfs = next(
                item for item in plan.artifacts if item.name == "initramfs"
            )
            self.assertEqual(
                sends.count(
                    f"booti 0x80200000 0x83000000:0x{initramfs.size:x} 0xf0000000"
                ),
                1,
            )
            self.assertFalse(any("saveenv" in command for command in commands))
            mmc_device = commands.index("mmc dev 1")
            self.assertEqual(
                commands[mmc_device : mmc_device + 2], ["mmc dev 1", "mmc rescan"]
            )
            self.assertLess(mmc_device, commands.index("fdt addr 0xf0000000"))
            self.assertTrue(
                any(command.startswith("fdt addr ") for command in commands)
            )
            self.assertTrue(
                any("asterinas,usb-host" in command for command in commands)
            )
            self.assertTrue((output / "serial.log").is_file())
            self.assertTrue((output / "transport.json").is_file())
            trace = json.loads((output / "probe-tcp-info.json").read_bytes())
            self.assertEqual(trace["plan_sha256"], plan.plan_sha256)
            self.assertEqual(
                StageResult.from_bytes((output / "result.json").read_bytes()),
                result,
            )


class MegrezDebugCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.artifact_directory = self.directory / "artifacts with spaces"
        self.artifact_directory.mkdir()
        self.artifacts: dict[str, Path] = {}
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = self.artifact_directory / f"{name} image"
            path.write_bytes(f"{name}-payload".encode())
            self.artifacts[name] = path
        self.plan_path = self.directory / "plan output" / "debug-plan.json"

    def _run(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.riscv.megrez_debug",
                *(str(value) for value in arguments),
            ],
            cwd=self.directory,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def _create_plan(self) -> DebugPlan:
        result = self._run(
            "plan",
            "--kernel",
            self.artifacts["kernel"],
            "--initramfs",
            self.artifacts["initramfs"],
            "--qemu-dtb",
            self.artifacts["qemu_dtb"],
            "--megrez-dtb",
            self.artifacts["megrez_dtb"],
            "--bootargs",
            ("cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.reboot_after=180"),
            "--marker",
            "Enter riscv_boot",
            "--marker",
            "ASTERINAS_GMAC_TCP_PROBE_READY",
            "--output",
            self.plan_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return DebugPlan.from_bytes(self.plan_path.read_bytes())

    def test_plan_and_check_work_from_an_arbitrary_directory(self) -> None:
        plan = self._create_plan()

        self.assertEqual(stat.S_IMODE(self.plan_path.stat().st_mode), 0o644)
        self.assertEqual(plan.profile, "tcp-probe")
        self.assertEqual(plan.reboot_after, 180)
        check = self._run("check", self.plan_path)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(
            check.stdout,
            f"MEGREZ_DEBUG_CHECK_PASS plan={plan.plan_sha256}\n",
        )
        self.assertEqual(check.stderr, "")

    def test_plan_rejects_directory_and_symlink_outputs_without_mutation(self) -> None:
        output_directory = self.directory / "output-directory"
        output_directory.mkdir()
        protected = self.directory / "protected"
        protected.write_bytes(b"keep")
        output_symlink = self.directory / "output-symlink"
        output_symlink.symlink_to(protected)

        for output in (output_directory, output_symlink):
            with self.subTest(output=output.name):
                self.plan_path = output
                result = self._run(
                    "plan",
                    "--kernel",
                    self.artifacts["kernel"],
                    "--initramfs",
                    self.artifacts["initramfs"],
                    "--qemu-dtb",
                    self.artifacts["qemu_dtb"],
                    "--megrez-dtb",
                    self.artifacts["megrez_dtb"],
                    "--bootargs",
                    "init=/init asterinas.reboot_after=180",
                    "--marker",
                    "READY",
                    "--output",
                    output,
                )
                self.assertEqual(result.returncode, 2)
        self.assertEqual(protected.read_bytes(), b"keep")
        self.assertEqual(list(output_directory.iterdir()), [])

    def test_check_detects_artifact_drift(self) -> None:
        self._create_plan()
        self.artifacts["kernel"].write_bytes(b"changed-kernel")

        result = self._run("check", self.plan_path)

        self.assertEqual(result.returncode, 2)
        self.assertIn("plan-artifact-drift", result.stderr)

    def test_board_dry_run_is_complete_and_never_requires_serial(self) -> None:
        plan = self._create_plan()

        result = self._run(
            "board",
            self.plan_path,
            self.directory / "missing-serial-device",
            "--simulation-result",
            self.directory / "missing-result.json",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                {"action": "require-simulation", "tier": "fast"},
                {"action": "probe-uboot-baud", "choices": [115200, 1500000]},
                {
                    "action": "cache-or-transfer",
                    "artifact": "kernel",
                    "address": 0x80200000,
                },
                {
                    "action": "cache-or-transfer",
                    "artifact": "initramfs",
                    "address": 0x83000000,
                },
                {
                    "action": "cache-or-transfer",
                    "artifact": "megrez_dtb",
                    "address": 0xF0000000,
                },
                {"action": "boot-once", "reboot_after": plan.reboot_after},
                {"action": "capture-markers"},
                {"action": "await-automatic-recovery"},
            ],
        )

    def test_board_refuses_missing_simulation_before_serial_access(self) -> None:
        self._create_plan()

        result = self._run(
            "board",
            self.plan_path,
            self.directory / "missing-serial-device",
            "--simulation-result",
            self.directory / "missing-result.json",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("plan-simulation-missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
