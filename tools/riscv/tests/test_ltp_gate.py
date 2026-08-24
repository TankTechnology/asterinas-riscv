#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ltp_gate import (
    _git_commit,
    _parse_args,
    _package_lock,
    _prepared_artifact_paths,
    _source_commit,
    _validate_packaged_suite,
    exit_code,
    main,
    package_subset,
    profile_for_smp,
    run_paths,
    tree_sha256,
)
from ltp_package import publish_package_identity
from ltp_suite import suite_by_name


REPO = Path(__file__).resolve().parents[3]
OPERATOR_GUIDE = REPO / "tools/riscv/ltp/README.md"
ARCH_REPORT = REPO / "tools/riscv/ltp/ARCH-RISCV64-M1-report.md"
BUSYBOX_BUILDER = REPO / "tools/riscv/nixos/build_busybox.sh"
REPO_MAKEFILE = REPO / "Makefile"
IMPLEMENTATION_PLAN = (
    REPO / "docs/superpowers/plans/2026-08-20-riscv-ltp-gate-baseline.md"
)


class LtpGatePolicyTests(unittest.TestCase):
    def test_makefile_keeps_smp4_default_local_to_ltp(self) -> None:
        source = REPO_MAKEFILE.read_text()

        self.assertIn("SMP ?= 1", source)
        self.assertIn("RISCV_LTP_SMP ?= 4", source)
        self.assertIn('--smp "$(RISCV_LTP_SMP)"', source)

    def test_run_defaults_to_smp4(self) -> None:
        args = _parse_args(
            [
                "run",
                "--kernel",
                "target/osdk/kernel.Image",
                "--dry-run",
            ]
        )

        self.assertEqual(args.smp, 4)

    def test_prepared_artifacts_must_match_normalized_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = Path(temporary)
            fs_root = prepared / "fs-root"
            fs_root.mkdir()
            artifacts = {
                "kernel_sha256": hashlib.sha256(b"kernel").hexdigest(),
                "initrd_sha256": hashlib.sha256(b"initrd").hexdigest(),
                "dtb_sha256": hashlib.sha256(b"dtb").hexdigest(),
                "boot_disk_sha256": hashlib.sha256(b"boot").hexdigest(),
            }
            for path, payload in (
                (fs_root / "asterinas.booti", b"kernel"),
                (fs_root / "initramfs.cpio.gz", b"initrd"),
                (fs_root / "qemu-virt.dtb", b"dtb"),
                (prepared / "boot.ext4", b"boot"),
            ):
                path.write_bytes(payload)

            paths = _prepared_artifact_paths(prepared, {"artifacts": artifacts})
            self.assertEqual(len(paths), 4)
            (fs_root / "initramfs.cpio.gz").write_bytes(b"replaced")
            with self.assertRaisesRegex(ValueError, "initrd_sha256"):
                _prepared_artifact_paths(prepared, {"artifacts": artifacts})

    def test_failed_run_still_publishes_checksums_for_available_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            kernel = repo / "target/osdk/kernel.Image"
            initramfs = repo / "target/ltp/ltp-initramfs.cpio.gz"
            manifest = repo / "target/ltp/rootfs/opt/ltp/runtest/syscalls"
            unavailable = repo / "target/ltp/unavailable-tests.json"
            kernel.parent.mkdir(parents=True)
            initramfs.parent.mkdir(parents=True)
            manifest.parent.mkdir(parents=True)
            kernel.write_bytes(b"kernel")
            initramfs.write_bytes(b"initramfs")
            manifest.write_text("getpid01 getpid01\n")
            unavailable.write_text("[]\n")
            paths = run_paths(
                repo,
                run_id="failed-run",
                smp=4,
                kernel=kernel,
            )
            call_count = 0

            def failed_commands(command: list[str], **kwargs: object) -> Mock:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    fs_root = paths.prepared_dir / "fs-root"
                    fs_root.mkdir(parents=True)
                    for path, payload in (
                        (fs_root / "asterinas.booti", b"kernel"),
                        (fs_root / "initramfs.cpio.gz", b"initramfs"),
                        (fs_root / "qemu-virt.dtb", b"dtb"),
                        (paths.prepared_dir / "boot.ext4", b"boot"),
                        (paths.prepared_dir / "artifacts.json", b"{}"),
                        (paths.prepared_dir / "qemu-dtb-audit.json", b"{}"),
                        (paths.prepared_dir / "qemu-virt.dtb", b"dtb"),
                        (paths.prepared_dir / "qemu-virt.dts", b"dts"),
                        (paths.prepared_dir / "u-boot.config", b"config"),
                        (paths.prepared_dir / "u-boot-commit.txt", b"commit\n"),
                        (paths.prepared_dir / "qemu-version.txt", b"qemu\n"),
                        (paths.prepared_dir / "SHA256SUMS", b"prepared sums\n"),
                        (fs_root.parent / "fs-verify/asterinas.booti", b"kernel"),
                        (paths.result_dir / "serial.log", b"serial failure\n"),
                        (paths.result_dir / "progress.log", b"[RUN] getpid01\n"),
                        (paths.result_dir / "marker-event.txt", b""),
                        (paths.result_dir / "boot-result.json", b"{}"),
                    ):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(payload)
                return Mock(returncode=0 if call_count == 1 else 1)

            with (
                patch(
                    "ltp_gate._validate_packaged_suite",
                    return_value=(manifest, unavailable),
                ),
                patch("ltp_gate.subprocess.run", side_effect=failed_commands),
            ):
                status = main(
                    [
                        "run",
                        "--kernel",
                        str(kernel),
                        "--run-id",
                        "failed-run",
                        "--skip-build",
                        "--source-commit",
                        "a" * 40,
                    ],
                    repo=repo,
                )

            checksum_path = paths.result_dir / "SHA256SUMS"
            self.assertEqual(status, 1)
            self.assertEqual(call_count, 3)
            self.assertTrue(checksum_path.is_file())
            expected = {
                path.resolve().relative_to(repo.resolve()).as_posix()
                for root in (paths.prepared_dir, paths.result_dir)
                for path in root.rglob("*")
                if path.is_file() and path != checksum_path
            }
            actual = {
                line.split("  ", 1)[1]
                for line in checksum_path.read_text().splitlines()
            }
            self.assertEqual(actual, expected)

    def test_prepare_exception_still_publishes_checksums_for_run_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            kernel = repo / "target/osdk/kernel.Image"
            initramfs = repo / "target/ltp/ltp-initramfs.cpio.gz"
            manifest = repo / "target/ltp/rootfs/opt/ltp/runtest/syscalls"
            unavailable = repo / "target/ltp/unavailable-tests.json"
            kernel.parent.mkdir(parents=True)
            initramfs.parent.mkdir(parents=True)
            manifest.parent.mkdir(parents=True)
            kernel.write_bytes(b"kernel")
            initramfs.write_bytes(b"initramfs")
            manifest.write_text("getpid01 getpid01\n")
            unavailable.write_text("[]\n")
            paths = run_paths(
                repo,
                run_id="prepare-exception",
                smp=4,
                kernel=kernel,
            )

            def fail_prepare(command: list[str], **kwargs: object) -> Mock:
                partial = paths.prepared_dir / "partial-prepare.txt"
                partial.parent.mkdir(parents=True)
                partial.write_text("prepare failed\n")
                raise subprocess.CalledProcessError(2, command)

            with (
                patch(
                    "ltp_gate._validate_packaged_suite",
                    return_value=(manifest, unavailable),
                ),
                patch("ltp_gate.subprocess.run", side_effect=fail_prepare),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                main(
                    [
                        "run",
                        "--kernel",
                        str(kernel),
                        "--run-id",
                        "prepare-exception",
                        "--skip-build",
                        "--source-commit",
                        "a" * 40,
                    ],
                    repo=repo,
                )

            checksum_path = paths.result_dir / "SHA256SUMS"
            self.assertTrue(checksum_path.is_file())
            expected = {
                path.resolve().relative_to(repo.resolve()).as_posix()
                for root in (paths.prepared_dir, paths.result_dir)
                for path in root.rglob("*")
                if path.is_file() and path != checksum_path
            }
            actual = {
                line.split("  ", 1)[1]
                for line in checksum_path.read_text().splitlines()
            }
            self.assertEqual(actual, expected)

    def test_status_reports_current_test_and_mutually_exclusive_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            result = repo / "target/ltp/results/live"
            result.mkdir(parents=True)
            (result / "manifest.txt").write_text(
                "getpid01 getpid01\nread01 read01\nwrite01 write01\n"
            )
            (result / "progress.log").write_text(
                "[RUN] 1 getpid01\n"
                "[PASS] getpid01\n"
                "[RUN] 2 read01\n"
                "[FAIL] read01\n"
                "[RUN] 3 write01\n"
            )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                status = main(["status", "--run-id", "live"], repo=repo)

        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(),
            "state=RUNNING completed=2/3 current=write01\n"
            "pass=1 fail=1 conf=0 crash=0 timeout=0\n",
        )

    def test_git_commit_marks_the_bound_repository_as_safe(self) -> None:
        completed = Mock(stdout="a" * 40)
        with patch("ltp_gate.subprocess.run", return_value=completed) as run:
            commit = _git_commit(REPO)

        self.assertEqual(commit, "a" * 40)
        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "-c",
                f"safe.directory={REPO.resolve()}",
                "rev-parse",
                "HEAD",
            ],
        )

    def test_explicit_source_commit_supports_a_containerized_worktree(self) -> None:
        with patch("ltp_gate._git_commit") as git_commit:
            commit = _source_commit(REPO, "b" * 40)

        self.assertEqual(commit, "b" * 40)
        git_commit.assert_not_called()

    def test_explicit_source_commit_must_be_a_full_lowercase_object_id(self) -> None:
        for commit in ("", "c" * 39, "D" * 40, "not-an-object-id"):
            with self.subTest(commit=commit):
                with self.assertRaisesRegex(ValueError, "source commit"):
                    _source_commit(REPO, commit)

    def test_profile_for_smp_is_closed(self) -> None:
        self.assertEqual(profile_for_smp(1), "generic-sv39-ltp-smp1")
        self.assertEqual(profile_for_smp(4), "generic-sv39-ltp-smp4")
        with self.assertRaisesRegex(ValueError, "SMP must be 1 or 4"):
            profile_for_smp(2)

    def test_arch_suite_run_defaults_to_smp4(self) -> None:
        args = _parse_args(
            [
                "run",
                "--kernel",
                "target/osdk/kernel.Image",
                "--suite",
                "arch-riscv64",
            ]
        )

        self.assertEqual(args.smp, 4)

    def test_run_paths_never_overlap_shared_qemu_current(self) -> None:
        paths = run_paths(REPO, run_id="m1", smp=1)

        self.assertTrue(
            paths.prepared_dir.is_relative_to(REPO / "target" / "ltp" / "qemu")
        )
        self.assertEqual(
            paths.prepared_dir,
            REPO / "target" / "ltp" / "qemu" / "smp1" / "m1",
        )
        self.assertTrue(
            paths.result_dir.is_relative_to(REPO / "target" / "ltp" / "results")
        )
        self.assertNotIn(REPO / "target/qemu-uboot/current", paths.all_paths())

    def test_run_id_rejects_path_syntax(self) -> None:
        for run_id in ("../escape", "two words", "", "slash/name"):
            with self.subTest(run_id=run_id):
                with self.assertRaisesRegex(ValueError, "run id"):
                    run_paths(REPO, run_id=run_id, smp=1)

    def test_baseline_mode_ignores_ltp_failures_not_infrastructure_failures(
        self,
    ) -> None:
        self.assertEqual(
            exit_code(
                infrastructure_passed=True,
                ltp_passed=False,
                baseline=True,
            ),
            0,
        )
        self.assertEqual(
            exit_code(
                infrastructure_passed=False,
                ltp_passed=True,
                baseline=True,
            ),
            1,
        )
        self.assertEqual(
            exit_code(
                infrastructure_passed=True,
                ltp_passed=False,
                baseline=False,
            ),
            1,
        )

    def test_dry_run_never_invokes_or_names_shared_qemu_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            kernel = repo / "target/osdk/kernel.Image"
            kernel.parent.mkdir(parents=True)
            kernel.write_bytes(b"kernel")
            output = io.StringIO()

            with (
                patch("ltp_gate.subprocess.run") as run,
                contextlib.redirect_stdout(output),
            ):
                status = main(
                    [
                        "run",
                        "--kernel",
                        str(kernel),
                        "--run-id",
                        "dry-run",
                        "--skip-build",
                        "--dry-run",
                    ],
                    repo=repo,
                )

        self.assertEqual(status, 0)
        run.assert_not_called()
        self.assertIn("target/ltp/qemu/smp4/dry-run", output.getvalue())
        self.assertIn("target/ltp/results/dry-run/progress.log", output.getvalue())
        self.assertNotIn("target/qemu-uboot/current", output.getvalue())

    def test_arch_suite_dry_run_names_build_and_result_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            kernel = repo / "target/osdk/kernel.Image"
            kernel.parent.mkdir(parents=True)
            kernel.write_bytes(b"kernel")
            output = io.StringIO()

            with (
                patch("ltp_gate.subprocess.run") as run,
                contextlib.redirect_stdout(output),
            ):
                status = main(
                    [
                        "run",
                        "--kernel",
                        str(kernel),
                        "--suite",
                        "arch-riscv64",
                        "--run-id",
                        "arch-dry",
                        "--dry-run",
                    ],
                    repo=repo,
                )

        self.assertEqual(status, 0)
        run.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("build_ltp.sh --suite arch-riscv64", rendered)
        self.assertIn("ltp_result.py write", rendered)
        self.assertIn("target/ltp/results/arch-dry/manifest.txt", rendered)
        self.assertIn("--suite arch-riscv64", rendered)
        self.assertIn("--smp 4", rendered)

    def test_packaged_arch_suite_requires_exact_manifest_and_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            names = tuple(f"arch_test_{index:03d}" for index in range(138))
            missing = "rt_sigtimedwait01"
            enabled = repo / "tools/riscv/ltp/manifests/arch-riscv64.txt"
            runtest = repo / "target/ltp/src/runtest/syscalls"
            binaries = repo / "target/ltp/rootfs/opt/ltp/testcases/bin"
            manifest = repo / "target/ltp/rootfs/opt/ltp/runtest/syscalls"
            unavailable = repo / "target/ltp/unavailable-tests.json"
            initramfs = repo / "target/ltp/ltp-initramfs.cpio.gz"
            identity = repo / "target/ltp/package.json"
            enabled.parent.mkdir(parents=True)
            runtest.parent.mkdir(parents=True)
            binaries.mkdir(parents=True)
            manifest.parent.mkdir(parents=True)
            enabled.write_text("\n".join((*names, missing)) + "\n")
            runtest.write_text(
                "\n".join(f"{name} {name}" for name in (*names, missing)) + "\n"
            )
            manifest.write_text(
                "\n".join(f"{name} {name}" for name in names) + "\n"
            )
            for name in names:
                (binaries / name).write_bytes(b"binary")
            unavailable.write_text(
                json.dumps([{"name": missing, "reason": "missing-binary"}]) + "\n"
            )
            initramfs.write_bytes(b"archive")
            suite = suite_by_name(repo, "arch-riscv64")

            publish_package_identity(
                suite=suite.name,
                initramfs=initramfs,
                manifest=manifest,
                unavailable=unavailable,
                output=identity,
            )

            selected, omitted = _validate_packaged_suite(repo, suite)
            self.assertEqual(selected, manifest)
            self.assertEqual(omitted, unavailable)

            manifest.write_text(manifest.read_text().splitlines()[0] + "\n")
            with self.assertRaisesRegex(ValueError, "packaged manifest"):
                _validate_packaged_suite(repo, suite)

            manifest.write_text(
                "\n".join(f"{name} {name}" for name in names) + "\n"
            )
            unavailable.write_text("[]\n")
            with self.assertRaisesRegex(ValueError, "unavailable"):
                _validate_packaged_suite(repo, suite)

    def test_package_lock_excludes_a_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            code = """
import fcntl
import sys

with open(sys.argv[1], 'a+b') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
raise SystemExit(1)
"""

            with _package_lock(repo):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(repo / "target/ltp/package.lock"),
                    ],
                    check=False,
                )

        self.assertEqual(completed.returncode, 0)

    def test_dry_run_rejects_an_ltp_target_symlink_outside_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            repo = parent / "repo"
            escaped = parent / "escaped"
            kernel = repo / "kernel.Image"
            (repo / "target").mkdir(parents=True)
            escaped.mkdir()
            kernel.write_bytes(b"kernel")
            (repo / "target/ltp").symlink_to(escaped, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "LTP target root"):
                main(
                    [
                        "run",
                        "--kernel",
                        str(kernel),
                        "--run-id",
                        "escape",
                        "--skip-build",
                        "--dry-run",
                    ],
                    repo=repo,
                )

    def test_dry_run_rejects_a_symlinked_result_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            kernel = repo / "kernel.Image"
            results = repo / "target/ltp/results"
            existing = results / "existing"
            existing.mkdir(parents=True)
            kernel.write_bytes(b"kernel")
            (results / "linked").symlink_to(existing, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "result directory.*symlink"):
                main(
                    [
                        "run",
                        "--kernel",
                        str(kernel),
                        "--run-id",
                        "linked",
                        "--skip-build",
                        "--dry-run",
                    ],
                    repo=repo,
                )


class LtpGateDocumentationTests(unittest.TestCase):
    def test_arch_report_is_explicitly_historical_selection_evidence(self) -> None:
        source = ARCH_REPORT.read_text()
        normalized = " ".join(source.replace("\n> ", " ").split())

        self.assertIn("Historical Selection Evidence", source)
        self.assertIn("does not establish a current baseline", normalized)
        self.assertIn("not reachable from a current `origin` ref", normalized)

    def test_operator_guide_builds_the_required_busybox(self) -> None:
        source = OPERATOR_GUIDE.read_text()

        self.assertTrue(BUSYBOX_BUILDER.is_file())
        builder = BUSYBOX_BUILDER.read_text()
        self.assertIn('CROSS_PREFIX="riscv64-linux-gnu-"', builder)
        self.assertIn("ASH", builder)
        self.assertIn("CAT", builder)
        self.assertIn("TRUE", builder)
        self.assertIn("tools/riscv/nixos/build_busybox.sh", source)
        self.assertIn("target/nixos/busybox", source)

    def test_operator_guide_binds_skip_build_to_the_packaged_suite(self) -> None:
        source = OPERATOR_GUIDE.read_text()

        self.assertIn(
            "Every `--skip-build` run must name the suite currently packaged",
            source,
        )
        self.assertIn(
            "build_ltp.sh --skip-compile --suite syscalls",
            source,
        )

    def test_container_commands_do_not_override_the_image_vdso_directory(self) -> None:
        for document in (OPERATOR_GUIDE, IMPLEMENTATION_PLAN):
            with self.subTest(document=document):
                self.assertNotIn(
                    "/root/.local/share/linux_vdso",
                    document.read_text(),
                )

    def test_ltp_build_uses_the_cross_image_and_pinned_musl_package(self) -> None:
        for document in (OPERATOR_GUIDE, IMPLEMENTATION_PLAN):
            with self.subTest(document=document):
                source = document.read_text()
                self.assertNotIn("asterinas-env:nixos-build", source)
                self.assertIn(
                    "0797f54b48c415739bb5360739bc8f9dc8b2019e01de86d89c2859810200b589",
                    source,
                )
                self.assertIn("autoconf automake", source)
                self.assertIn("linux-libc-dev-riscv64-cross", source)


class LtpSubsetPackagingTests(unittest.TestCase):
    def _make_repo(self, root: Path) -> tuple[Path, tuple[str, ...]]:
        tags = ("accept01", "bind01", "clock_getres01", "execve01", "mmap01")
        manifest = root / "test/initramfs/src/conformance/ltp/testcases/all.txt"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("\n".join((*tags, "unavailable01")) + "\n")

        rootfs = root / "target/ltp/rootfs"
        binaries = rootfs / "opt/ltp/testcases/bin"
        runtest = rootfs / "opt/ltp/runtest/syscalls"
        source_runtest = root / "target/ltp/src/runtest/syscalls"
        binaries.mkdir(parents=True)
        runtest.parent.mkdir(parents=True)
        source_runtest.parent.mkdir(parents=True)
        runtest_text = (
            "\n".join(f"{tag} {tag} --fixture" for tag in tags)
            + "\nunavailable01 unavailable01\n"
        )
        runtest.write_text(runtest_text)
        source_runtest.write_text(runtest_text)
        for tag in tags:
            binary = binaries / tag
            binary.write_text(f"fixture:{tag}\n")
            binary.chmod(0o755)
        busybox = rootfs / "bin/busybox"
        busybox.parent.mkdir()
        busybox.write_bytes(b"busybox")
        (rootfs / "bin/sh").symlink_to("busybox")
        return rootfs, tags

    def test_five_tag_subset_is_staged_without_mutating_full_rootfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            rootfs, tags = self._make_repo(repo)
            build_root = repo / "target/ltp/build"
            build_root.mkdir(parents=True)
            before = tree_sha256(rootfs)

            with tempfile.TemporaryDirectory(dir=build_root) as workspace_name:
                package = package_subset(
                    repo,
                    tags=tags,
                    workspace=Path(workspace_name),
                )
                selected = package.manifest.read_text().splitlines()

                self.assertTrue(package.initramfs.is_file())
                self.assertGreater(package.initramfs.stat().st_size, 0)
                self.assertEqual(len(selected), 5)
                self.assertEqual(
                    [line.split()[0] for line in selected],
                    list(tags),
                )
                self.assertTrue((package.rootfs / "bin/sh").is_symlink())

            self.assertEqual(tree_sha256(rootfs), before)

    def test_unknown_or_unavailable_subset_is_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self._make_repo(repo)
            workspace = repo / "target/ltp/build/subset"
            workspace.mkdir(parents=True)

            for tag in ("unknown01", "unavailable01"):
                with self.subTest(tag=tag):
                    with self.assertRaisesRegex(ValueError, "subset tag"):
                        package_subset(repo, tags=(tag,), workspace=workspace)
                    self.assertEqual(tuple(workspace.iterdir()), ())


if __name__ == "__main__":
    unittest.main()
