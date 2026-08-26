# SPDX-License-Identifier: MPL-2.0

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv.debian.rootfs.megrez_installer import (
    InstallerError,
    build_archive,
    main,
    parse_newc,
    plan_chunks,
    render_init,
)


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def _entry(name: str, data: bytes, *, mode: int = 0o100644, ino: int = 1) -> bytes:
    encoded_name = name.encode() + b"\0"
    fields = (ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded_name), 0)
    header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
    name_padding = b"\0" * (-(len(header) + len(encoded_name)) % 4)
    return header + encoded_name + name_padding + _pad4(data)


def _archive(*entries: tuple[str, bytes, int]) -> bytes:
    result = b"".join(
        _entry(name, data, mode=mode, ino=index + 1)
        for index, (name, data, mode) in enumerate(entries)
    )
    return result + _entry("TRAILER!!!", b"", ino=len(entries) + 1)


_INSTALLER_COMMANDS = (
    "blockdev",
    "cat",
    "dd",
    "gzip",
    "mkdir",
    "mount",
    "sha256sum",
    "sleep",
    "sync",
)


def _busybox_base_entries() -> tuple[tuple[str, bytes, int], ...]:
    entries = [
        (".", b"", 0o040755),
        ("init", b"old-init", 0o100755),
        ("bin", b"usr/bin", 0o120777),
        ("usr", b"", 0o040755),
        ("usr/bin", b"", 0o040755),
        ("usr/bin/busybox", b"elf", 0o100555),
        ("usr/bin/sh", b"busybox", 0o120777),
    ]
    entries.extend(
        (f"usr/bin/{command}", b"busybox", 0o120777) for command in _INSTALLER_COMMANDS
    )
    return tuple(entries)


class MegrezDebianInstallerTests(unittest.TestCase):
    def test_parse_newc_preserves_entries_and_rejects_unsafe_names(self):
        archive = _archive(
            (".", b"", 0o040755),
            ("init", b"old", 0o100755),
            ("bin/sh", b"busybox", 0o100755),
        )
        entries = parse_newc(archive)
        self.assertEqual([entry.name for entry in entries], [".", "init", "bin/sh"])
        self.assertEqual(entries[1].data, b"old")

        for unsafe in ("../escape", "/absolute", "a/../../escape", "", "."):
            with self.subTest(unsafe=unsafe):
                bad = _archive((".", b"", 0o040755), (unsafe, b"x", 0o100644))
                with self.assertRaises(InstallerError):
                    parse_newc(bad)

        duplicate = _archive(
            (".", b"", 0o040755),
            ("init", b"first", 0o100755),
            ("init", b"second", 0o100755),
        )
        with self.assertRaises(InstallerError):
            parse_newc(duplicate)

    def test_plan_chunks_is_deterministic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "root.ext2"
            image.write_bytes(b"a" * 4096 + b"b" * 4096)

            first = plan_chunks(image, chunk_size=4096)
            second = plan_chunks(image, chunk_size=4096)

        self.assertEqual(first, second)
        self.assertEqual([chunk.offset for chunk in first], [0, 4096])
        self.assertEqual(
            b"".join(gzip.decompress(c.compressed) for c in first),
            b"a" * 4096 + b"b" * 4096,
        )
        self.assertEqual(
            first[0].uncompressed_sha256, hashlib.sha256(b"a" * 4096).hexdigest()
        )
        self.assertEqual(
            first[0].compressed_sha256, hashlib.sha256(first[0].compressed).hexdigest()
        )

    def test_render_init_freezes_guards_resume_and_terminal_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "root.ext2"
            image.write_bytes(b"a" * 4096)
            chunks = plan_chunks(image, chunk_size=4096)
        root_hash = hashlib.sha256(b"a" * 4096).hexdigest()

        script = render_init(root_hash, len(b"a" * 4096), chunks).decode()

        self.assertIn("asterinas.mmc_write_partition2", script)
        self.assertIn(f"asterinas.debian_install_sha256={root_hash}", script)
        self.assertIn("blockdev --getsize64", script)
        self.assertIn("set -o pipefail", script)
        self.assertIn("DEBIAN_INSTALL_CHUNK_OK", script)
        self.assertIn("DEBIAN_INSTALL_CHUNK_SKIP", script)
        self.assertIn("DEBIAN_INSTALL_PASS", script)
        self.assertIn("DEBIAN_INSTALL_FAIL", script)
        self.assertNotIn("reboot", script)

    def test_build_archive_is_reproducible_and_replaces_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cpio"
            image = root / "root.ext2"
            first = root / "first.cpio"
            second = root / "second.cpio"
            base.write_bytes(_archive(*_busybox_base_entries()))
            image.write_bytes(b"a" * 4096 + b"b" * 4096)
            image_hash = hashlib.sha256(image.read_bytes()).hexdigest()

            build_archive(base, image, first, image_hash, chunk_size=4096)
            build_archive(base, image, second, image_hash, chunk_size=4096)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            entries = {entry.name: entry for entry in parse_newc(first.read_bytes())}
            self.assertTrue(entries["init"].data.startswith(b"#!/bin/sh"))
            self.assertEqual(entries["init"].mode & 0o7777, 0o755)
            self.assertIn("installer/chunks.tsv", entries)
            self.assertIn("installer/chunks/0000.gz", entries)
            self.assertIn("installer/chunks/0001.gz", entries)

    def test_build_archive_rejects_missing_installer_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cpio"
            image = root / "root.ext2"
            output = root / "installer.cpio"
            image.write_bytes(b"a" * 4096)
            image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
            output.write_bytes(b"published")

            required_paths = ("usr/bin/sh",) + tuple(
                f"usr/bin/{command}" for command in _INSTALLER_COMMANDS
            )
            for missing in required_paths:
                with self.subTest(missing=missing):
                    output.write_bytes(b"published")
                    entries = tuple(
                        entry
                        for entry in _busybox_base_entries()
                        if entry[0] != missing
                    )
                    base.write_bytes(_archive(*entries))
                    with self.assertRaisesRegex(
                        InstallerError, "missing executable installer runtime"
                    ):
                        build_archive(base, image, output, image_hash, chunk_size=4096)
                    self.assertEqual(output.read_bytes(), b"published")

    def test_build_archive_accepts_absolute_internal_runtime_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cpio"
            image = root / "root.ext2"
            output = root / "installer.cpio"
            entries = tuple(
                (name, b"/usr/bin" if name == "bin" else data, mode)
                for name, data, mode in _busybox_base_entries()
            )
            base.write_bytes(_archive(*entries))
            image.write_bytes(b"a" * 4096)

            try:
                build_archive(
                    base,
                    image,
                    output,
                    hashlib.sha256(image.read_bytes()).hexdigest(),
                    chunk_size=4096,
                )
            except InstallerError as error:
                self.fail(f"absolute in-root symlink was rejected: {error}")

            self.assertTrue(output.is_file())

    def test_build_archive_preserves_existing_output_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cpio"
            image = root / "root.ext2"
            output = root / "installer.cpio"
            base.write_bytes(_archive((".", b"", 0o040755), ("init", b"old", 0o100755)))
            image.write_bytes(b"a" * 4096)
            output.write_bytes(b"published")

            with self.assertRaises(InstallerError):
                build_archive(base, image, output, "0" * 64, chunk_size=4096)

            self.assertEqual(output.read_bytes(), b"published")

    def test_cli_uses_the_manifest_root_image_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "root.ext2"
            with image.open("wb") as image_file:
                image_file.truncate(1024 * 1024 * 1024)
            arguments = [
                "--base-cpio",
                str(root / "base.cpio"),
                "--root-image",
                str(image),
                "--manifest",
                str(root / "manifest.json"),
                "--packages-lock",
                str(root / "packages.lock"),
                "--output",
                str(root / "installer.cpio"),
            ]
            identity = SimpleNamespace(root_image_sha256="a" * 64)
            with (
                mock.patch(
                    "tools.riscv.debian.rootfs.megrez_installer.load_manifest",
                    return_value=identity,
                ),
                mock.patch(
                    "tools.riscv.debian.rootfs.megrez_installer.validate_frozen_root"
                ),
                mock.patch(
                    "tools.riscv.debian.rootfs.megrez_installer.build_archive"
                ) as build,
            ):
                self.assertEqual(main(arguments), 0)

            self.assertEqual(build.call_args.args[3], "a" * 64)


if __name__ == "__main__":
    unittest.main()
