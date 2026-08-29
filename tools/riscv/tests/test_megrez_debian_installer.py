# SPDX-License-Identifier: MPL-2.0

import gzip
import hashlib
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.riscv.debian.rootfs.megrez_installer import (
    InstallerError,
    build_archive,
    build_network_archive,
    build_verify_archive,
    main,
    parse_newc,
    plan_chunks,
    render_init,
    render_network_init,
    render_verify_init,
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
    "reboot",
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
    entries.extend(
        (f"usr/bin/{command}", b"busybox", 0o120777)
        for command in ("mkfifo", "rm", "tee", "wget")
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
        self.assertNotIn(
            f'count="{len(b"a" * 4096) // 4096}" 2>/dev/null | sha256sum',
            script,
        )
        self.assertIn(f'verified_sha256="{root_hash}"', script)
        sync_offset = script.index("sync || fail final-sync")
        pass_offset = script.index("DEBIAN_INSTALL_PASS")
        reboot_offset = script.index("reboot -f")
        self.assertLess(sync_offset, pass_offset)
        self.assertLess(pass_offset, reboot_offset)
        self.assertIn("reboot -f\nfail reboot-returned", script)

    def test_render_init_requires_chunks_to_cover_the_image_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "root.ext2"
            image.write_bytes(b"a" * 4096 + b"b" * 4096)
            chunks = plan_chunks(image, chunk_size=4096)
        root_hash = hashlib.sha256(b"a" * 4096 + b"b" * 4096).hexdigest()
        overlapping = (chunks[0], replace(chunks[1], offset=0))

        with self.assertRaisesRegex(InstallerError, "contiguous image order"):
            render_init(root_hash, 8192, overlapping)

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

    def test_network_installer_hashes_the_exact_stream_written_to_the_root(self):
        root_hash = hashlib.sha256(b"a" * 4096).hexdigest()
        root_url = "http://10.100.19.216:8080/debian-root.ext2.gz"

        script = render_network_init(root_hash, 4096, root_url).decode()

        self.assertIn("mkdir -p /proc /sys /dev /run", script)
        self.assertIn("asterinas.mmc_write_partition2", script)
        self.assertIn(f"asterinas.debian_install_sha256={root_hash}", script)
        self.assertIn(f"wget -T 30 -O - '{root_url}'", script)
        self.assertIn("| gzip -dc", script)
        self.assertLess(script.index("wget -T 30"), script.index("gzip -dc"))
        self.assertIn('mkfifo "$hash_fifo"', script)
        self.assertIn('sha256sum < "$hash_fifo" > "$hash_result" &', script)
        self.assertLess(script.index("gzip -dc"), script.index('tee "$hash_fifo"'))
        self.assertLess(
            script.index('tee "$hash_fifo"'), script.index('dd of="$target"')
        )
        self.assertIn(
            'dd of="$target" bs=1048576 iflag=fullblock conv=notrunc count=1',
            script,
        )
        self.assertIn('wait "$hash_pid"', script)
        self.assertIn(f'[ "$1" = "{root_hash}" ]', script)
        self.assertNotIn('dd if="$target"', script)
        self.assertIn("DEBIAN_INSTALL_FETCH_OK", script)
        self.assertIn("DEBIAN_INSTALL_PASS", script)
        self.assertLess(script.index("DEBIAN_INSTALL_PASS"), script.index("reboot -f"))

        for unsafe in (
            "https://10.100.19.216/root.ext2",
            "http://user@10.100.19.216/root.ext2",
            "http://10.100.19.216/root.ext2#fragment",
            "http://10.100.19.216/root.ext2\nreboot",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(InstallerError):
                render_network_init(root_hash, 4096, unsafe)

    def test_buffered_tee_pipeline_hashes_exact_bytes_and_propagates_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool_directory = root / "bin"
            tool_directory.mkdir()
            for command in ("dd", "gzip", "mkfifo", "rm", "sha256sum", "tee"):
                (tool_directory / command).symlink_to("/usr/bin/busybox")
            source = bytes(range(256)) * 16
            compressed = root / "root.ext2.gz"
            with compressed.open("wb") as compressed_file:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=compressed_file, mtime=0
                ) as archive:
                    archive.write(source)
            target = root / "target"
            fifo = root / "hash.fifo"
            result = root / "hash.result"
            command = """
rm -f "$FIFO" "$RESULT"
mkfifo "$FIFO"
sha256sum < "$FIFO" > "$RESULT" &
hash_pid=$!
gzip -dc "$SOURCE" | tee "$FIFO" | dd of="$TARGET" bs=1048576 iflag=fullblock conv=notrunc count=1
pipeline_status=$?
wait "$hash_pid"
hash_status=$?
[ "$pipeline_status" = 0 ] && [ "$hash_status" = 0 ]
"""
            environment = {
                **os.environ,
                "FIFO": str(fifo),
                "RESULT": str(result),
                "SOURCE": str(compressed),
                "TARGET": str(target),
                "PATH": str(tool_directory),
            }

            completed = subprocess.run(
                ["/usr/bin/busybox", "ash", "-o", "pipefail", "-c", command],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(target.read_bytes(), source)
            self.assertEqual(
                result.read_text().split(), [hashlib.sha256(source).hexdigest(), "-"]
            )

            compressed.write_bytes(compressed.read_bytes()[:-8])
            failed = subprocess.run(
                ["/usr/bin/busybox", "ash", "-o", "pipefail", "-c", command],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(failed.returncode, 0)

    def test_network_archive_is_deterministic_and_does_not_embed_root_chunks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cpio"
            image = root / "root.ext2"
            first = root / "first.cpio"
            second = root / "second.cpio"
            base.write_bytes(_archive(*_busybox_base_entries()))
            image.write_bytes(b"a" * 4096)
            image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
            root_url = "http://10.100.19.216:8080/debian-root.ext2.gz"

            build_network_archive(base, image, first, image_hash, root_url)
            build_network_archive(base, image, second, image_hash, root_url)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            entries = {entry.name: entry for entry in parse_newc(first.read_bytes())}
            self.assertIn(root_url.encode(), entries["init"].data)
            self.assertFalse(
                any(name.startswith("installer/chunks") for name in entries)
            )

            base.write_bytes(
                _archive(
                    *(
                        entry
                        for entry in _busybox_base_entries()
                        if entry[0] != "usr/bin/tee"
                    )
                )
            )
            with self.assertRaisesRegex(
                InstallerError, "missing executable installer runtime"
            ):
                build_network_archive(base, image, first, image_hash, root_url)
            self.assertLess(first.stat().st_size, base.stat().st_size + 16 * 1024)

    def test_verify_init_reads_exact_root_without_write_authority(self):
        root_hash = hashlib.sha256(b"a" * (1024 * 1024)).hexdigest()

        script = render_verify_init(root_hash, 1024 * 1024).decode()

        self.assertIn('dd if="$target" bs=1048576 iflag=fullblock count=1', script)
        self.assertIn(f'[ "$1" = "{root_hash}" ]', script)
        self.assertIn("DEBIAN_VERIFY_PASS", script)
        self.assertIn("DEBIAN_VERIFY_FAIL", script)
        self.assertIn("printf '%s\\n' \"$1\" >/dev/ttyS0", script)
        self.assertIn("reboot -f", script)
        self.assertNotIn("dd of=", script)
        self.assertNotIn("asterinas.mmc_write_partition2", script)

    def test_verify_archive_is_deterministic_and_replaces_only_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cpio"
            image = root / "root.ext2"
            first = root / "first.cpio"
            second = root / "second.cpio"
            base.write_bytes(_archive(*_busybox_base_entries()))
            image.write_bytes(b"a" * (1024 * 1024))
            image_hash = hashlib.sha256(image.read_bytes()).hexdigest()

            build_verify_archive(base, image, first, image_hash)
            build_verify_archive(base, image, second, image_hash)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            entries = {entry.name: entry for entry in parse_newc(first.read_bytes())}
            self.assertIn(b"DEBIAN_VERIFY_PASS", entries["init"].data)
            self.assertNotIn(b"dd of=", entries["init"].data)
            self.assertEqual(entries["init"].mode & 0o7777, 0o755)

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

    def test_cli_selects_the_network_installer_without_embedding_the_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "root.ext2"
            with image.open("wb") as image_file:
                image_file.truncate(1024 * 1024 * 1024)
            root_url = "http://10.100.19.216:8080/debian-root.ext2.gz"
            arguments = [
                "--base-cpio",
                str(root / "base.cpio"),
                "--root-image",
                str(image),
                "--manifest",
                str(root / "manifest.json"),
                "--packages-lock",
                str(root / "packages.lock"),
                "--root-url",
                root_url,
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
                    "tools.riscv.debian.rootfs.megrez_installer.build_network_archive"
                ) as build,
            ):
                self.assertEqual(main(arguments), 0)

            self.assertEqual(build.call_args.args[3:], ("a" * 64, root_url))

    def test_cli_selects_read_only_verify_archive(self):
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
                "--verify-only",
                "--output",
                str(root / "verify.cpio"),
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
                    "tools.riscv.debian.rootfs.megrez_installer.build_verify_archive"
                ) as build,
            ):
                self.assertEqual(main(arguments), 0)

            self.assertEqual(build.call_args.args[3], "a" * 64)


if __name__ == "__main__":
    unittest.main()
