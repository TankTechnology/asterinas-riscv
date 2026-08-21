#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS / "riscv"))
sys.path.insert(0, str(TOOLS / "nixos"))

from qemu_uboot_profiles import GENERIC_SV39, QEMU_VIRT_SMP4  # noqa: E402
from riscv_preflight import (  # noqa: E402
    ArtifactContract,
    PreflightFailure,
    check_artifacts,
    main,
    qemu_argv,
)


class RiscvNixosPreflightTests(unittest.TestCase):
    def _complete_repo(self, parent: Path, name: str = "repo") -> Path:
        repo = parent / name
        contract = ArtifactContract.from_repo(repo)
        for path in (
            contract.uboot,
            contract.boot_disk,
            contract.root_disk,
            contract.dtb,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"artifact")
        return repo

    def _replace_root_with_hardlink(self, contract: ArtifactContract) -> None:
        contract.root_disk.unlink()
        os.link(contract.boot_disk, contract.root_disk)

    def _replace_root_with_symlink(self, contract: ArtifactContract) -> None:
        contract.root_disk.unlink()
        contract.root_disk.symlink_to(contract.boot_disk)

    def _assert_disk_alias_failure(self, contract: ArtifactContract) -> None:
        failures = check_artifacts(contract)

        self.assertEqual([failure.kind for failure in failures], ["disk-alias"])
        self.assertEqual(failures[0].path, contract.root_disk)
        self.assertIn("same underlying file", failures[0].remedy)
        self.assertIn(str(contract.boot_disk), failures[0].remedy)
        self.assertIn(str(contract.root_disk), failures[0].remedy)

    def _assert_print_qemu_rejects_disk_alias(self, repo: Path) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["--print-qemu", "--repo", str(repo)])

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("preflight failure: disk-alias", stderr.getvalue())
        self.assertIn("same underlying file", stderr.getvalue())

    def test_default_contract_uses_exact_artifact_paths(self) -> None:
        contract = ArtifactContract.from_repo(Path("/repo"))

        self.assertEqual(
            contract.uboot,
            Path("/repo/target/qemu-uboot/cache/u-boot-build/u-boot"),
        )
        self.assertEqual(
            contract.boot_disk, Path("/repo/target/nixos/riscv64/boot.ext4")
        )
        self.assertEqual(
            contract.root_disk, Path("/repo/target/nixos/riscv64/root.ext2")
        )
        self.assertEqual(contract.dtb, Path("/repo/target/nixos/riscv64/qemu-virt.dtb"))
        self.assertNotIn("smp", ArtifactContract.__dataclass_fields__)

    def test_contract_and_failure_records_are_frozen(self) -> None:
        contract = ArtifactContract.from_repo(Path("/repo"))
        failure = PreflightFailure("dtb", Path("/missing"), "produce it")

        with self.assertRaises(FrozenInstanceError):
            contract.uboot = Path("/changed")
        with self.assertRaises(FrozenInstanceError):
            failure.remedy = "changed"

    def test_default_contract_uses_smp4_and_snapshots_both_disks(self) -> None:
        contract = ArtifactContract.from_repo(Path("/repo"))
        argv = qemu_argv(contract)
        self.assertEqual(argv[argv.index("-smp") + 1], str(QEMU_VIRT_SMP4.hart_count))
        boot_drive = argv[argv.index("-drive") + 1]
        root_drive = argv[argv.index("-drive", argv.index("-drive") + 1) + 1]
        self.assertIn("snapshot=on", boot_drive)
        self.assertIn("snapshot=on", root_drive)
        self.assertIn("id=bootdisk", boot_drive)
        self.assertIn("id=rootdisk", root_drive)

    def test_preflight_reports_every_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            failures = check_artifacts(ArtifactContract.from_repo(Path(directory)))
        self.assertEqual(
            {failure.kind for failure in failures},
            {"uboot", "boot-disk", "root-disk", "dtb"},
        )

    def test_preflight_failure_order_is_stable_and_remedies_name_producers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            failures = check_artifacts(ArtifactContract.from_repo(Path(directory)))

        self.assertEqual(
            [failure.kind for failure in failures],
            ["uboot", "boot-disk", "root-disk", "dtb"],
        )
        self.assertIn("prepare_qemu_uboot_booti.sh", failures[0].remedy)
        self.assertIn("--check-tools", failures[0].remedy)
        self.assertIn("ASTERINAS_RISCV_BOOTI", failures[0].remedy)
        self.assertIn("ASTERINAS_INITRAMFS", failures[0].remedy)
        self.assertIn(
            "tools/riscv/prepare_qemu_uboot_booti.sh prepare",
            failures[0].remedy,
        )
        self.assertIn("R1-B", failures[1].remedy)
        self.assertIn("R1-A", failures[2].remedy)
        self.assertIn("R1-B", failures[2].remedy)
        self.assertIn("R1-B", failures[3].remedy)
        self.assertTrue(all("missing" in failure.remedy for failure in failures))

    def test_empty_directory_and_missing_artifacts_have_distinct_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            contract = ArtifactContract.from_repo(Path(directory))
            contract.uboot.mkdir(parents=True)
            contract.boot_disk.parent.mkdir(parents=True)
            contract.boot_disk.touch()
            contract.dtb.write_bytes(b"dtb")

            failures = check_artifacts(contract)

        self.assertEqual(
            [failure.kind for failure in failures],
            ["uboot", "boot-disk", "root-disk"],
        )
        self.assertIn("not a regular file", failures[0].remedy)
        self.assertIn("empty", failures[1].remedy)
        self.assertIn("missing", failures[2].remedy)

    def test_preflight_returns_no_failures_for_nonempty_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))

            self.assertEqual(check_artifacts(ArtifactContract.from_repo(repo)), ())

    def test_preflight_reports_unreadable_artifact_and_closes_other_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
            file_descriptors = iter((101, 102, 103))
            events: list[tuple[str, object]] = []
            metadata_by_fd = {
                101: contract.uboot.stat(),
                102: contract.root_disk.stat(),
                103: contract.dtb.stat(),
            }

            def open_artifact(path: Path, actual_flags: int) -> int:
                events.append(("open", (path, actual_flags)))
                if path == contract.boot_disk:
                    raise PermissionError("permission denied")
                return next(file_descriptors)

            def close_artifact(file_descriptor: int) -> None:
                events.append(("close", file_descriptor))

            def inspect_artifact(file_descriptor: int) -> os.stat_result:
                events.append(("fstat", file_descriptor))
                return metadata_by_fd[file_descriptor]

            with (
                mock.patch("riscv_preflight.os.open", side_effect=open_artifact),
                mock.patch("riscv_preflight.os.fstat", side_effect=inspect_artifact),
                mock.patch("riscv_preflight.os.close", side_effect=close_artifact),
            ):
                failures = check_artifacts(contract)

        self.assertEqual([failure.kind for failure in failures], ["boot-disk"])
        self.assertIn("unreadable", failures[0].remedy)
        self.assertIn("permission denied", failures[0].remedy)
        self.assertIn("R1-B", failures[0].remedy)
        self.assertEqual(
            events,
            [
                ("open", (contract.uboot, flags)),
                ("fstat", 101),
                ("close", 101),
                ("open", (contract.boot_disk, flags)),
                ("open", (contract.root_disk, flags)),
                ("fstat", 102),
                ("close", 102),
                ("open", (contract.dtb, flags)),
                ("fstat", 103),
                ("close", 103),
            ],
        )

    def test_empty_replacement_after_path_stat_uses_open_descriptor_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            original_open = os.open
            original_close = os.close
            original_stat = Path.stat
            path_was_statted = False

            def observe_path_stat(
                path: Path, *args: object, **kwargs: object
            ) -> os.stat_result:
                nonlocal path_was_statted
                if path == contract.boot_disk:
                    path_was_statted = True
                return original_stat(path, *args, **kwargs)

            def replace_before_open(path: Path, flags: int) -> int:
                if path == contract.boot_disk:
                    contract.boot_disk.unlink()
                    replacement = original_open(
                        contract.boot_disk,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        0o600,
                    )
                    original_close(replacement)
                return original_open(path, flags)

            with (
                mock.patch.object(
                    Path, "stat", autospec=True, side_effect=observe_path_stat
                ),
                mock.patch("riscv_preflight.os.open", side_effect=replace_before_open),
            ):
                failures = check_artifacts(contract)

        self.assertEqual([failure.kind for failure in failures], ["boot-disk"])
        self.assertIn("empty", failures[0].remedy)
        self.assertFalse(path_was_statted)

    def test_fifo_replacement_after_path_stat_is_opened_nonblocking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            original_open = os.open
            original_stat = Path.stat
            path_was_statted = False
            boot_open_flags = 0

            def observe_path_stat(
                path: Path, *args: object, **kwargs: object
            ) -> os.stat_result:
                nonlocal path_was_statted
                if path == contract.boot_disk:
                    path_was_statted = True
                return original_stat(path, *args, **kwargs)

            def replace_before_open(path: Path, flags: int) -> int:
                nonlocal boot_open_flags
                if path == contract.boot_disk:
                    boot_open_flags = flags
                    contract.boot_disk.unlink()
                    os.mkfifo(contract.boot_disk)
                    if not flags & os.O_NONBLOCK:
                        return original_open(os.devnull, flags)
                return original_open(path, flags)

            with (
                mock.patch.object(
                    Path, "stat", autospec=True, side_effect=observe_path_stat
                ),
                mock.patch("riscv_preflight.os.open", side_effect=replace_before_open),
            ):
                failures = check_artifacts(contract)

        self.assertEqual([failure.kind for failure in failures], ["boot-disk"])
        self.assertIn("not a regular file", failures[0].remedy)
        self.assertTrue(boot_open_flags & os.O_NONBLOCK)
        self.assertFalse(path_was_statted)

    def test_preflight_closes_descriptor_when_fstat_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            opened = iter((201, 202, 203, 204))
            closed: list[int] = []

            with (
                mock.patch(
                    "riscv_preflight.os.open", side_effect=lambda *_: next(opened)
                ),
                mock.patch(
                    "riscv_preflight.os.fstat",
                    side_effect=PermissionError("cannot inspect descriptor"),
                ),
                mock.patch("riscv_preflight.os.close", side_effect=closed.append),
            ):
                failures = check_artifacts(contract)

        self.assertEqual(
            [failure.kind for failure in failures],
            ["uboot", "boot-disk", "root-disk", "dtb"],
        )
        self.assertTrue(
            all("not inspectable" in failure.remedy for failure in failures)
        )
        self.assertEqual(closed, [201, 202, 203, 204])

    def test_check_artifacts_rejects_hardlinked_boot_and_root_disks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            self._replace_root_with_hardlink(contract)

            self._assert_disk_alias_failure(contract)

    def test_check_artifacts_rejects_symlinked_boot_and_root_disks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            self._replace_root_with_symlink(contract)

            self._assert_disk_alias_failure(contract)

    def test_check_artifacts_rejects_non_path_fields_deliberately(self) -> None:
        contract = ArtifactContract.from_repo(Path("/repo"))

        for field in ("uboot", "boot_disk", "root_disk", "dtb"):
            with self.subTest(field=field):
                invalid = replace(contract, **{field: "/not/a/path"})
                with self.assertRaisesRegex(ValueError, "path must be"):
                    check_artifacts(invalid)

    def test_check_artifacts_documents_point_in_time_symlink_semantics(self) -> None:
        documentation = check_artifacts.__doc__ or ""

        self.assertIn("point-in-time", documentation)
        self.assertIn("follows symlinks", documentation)
        self.assertIn("does not authenticate or freeze", documentation)

    def test_boot_and_root_disks_are_distinct(self) -> None:
        contract = replace(
            ArtifactContract.from_repo(Path("/repo")),
            root_disk=Path("/repo/target/nixos/riscv64/boot.ext4"),
        )
        with self.assertRaisesRegex(ValueError, "boot and root disk"):
            qemu_argv(contract)

    def test_comma_in_either_drive_path_is_rejected(self) -> None:
        base = ArtifactContract.from_repo(Path("/repo"))
        for field in ("boot_disk", "root_disk"):
            with self.subTest(field=field):
                contract = replace(base, **{field: Path(f"/repo/{field},bad")})
                with self.assertRaisesRegex(ValueError, "comma"):
                    qemu_argv(contract)

    def test_empty_qemu_program_is_rejected(self) -> None:
        contract = ArtifactContract.from_repo(Path("/repo"))

        for qemu in ("", "   "):
            with self.subTest(qemu=qemu):
                with self.assertRaisesRegex(ValueError, "QEMU"):
                    qemu_argv(contract, qemu=qemu)

    def test_qemu_machine_cpu_memory_and_smp_match_sv39_contract(self) -> None:
        argv = qemu_argv(ArtifactContract.from_repo(Path("/repo")))

        self.assertEqual(argv[0], "qemu-system-riscv64")
        self.assertEqual(
            argv[argv.index("-machine") + 1],
            GENERIC_SV39.machine.qemu_machine.value,
        )
        self.assertEqual(argv[argv.index("-cpu") + 1], GENERIC_SV39.cpu)
        self.assertEqual(argv[argv.index("-m") + 1], GENERIC_SV39.memory)
        self.assertEqual(argv[argv.index("-smp") + 1], str(QEMU_VIRT_SMP4.hart_count))
        self.assertIn("-no-reboot", argv)

    def test_qemu_uses_uboot_dtb_and_ordered_virtio_block_devices(self) -> None:
        contract = ArtifactContract.from_repo(Path("/repo with spaces"))
        argv = qemu_argv(contract)

        self.assertEqual(argv[argv.index("-kernel") + 1], str(contract.uboot))
        self.assertEqual(argv[argv.index("-dtb") + 1], str(contract.dtb))
        first_drive = argv.index("-drive")
        second_drive = argv.index("-drive", first_drive + 1)
        self.assertEqual(
            argv[first_drive:second_drive],
            [
                "-drive",
                f"if=none,format=raw,file={contract.boot_disk},id=bootdisk,snapshot=on",
                "-device",
                "virtio-blk-device,drive=bootdisk",
            ],
        )
        self.assertEqual(
            argv[second_drive : second_drive + 4],
            [
                "-drive",
                f"if=none,format=raw,file={contract.root_disk},id=rootdisk,snapshot=on",
                "-device",
                "virtio-blk-device,drive=rootdisk",
            ],
        )
        self.assertIn("snapshot=on", argv[first_drive + 1])
        self.assertIn("snapshot=on", argv[second_drive + 1])

    def test_qemu_uses_interactive_graphics_input_user_network_and_serial(
        self,
    ) -> None:
        argv = qemu_argv(ArtifactContract.from_repo(Path("/repo")))

        self.assertEqual(argv[argv.index("-display") + 1], "gtk")
        self.assertEqual(argv[argv.index("-serial") + 1], "stdio")
        self.assertIn("virtio-gpu-device", argv)
        self.assertIn("virtio-keyboard-device", argv)
        self.assertIn("virtio-tablet-device", argv)
        self.assertIn("user,id=net0", argv)
        self.assertIn("virtio-net-device,netdev=net0", argv)

    def test_qemu_argv_is_pure_and_does_not_inspect_artifacts(self) -> None:
        contract = ArtifactContract.from_repo(Path("/does/not/exist"))

        with mock.patch.object(
            Path, "stat", side_effect=AssertionError("artifact inspection")
        ):
            argv = qemu_argv(contract)

        self.assertEqual(argv[0], "qemu-system-riscv64")

    def test_check_cli_success_prints_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--check", "--repo", str(repo)])

        self.assertEqual(result, 0)
        self.assertIn("preflight OK", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_check_cli_does_not_validate_unused_qemu_program(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--check", "--repo", str(repo), "--qemu", ""])

        self.assertEqual(result, 0)
        self.assertIn("preflight OK", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_check_cli_failure_returns_2_and_prints_every_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--check", "--repo", directory])

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        for kind in ("uboot", "boot-disk", "root-disk", "dtb"):
            self.assertIn(kind, stderr.getvalue())
        self.assertEqual(stderr.getvalue().count("preflight failure:"), 4)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_print_qemu_is_shell_safe_for_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory), "repo with spaces")
            contract = ArtifactContract.from_repo(repo)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--print-qemu", "--repo", str(repo)])

        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue().rstrip("\n"), shlex.join(qemu_argv(contract))
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_print_qemu_honors_program_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = main(
                    [
                        "--print-qemu",
                        "--repo",
                        str(repo),
                        "--qemu",
                        "/opt/QEMU build/qemu-system-riscv64",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.getvalue().rstrip("\n"),
            shlex.join(qemu_argv(contract, qemu="/opt/QEMU build/qemu-system-riscv64")),
        )

    def test_print_qemu_runs_preflight_before_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--print-qemu", "--repo", directory])

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue().count("preflight failure:"), 4)

    def test_print_qemu_rejects_hardlinked_disk_alias_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            self._replace_root_with_hardlink(contract)

            self._assert_print_qemu_rejects_disk_alias(repo)

    def test_print_qemu_rejects_symlinked_disk_alias_without_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            contract = ArtifactContract.from_repo(repo)
            self._replace_root_with_symlink(contract)

            self._assert_print_qemu_rejects_disk_alias(repo)

    def test_cli_help_does_not_offer_smp_override(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("--smp", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_rejects_unknown_smp_override(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["--check", "--smp", "4"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --smp 4", stderr.getvalue())

    def test_invalid_cli_contract_is_concise_and_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory), "repo,invalid")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = main(["--check", "--repo", str(repo)])

        self.assertNotEqual(result, 0)
        self.assertIn("error:", stderr.getvalue())
        self.assertIn("comma", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_modes_are_required_and_mutually_exclusive(self) -> None:
        for arguments in ([], ["--check", "--print-qemu"]):
            with self.subTest(arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_cli_never_invokes_qemu_or_any_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._complete_repo(Path(directory))
            with (
                mock.patch.object(subprocess, "run") as run,
                mock.patch.object(subprocess, "Popen") as popen,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                check_result = main(["--check", "--repo", str(repo)])
                print_result = main(["--print-qemu", "--repo", str(repo)])

        self.assertEqual((check_result, print_result), (0, 0))
        run.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
