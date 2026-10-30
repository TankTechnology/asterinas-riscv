#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for descriptor-pinned rootfs publication and caching."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs import fsops


class Stage1PublicationTests(unittest.TestCase):
    def test_init_and_archive_are_published_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init = root / "init"
            archive = root / "initramfs.cpio"
            output = root / "published"
            init.write_bytes(b"\x7fELF-stage1")
            archive.write_bytes(b"newc-archive")

            fsops.publish_stage1(output, init, archive)

            self.assertEqual((output / "init").read_bytes(), init.read_bytes())
            self.assertEqual(
                (output / "initramfs.cpio").read_bytes(),
                archive.read_bytes(),
            )
            self.assertEqual((output / "init").stat().st_mode & 0o777, 0o755)
            self.assertEqual(
                (output / "initramfs.cpio").stat().st_mode & 0o777,
                0o644,
            )

    def test_unsafe_archive_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init = root / "init"
            archive = root / "archive"
            init.write_bytes(b"init")
            archive.write_bytes(b"archive")
            output = root / "published"

            for name in ("", ".", "..", "init", "nested/name", "bad\nname"):
                with self.subTest(name=name), self.assertRaises(
                    fsops.FsOpsError
                ):
                    fsops.publish_stage1(output, init, archive, name)
            self.assertFalse(output.exists())


class RootfsSetTests(unittest.TestCase):
    def make_source_root(self, root: Path) -> None:
        (root / "source-metadata").mkdir(parents=True)
        for relative in (
            "debian-root.ext2",
            "rootfs-manifest.json",
            "packages.lock",
            "source-metadata/InRelease",
            "source-metadata/package-checksums",
        ):
            path = root / relative
            path.write_bytes(relative.encode())

    def test_published_set_matches_the_declared_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            self.make_source_root(source_root)
            output = root / "output"

            fsops.publish_set(output, source_root)

            for relative in (
                "debian-root.ext2",
                "rootfs-manifest.json",
                "packages.lock",
                "source-metadata/InRelease",
                "source-metadata/package-checksums",
            ):
                with self.subTest(path=relative):
                    self.assertEqual(
                        (output / relative).read_bytes(),
                        relative.encode(),
                    )
            self.assertFalse((output / "source-metadata/Security-InRelease").exists())

    def test_security_inrelease_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            self.make_source_root(source_root)
            (source_root / "source-metadata/Security-InRelease").write_bytes(b"security")
            output = root / "output"

            fsops.publish_set(
                output,
                source_root,
                include_security_inrelease=True,
            )

            self.assertEqual(
                (output / "source-metadata/Security-InRelease").read_bytes(),
                b"security",
            )


class CacheTests(unittest.TestCase):
    def test_cache_admission_is_content_addressed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "package.deb"
            source.write_bytes(b"package-bytes")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            cache = root / "cache"

            fsops.admit_cache_entry(cache, source, digest)
            fsops.admit_cache_entry(cache, source, digest)

            cached = cache / "sha256" / digest[:2] / f"{digest}.deb"
            self.assertEqual(cached.read_bytes(), source.read_bytes())
            self.assertEqual(cached.stat().st_mode & 0o777, 0o444)

    def test_invalid_hash_is_rejected_before_touching_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "package.deb"
            source.write_bytes(b"package-bytes")
            cache = root / "cache"

            with self.assertRaisesRegex(fsops.FsOpsError, "64 lowercase"):
                fsops.admit_cache_entry(cache, source, "ABC")
            self.assertFalse(cache.exists())

    def test_changed_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "package.deb"
            source.write_bytes(b"package-bytes")
            cache = root / "cache"
            wrong = "0" * 64

            with self.assertRaisesRegex(fsops.FsOpsError, "package changed"):
                fsops.admit_cache_entry(cache, source, wrong)


if __name__ == "__main__":
    unittest.main()
