#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for one reset-safe Megrez extlinux generation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import zlib

from tools.riscv import megrez_boot_manifest as manifest


ARTIFACT_BYTES = {
    "kernel": b"current-sv39-kernel",
    "initramfs": b"lightweight-stage1",
    "megrez_dtb": b"megrez-device-tree",
}


def _identity(name: str):
    payload = ARTIFACT_BYTES[name]
    return SimpleNamespace(
        name=name,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        crc32=f"{zlib.crc32(payload):08x}",
    )


def _plan():
    return SimpleNamespace(
        plan_sha256="a" * 64,
        bootargs="console=ttyS0 init=/init asterinas.reboot_after=90",
        artifacts=tuple(
            _identity(name) for name in ("kernel", "initramfs", "megrez_dtb")
        ),
        validate=lambda: None,
    )


def _paths(plan=None):
    identities = {
        identity.name: identity for identity in (plan or _plan()).artifacts
    }
    return {
        "kernel": f"/asterinas-{identities['kernel'].sha256[:12]}.booti",
        "initramfs": f"/stage1-{identities['initramfs'].sha256[:12]}.cpio",
        "megrez_dtb": f"/dtb/megrez-{identities['megrez_dtb'].sha256[:12]}.dtb",
    }


def _rendered(plan=None, paths=None):
    selected_plan = plan or _plan()
    selected_paths = paths or _paths(selected_plan)
    return (
        f"default asterinas-{selected_plan.plan_sha256[:12]}\n"
        f"label asterinas-{selected_plan.plan_sha256[:12]}\n"
        f"linux {selected_paths['kernel']}\n"
        f"initrd {selected_paths['initramfs']}\n"
        f"fdt {selected_paths['megrez_dtb']}\n"
        f"append {selected_plan.bootargs}\n"
    ).encode()


class ExtlinuxGenerationTests(unittest.TestCase):
    def test_parses_and_renders_one_plan_bound_generation(self) -> None:
        plan = _plan()
        generation = manifest.ExtlinuxGeneration.from_bytes(_rendered(plan))

        generation.validate_against_plan(plan)

        self.assertEqual(generation.canonical_bytes(), _rendered(plan))
        self.assertEqual(generation.artifact_paths, _paths(plan))

    def test_recorded_stale_generation_reports_missing_kernel_first(self) -> None:
        stale = (
            "default asterinas\n"
            "label asterinas\n"
            "linux /asterinas-sv48-fe1dcfdf7.booti\n"
            "initrd /initramfs-full-712208ba4.cpio\n"
            "fdt /eic7700-milkv-megrez.dtb\n"
            "append console=ttyS0 init=/init\n"
        ).encode()
        generation = manifest.ExtlinuxGeneration.from_bytes(stale)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                manifest.BootManifestError,
                "kernel: staged file is missing",
            ):
                generation.validate_staged_directory(Path(temporary), _plan())

    def test_rejects_missing_or_duplicate_directives(self) -> None:
        valid_lines = _rendered().decode().splitlines()
        for directive in ("default", "label", "linux", "initrd", "fdt", "append"):
            with self.subTest(missing=directive):
                encoded = "\n".join(
                    line for line in valid_lines if not line.startswith(f"{directive} ")
                ).encode() + b"\n"
                with self.assertRaisesRegex(
                    manifest.BootManifestError,
                    f"{directive} directive",
                ):
                    manifest.ExtlinuxGeneration.from_bytes(encoded)

            with self.subTest(duplicate=directive):
                line = next(
                    line for line in valid_lines if line.startswith(f"{directive} ")
                )
                encoded = _rendered() + f"{line}\n".encode()
                with self.assertRaisesRegex(
                    manifest.BootManifestError,
                    f"{directive} directive",
                ):
                    manifest.ExtlinuxGeneration.from_bytes(encoded)

    def test_rejects_unknown_directives_labels_and_unsafe_paths(self) -> None:
        replacements = (
            (b"append ", b"timeout 5\nappend ", "unknown extlinux directive"),
            (b"label asterinas-", b"label other-", "default and label differ"),
            (b"linux /asterinas-", b"linux /../asterinas-", "unsafe linux path"),
        )
        for old, new, message in replacements:
            with self.subTest(message=message):
                with self.assertRaisesRegex(manifest.BootManifestError, message):
                    manifest.ExtlinuxGeneration.from_bytes(
                        _rendered().replace(old, new, 1)
                    )

    def test_rejects_mutable_or_wrong_identity_names(self) -> None:
        plan = _plan()
        mutable = _paths(plan)
        mutable["kernel"] = "/asterinas-current.booti"
        wrong = _paths(plan)
        wrong["initramfs"] = "/stage1-deadbeefdead.cpio"

        for paths, message in (
            (mutable, "kernel: immutable SHA-256 prefix is absent"),
            (wrong, "initramfs: immutable SHA-256 prefix is absent"),
        ):
            with self.subTest(message=message):
                generation = manifest.ExtlinuxGeneration.from_bytes(
                    _rendered(plan, paths)
                )
                with self.assertRaisesRegex(manifest.BootManifestError, message):
                    generation.validate_against_plan(plan)

    def test_staged_files_must_match_all_plan_identities(self) -> None:
        plan = _plan()
        generation = manifest.ExtlinuxGeneration.from_bytes(_rendered(plan))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, relative_path in generation.artifact_paths.items():
                destination = root / relative_path.removeprefix("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(ARTIFACT_BYTES[name])

            self.assertEqual(
                generation.validate_staged_directory(root, plan),
                ("kernel", "initramfs", "megrez_dtb"),
            )

            kernel = root / generation.artifact_paths["kernel"].removeprefix("/")
            kernel.write_bytes(b"wrong-current-sv39-kernel")
            with self.assertRaisesRegex(
                manifest.BootManifestError,
                "kernel: staged size mismatch",
            ):
                generation.validate_staged_directory(root, plan)

    def test_staged_symlink_is_never_followed(self) -> None:
        plan = _plan()
        generation = manifest.ExtlinuxGeneration.from_bytes(_rendered(plan))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, relative_path in generation.artifact_paths.items():
                destination = root / relative_path.removeprefix("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(ARTIFACT_BYTES[name])
            kernel = root / generation.artifact_paths["kernel"].removeprefix("/")
            target = root / "kernel-target"
            target.write_bytes(ARTIFACT_BYTES["kernel"])
            kernel.unlink()
            kernel.symlink_to(target)

            with self.assertRaisesRegex(
                manifest.BootManifestError,
                "kernel: staged file is not a regular non-symlink file",
            ):
                generation.validate_staged_directory(root, plan)


if __name__ == "__main__":
    unittest.main()
