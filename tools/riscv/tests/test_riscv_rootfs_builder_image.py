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
ROOTFS_BUILDER = (
    REPOSITORY_ROOT / "tools" / "riscv" / "debian" / "rootfs" / "build_rootfs.sh"
)


class RiscvRootfsBuilderImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        cls.entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")
        cls.docker_readme = DOCKER_README.read_text(encoding="utf-8")
        cls.makefile = MAKEFILE.read_text(encoding="utf-8")
        cls.rootfs_builder = ROOTFS_BUILDER.read_text(encoding="utf-8")

    def test_dockerfile_is_derived_and_installs_build_contract(self) -> None:
        self.assertIn(
            "ARG BASE_IMAGE=asterinas/asterinas:0.18.0-20260702",
            self.dockerfile,
        )
        self.assertIn("ARG DEBIAN_ARCHIVE_KEYRING_VERSION=2025.1", self.dockerfile)
        self.assertIn(
            "ARG DEBIAN_ARCHIVE_KEYRING_SHA256="
            "9ea7778e443144ca490668737a8ab22dd3e748bb99e805e22ec055abeb3c7fac",
            self.dockerfile,
        )
        for package in (
            "ca-certificates",
            "cpio",
            "curl",
            "debootstrap",
            "device-tree-compiler",
            "e2fsprogs",
            "fontconfig",
            "gcc-riscv64-linux-gnu",
            "gpgv",
            "libc6-dev-riscv64-cross",
            "linux-libc-dev-riscv64-cross",
            "proot",
            "qemu-system-misc",
            "qemu-user-static",
            "systemd",
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

    def test_entrypoint_defaults_to_non_mutating_explicit_qemu(self) -> None:
        for required_text in (
            'ASTERINAS_EXPLICIT_QEMU="${ASTERINAS_EXPLICIT_QEMU:-1}"',
            "explicit-proot",
            "host_binfmt=unchanged",
            "debootstrap proot qemu-riscv64-static",
            "fc-cache journalctl systemd-sysusers",
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
        self.assertNotIn("update-binfmts", self.entrypoint)
        self.assertNotIn("mount -t binfmt_misc", self.entrypoint)
        self.assertNotIn("binfmt-support", self.dockerfile)

    def test_explicit_qemu_proot_virtualizes_guest_credentials(self) -> None:
        self.assertIn(
            'command proot -0 -w / -q "$(command -v "$ARCH_EMULATOR")"',
            self.rootfs_builder,
        )

    def test_default_architecture_matches_the_architecture_table(self) -> None:
        # The builder resolves the default target's output paths without a
        # Python interpreter, so its shell-side default has to agree with the
        # architecture table that every other target comes from.
        from tools.debian.rootfs.arch import get_architecture

        default = get_architecture("riscv64")
        self.assertIn(
            f'ARCH_SLUG_DEFAULT="{default.arch_slug}"', self.rootfs_builder
        )
        self.assertIn(f'ARCHITECTURE="{default.debian_arch}"', self.rootfs_builder)

    def test_documentation_explains_runtime_contract(self) -> None:
        for required_text in (
            "explicit-QEMU",
            "qemu-riscv64-static",
            "qemu-system-riscv64",
            "changes the host's `binfmt_misc` registration",
            "target/debian-riscv/cache",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, self.readme)
        self.assertIn("build_riscv_rootfs_image", self.docker_readme)

    def test_image_is_fsck_repaired_and_gated(self) -> None:
        # `mke2fs -d` populates the tree with e2fsprogs' own copier rather than a
        # mounted filesystem, and it does not always get the block and inode
        # bitmaps right. A block owned by a live inode can be left marked free,
        # so a later write allocates it, two inodes own it, and one of them is
        # destroyed -- which shows up far away as an unrelated file reading back
        # wrong bytes. The builder must repair the image and then refuse to
        # publish one that is still inconsistent.
        self.assertIn("e2fsck -fy", self.rootfs_builder)
        self.assertIn('e2fsck -fn "$root_image"', self.rootfs_builder)
        self.assertIn(
            "root image is not a consistent filesystem", self.rootfs_builder
        )
        # The repair only runs if e2fsck is present, so the tool contract has to
        # name it.
        tools_line = next(
            line
            for line in self.rootfs_builder.splitlines()
            if "gpgv" in line and "printf" in line
        )
        self.assertIn("e2fsck", tools_line)

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
