#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Current operator-documentation and Makefile contract tests for Debian."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class DebianRootfsDocumentationTests(unittest.TestCase):
    def test_browser_network_plan_builds_the_generic_sv39_kernel(self) -> None:
        plan = (
            REPOSITORY_ROOT
            / "docs/superpowers/plans/2026-08-27-debian-browser-ready-network.md"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode",
            plan,
        )

    def test_operator_guide_covers_the_frozen_offline_runtime_workflow(self) -> None:
        guide = (REPOSITORY_ROOT / "tools/riscv/debian/rootfs/README.md").read_text(
            encoding="utf-8"
        )
        image_guide = (
            REPOSITORY_ROOT / "tools/docker/riscv-rootfs/README.md"
        ).read_text(encoding="utf-8")
        dockerfile = (
            REPOSITORY_ROOT / "tools/docker/riscv-rootfs/Dockerfile"
        ).read_text(encoding="utf-8")
        entrypoint = (
            REPOSITORY_ROOT / "tools/docker/riscv-rootfs/entrypoint.sh"
        ).read_text(encoding="utf-8")
        documented_contract = "\n".join((guide, image_guide, dockerfile))

        for required in (
            "127.0.0.1:17892",
            "mirrors.tuna.tsinghua.edu.cn/debian",
            "mirrors.ustc.edu.cn/debian",
            "deb.debian.org/debian",
            "asterinas/asterinas:0.18.0-20260702",
            "asterinas/asterinas:0.18.0-20260702-riscv-rootfs",
            "debootstrap",
            "qemu-user-static",
            "proot",
            "debian-archive-keyring",
            "libc6-dev-riscv64-cross",
            "linux-libc-dev-riscv64-cross",
            "cpio",
            "e2fsprogs",
            "curl",
            "gpgv",
            "Do not enable or register a handler on the host",
            "ASTERINAS_EXPLICIT_QEMU=1",
            "ASTERINAS_BINFMT_ROOT",
            "host_binfmt=unchanged",
            "make test_riscv_debian_rootfs_unit",
            "make test_riscv_debian_rootfs_gate",
            "-nic none",
            "rootfs-manifest.json",
            "packages.lock",
            "boot1.serial.log",
            "boot2.serial.log",
            "result.json",
            "final_root_sha256",
            "U-Boot exposes only `ethernet@50400000`",
            "--host-interface enp12s0 --load-transport mmc",
            "--reboot-after 420",
        ):
            with self.subTest(required=required):
                self.assertIn(required, documented_contract)
        self.assertNotIn("update-binfmts", entrypoint)
        self.assertNotIn("mount -t binfmt_misc", entrypoint)

    def test_runtime_make_target_requires_artifacts_and_never_builds_rootfs(
        self,
    ) -> None:
        missing = subprocess.run(
            ["make", "--no-print-directory", "test_riscv_debian_rootfs_gate"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("DEBIAN_KERNEL is required", missing.stderr)

        variable_names = (
            "DEBIAN_KERNEL",
            "DEBIAN_UBOOT",
            "DEBIAN_DTB",
            "DEBIAN_STAGE1_INITRAMFS",
            "DEBIAN_ROOT_IMAGE",
            "DEBIAN_ROOT_MANIFEST",
            "DEBIAN_PACKAGES_LOCK",
            "DEBIAN_PACKAGE_CHECKSUMS",
            "DEBIAN_GATE_OUTPUT",
        )
        dry_run = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-n",
                "test_riscv_debian_rootfs_gate",
                *(f"{name}=/tmp/{name.lower()}" for name in variable_names),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        self.assertIn("python3 -m tools.riscv.debian.rootfs.rootfs_gate", dry_run)
        for forbidden in ("build_rootfs.sh", "debootstrap", "http://", "https://"):
            self.assertNotIn(forbidden, dry_run)


if __name__ == "__main__":
    unittest.main()
