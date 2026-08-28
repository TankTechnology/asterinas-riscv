"""Plan/permit-bound Megrez Debian LAN installer workflow tests."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
import zlib
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tools.riscv.megrez_debug_contract import (
    DEBIAN_BROWSER_ARTIFACT_ORDER,
    DEBIAN_BROWSER_MARKERS,
    ROOT_IMAGE_BYTES,
    ArtifactIdentity,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debian_install import InstallError, run_network_install
from tools.riscv.megrez_board_session import validate_recovery_epoch
from tools.riscv.megrez_preboard import PreboardPermit


class MegrezInstallWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        source = self.repository / "artifacts"
        source.mkdir()
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        self.identities = {}
        for name in DEBIAN_BROWSER_ARTIFACT_ORDER:
            path = source / name
            payload = name.encode()
            path.write_bytes(payload)
            self.identities[name] = ArtifactIdentity(
                name,
                str(path.absolute()),
                addresses.get(name, 0),
                ROOT_IMAGE_BYTES if name == "root_image" else 4096,
                hashlib.sha256(name.encode()).hexdigest(),
                f"{zlib.crc32(name.encode()):08x}",
            )
        self.plan = DebugPlan(
            2,
            "debian-browser",
            tuple(self.identities[name] for name in DEBIAN_BROWSER_ARTIFACT_ORDER),
            (
                "console=tty0 console=ttyS0 cpu_no_boost_1_6ghz loglevel=info "
                "init=/init asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
                "asterinas.reboot_after=600 -- --root-init=systemd"
            ),
            4,
            True,
            DEBIAN_BROWSER_MARKERS,
            600,
        )
        self.permit = PreboardPermit(
            1,
            True,
            "preboard-pass",
            self.plan.plan_sha256,
            "a" * 64,
            "b" * 64,
            "c" * 40,
            self.identities["kernel"].sha256,
            tuple(
                (name, self.identities[name].crc32)
                for name in ("kernel", "initramfs", "megrez_dtb")
            ),
            self.plan.bootargs,
            600,
        )
        self.permit_path = self.repository / "permit.json"
        self.permit_path.write_bytes(self.permit.canonical_bytes())
        self.base = self.repository / "base.cpio"
        self.base.write_bytes(b"base")
        self.tftp = self.repository / "target/tftp"
        self.output = self.repository / "target/install"

    def _artifacts(self, plan: DebugPlan) -> dict[str, ArtifactIdentity]:
        self.assertIs(plan, self.plan)
        return self.identities

    def test_success_builds_exact_installer_and_requires_recovery(self) -> None:
        events: list[object] = []

        def build(
            base: Path,
            root: Path,
            output: Path,
            root_hash: str,
            root_url: str,
        ) -> None:
            events.append(("build", base, root, root_hash, root_url))
            output.write_bytes(b"installer")

        @contextmanager
        def server(address: str, port: int, root: Path):
            events.append(("server-enter", address, port, root))
            yield
            events.append("server-exit")

        def run(command: list[str], **options: object):
            events.append(("run", tuple(command), options))
            return subprocess.CompletedProcess(command, 0, "", "")

        result = run_network_install(
            self.plan,
            self.permit_path,
            "/dev/ttyUSB0",
            self.output,
            self.base,
            self.tftp,
            "http://10.100.19.216:8080/debian-root.ext2",
            artifact_validator=self._artifacts,
            git_identity=lambda _repository: "c" * 40,
            build_installer=build,
            server_factory=server,
            run_command=run,
            repository_root=self.repository,
        )

        self.assertEqual(result.stage, "install")
        self.assertTrue(result.passed)
        self.assertEqual(result.plan_sha256, self.plan.plan_sha256)
        self.assertEqual(events[0][0], "build")
        self.assertEqual(events[1][0], "server-enter")
        command = events[2][1]
        self.assertIn("--require-recovery", command)
        self.assertIn("--final-profile", command)
        self.assertEqual(command[command.index("--final-profile") + 1], "installer")
        bootargs = command[command.index("--bootargs") + 1]
        self.assertIn("asterinas.mmc_write_partition2", bootargs.split())
        self.assertIn(
            f"asterinas.debian_install_sha256={self.identities['root_image'].sha256}",
            bootargs.split(),
        )
        self.assertIn("asterinas.reboot_after=600", bootargs.split())
        self.assertNotIn("saveenv", command)
        self.assertNotIn("linux", command)
        self.assertEqual(events[-1], "server-exit")
        self.assertEqual(
            StageResult.from_bytes((self.output / "result.json").read_bytes()),
            result,
        )

    def test_invalid_permit_url_or_git_stops_before_build_and_serial(self) -> None:
        calls: list[str] = []

        def forbidden(*_args: object, **_kwargs: object):
            calls.append("called")
            raise AssertionError("side effect reached")

        variants = (
            (self.permit_path, "http://example.com/root", "c" * 40),
            (self.permit_path, "http://10.100.19.216:8080/root", "d" * 40),
        )
        mismatched = self.repository / "mismatched.json"
        mismatched.write_bytes(
            PreboardPermit(
                *(
                    self.permit.schema_version,
                    self.permit.passed,
                    self.permit.reason,
                    "0" * 64,
                    self.permit.desktop_result_sha256,
                    self.permit.recovery_result_sha256,
                    self.permit.git_commit,
                    self.permit.kernel_sha256,
                    self.permit.transfer_crc32,
                    self.permit.bootargs,
                    self.permit.reboot_after,
                )
            ).canonical_bytes()
        )
        variants += ((mismatched, "http://10.100.19.216:8080/root", "c" * 40),)
        for permit, url, commit in variants:
            with self.subTest(url=url, commit=commit), self.assertRaises(InstallError):
                run_network_install(
                    self.plan,
                    permit,
                    "/dev/ttyUSB0",
                    self.output,
                    self.base,
                    self.tftp,
                    url,
                    artifact_validator=self._artifacts,
                    git_identity=lambda _repository, commit=commit: commit,
                    build_installer=forbidden,
                    server_factory=forbidden,
                    run_command=forbidden,
                    repository_root=self.repository,
                )
        self.assertEqual(calls, [])

    def test_failed_board_run_invalidates_stale_success(self) -> None:
        self.output.mkdir(parents=True)
        (self.output / "result.json").write_text('{"passed":true}\n')

        def build(
            _base: Path,
            _root: Path,
            output: Path,
            _root_hash: str,
            _root_url: str,
        ) -> None:
            output.write_bytes(b"installer")

        @contextmanager
        def server(_address: str, _port: int, _root: Path):
            yield

        with self.assertRaisesRegex(InstallError, "board.*exit 7"):
            run_network_install(
                self.plan,
                self.permit_path,
                "/dev/ttyUSB0",
                self.output,
                self.base,
                self.tftp,
                "http://10.100.19.216:8080/debian-root.ext2",
                artifact_validator=self._artifacts,
                git_identity=lambda _repository: "c" * 40,
                build_installer=build,
                server_factory=server,
                run_command=lambda command, **_options: subprocess.CompletedProcess(
                    command, 7, "", ""
                ),
                repository_root=self.repository,
            )
        self.assertFalse((self.output / "result.json").exists())

    def test_recovery_epoch_requires_new_ordered_firmware_and_prompt(self) -> None:
        validate_recovery_epoch("OpenSBI v1.7\nU-Boot 2026.07\n=> ")
        for invalid in (
            "U-Boot 2026.07\n=> ",
            "U-Boot 2026.07\nOpenSBI v1.7\n=> ",
            "OpenSBI v1.7\n=> \nU-Boot 2026.07\n",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                validate_recovery_epoch(invalid)

    def test_unified_cli_dispatches_the_permit_bound_installer(self) -> None:
        from tools.riscv import megrez_debug

        plan_path = self.repository / "plan.json"
        plan_path.write_bytes(self.plan.canonical_bytes())
        expected = StageResult(
            1,
            "install",
            True,
            "install-pass",
            self.plan.plan_sha256,
            ("installer.serial.log",),
        )
        with mock.patch.object(
            megrez_debug, "run_network_install", return_value=expected, create=True
        ) as install:
            status = megrez_debug.main(
                (
                    "install",
                    str(plan_path),
                    "/dev/ttyUSB0",
                    "--permit",
                    str(self.permit_path),
                    "--output-directory",
                    str(self.output),
                    "--base-cpio",
                    str(self.base),
                    "--tftp-directory",
                    str(self.tftp),
                    "--root-url",
                    "http://10.100.19.216:8080/debian-root.ext2",
                    "--timeout",
                    "900",
                )
            )
        self.assertEqual(status, 0)
        install.assert_called_once_with(
            self.plan,
            self.permit_path,
            "/dev/ttyUSB0",
            self.output,
            self.base,
            self.tftp,
            "http://10.100.19.216:8080/debian-root.ext2",
            timeout=900.0,
        )


if __name__ == "__main__":
    unittest.main()
