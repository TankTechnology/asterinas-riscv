#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ltp_gate import (
    _git_commit,
    exit_code,
    main,
    package_subset,
    profile_for_smp,
    run_paths,
    tree_sha256,
)


REPO = Path(__file__).resolve().parents[3]
OPERATOR_GUIDE = REPO / "tools/riscv/ltp/README.md"
IMPLEMENTATION_PLAN = (
    REPO / "docs/superpowers/plans/2026-08-20-riscv-ltp-gate-baseline.md"
)


class LtpGatePolicyTests(unittest.TestCase):
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

    def test_profile_for_smp_is_closed(self) -> None:
        self.assertEqual(profile_for_smp(1), "generic-sv39-ltp-smp1")
        self.assertEqual(profile_for_smp(4), "generic-sv39-ltp-smp4")
        with self.assertRaisesRegex(ValueError, "SMP must be 1 or 4"):
            profile_for_smp(2)

    def test_run_paths_never_overlap_shared_qemu_current(self) -> None:
        paths = run_paths(REPO, run_id="m1", smp=1)

        self.assertTrue(
            paths.prepared_dir.is_relative_to(REPO / "target" / "ltp" / "qemu")
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
        self.assertIn("target/ltp/qemu/smp1", output.getvalue())
        self.assertNotIn("target/qemu-uboot/current", output.getvalue())

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
        binaries.mkdir(parents=True)
        runtest.parent.mkdir(parents=True)
        runtest.write_text(
            "\n".join(f"{tag} {tag} --fixture" for tag in tags)
            + "\nunavailable01 unavailable01\n"
        )
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
