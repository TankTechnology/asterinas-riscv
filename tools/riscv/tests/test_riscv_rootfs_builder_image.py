#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
IMAGE_DIRECTORY = REPOSITORY_ROOT / "tools" / "docker" / "riscv-rootfs"
DOCKERFILE = IMAGE_DIRECTORY / "Dockerfile"
ENTRYPOINT = IMAGE_DIRECTORY / "entrypoint.sh"
README = IMAGE_DIRECTORY / "README.md"
DOCKER_README = REPOSITORY_ROOT / "tools" / "docker" / "README.md"
MAKEFILE = REPOSITORY_ROOT / "Makefile"


class RiscvRootfsBuilderImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        cls.entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")
        cls.docker_readme = DOCKER_README.read_text(encoding="utf-8")
        cls.makefile = MAKEFILE.read_text(encoding="utf-8")

    def test_dockerfile_is_derived_and_installs_build_contract(self) -> None:
        self.assertIn(
            "ARG BASE_IMAGE=asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached",
            self.dockerfile,
        )
        self.assertIn("ARG DEBIAN_ARCHIVE_KEYRING_VERSION=2025.1", self.dockerfile)
        self.assertIn(
            "ARG DEBIAN_ARCHIVE_KEYRING_SHA256="
            "9ea7778e443144ca490668737a8ab22dd3e748bb99e805e22ec055abeb3c7fac",
            self.dockerfile,
        )
        for package in (
            "binfmt-support",
            "ca-certificates",
            "cpio",
            "curl",
            "debootstrap",
            "device-tree-compiler",
            "e2fsprogs",
            "gcc-riscv64-linux-gnu",
            "gpgv",
            "libc6-dev-riscv64-cross",
            "linux-libc-dev-riscv64-cross",
            "qemu-system-misc",
            "qemu-user-static",
            "util-linux",
        ):
            with self.subTest(package=package):
                self.assertIn(f"        {package}", self.dockerfile)
        self.assertIn(
            "debian-archive-keyring_${DEBIAN_ARCHIVE_KEYRING_VERSION}_all.deb",
            self.dockerfile,
        )
        self.assertIn(
            'ENTRYPOINT ["/usr/local/bin/asterinas-riscv-rootfs-entrypoint"]',
            self.dockerfile,
        )

    def test_entrypoint_enforces_binfmt_keyring_and_tools(self) -> None:
        for required_text in (
            "/proc/sys/fs/binfmt_misc",
            "update-binfmts --enable qemu-riscv64",
            "grep -q '^flags:.*F'",
            "qemu-riscv64-static",
            "/usr/share/keyrings/debian-archive-keyring.gpg",
            "MINIMUM_DEBIAN_KEYRING_VERSION=2025.1",
            "dpkg --compare-versions",
            "--check",
            "ASTERINAS_RISCV_ROOTFS_ENV_PASS",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, self.entrypoint)

    def test_documentation_explains_runtime_contract(self) -> None:
        for required_text in (
            "binfmt_misc",
            "qemu-riscv64-static",
            "qemu-system-riscv64",
            "--privileged",
            "target/debian-riscv/cache",
            "F` (fix-binary)",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, self.readme)
        self.assertIn("build_riscv_rootfs_image", self.docker_readme)

    def test_makefile_exposes_explicit_image_target(self) -> None:
        for required_text in (
            "RISCV_ROOTFS_BASE_IMAGE ?=",
            "RISCV_ROOTFS_IMAGE ?=",
            "RISCV_ROOTFS_DOCKERFILE ?= tools/docker/riscv-rootfs/Dockerfile",
            ".PHONY: build_riscv_rootfs_image",
            "docker build --pull=false",
            '--build-arg BASE_IMAGE="$(RISCV_ROOTFS_BASE_IMAGE)"',
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, self.makefile)


if __name__ == "__main__":
    unittest.main()
