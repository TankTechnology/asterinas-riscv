"""Fast contracts for the simulation-first Megrez debug workflow."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from tools.riscv.megrez_debug_contract import (
    MAX_ARTIFACT_BYTES,
    ArtifactIdentity,
    DebugContractError,
)


class MegrezDebugArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_identity_hashes_one_held_regular_file(self) -> None:
        artifact = self.directory / "kernel"
        replacement = self.directory / "replacement"
        payload = b"asterinas-megrez-kernel"
        artifact.write_bytes(payload)
        replacement.write_bytes(b"different-path-bytes")
        original_open = Path.open
        open_count = 0

        def replace_after_open(path: Path, *args: object, **kwargs: object):
            nonlocal open_count
            stream = original_open(path, *args, **kwargs)
            open_count += 1
            os.replace(replacement, artifact)
            return stream

        with mock.patch.object(Path, "open", new=replace_after_open):
            identity = ArtifactIdentity.from_path("kernel", artifact, 0x80200000)

        self.assertEqual(open_count, 1)
        self.assertEqual(identity.name, "kernel")
        self.assertEqual(identity.path, str(artifact.absolute()))
        self.assertEqual(identity.load_address, 0x80200000)
        self.assertEqual(identity.size, len(payload))
        self.assertEqual(identity.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(identity.crc32, f"{zlib.crc32(payload):08x}")
        self.assertEqual(artifact.read_bytes(), b"different-path-bytes")

    def test_identity_rejects_non_regular_and_out_of_bounds_inputs(self) -> None:
        empty = self.directory / "empty"
        empty.touch()
        directory = self.directory / "directory"
        directory.mkdir()
        target = self.directory / "target"
        target.write_bytes(b"target")
        symlink = self.directory / "symlink"
        symlink.symlink_to(target)
        oversized = self.directory / "oversized"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_ARTIFACT_BYTES + 1)

        for path, message in (
            (empty, "empty"),
            (directory, "regular non-symlink"),
            (symlink, "regular non-symlink"),
            (oversized, "64 MiB"),
        ):
            with (
                self.subTest(path=path.name),
                self.assertRaisesRegex(DebugContractError, message),
            ):
                ArtifactIdentity.from_path("kernel", path, 0x80200000)

    def test_identity_rejects_invalid_name_and_address(self) -> None:
        artifact = self.directory / "artifact"
        artifact.write_bytes(b"data")

        for name, address in (
            ("other", 0x80200000),
            ("kernel", 0),
            ("kernel", 0x80200001),
            ("kernel", True),
        ):
            with (
                self.subTest(name=name, address=address),
                self.assertRaises(DebugContractError),
            ):
                ArtifactIdentity.from_path(name, artifact, address)

    def test_identity_rejects_a_different_inode_opened_after_lstat(self) -> None:
        artifact = self.directory / "artifact"
        other = self.directory / "other"
        artifact.write_bytes(b"original")
        other.write_bytes(b"other")
        original_open = Path.open

        def open_other(_path: Path, *args: object, **kwargs: object):
            return original_open(other, *args, **kwargs)

        with (
            mock.patch.object(Path, "open", new=open_other),
            self.assertRaisesRegex(DebugContractError, "identity changed"),
        ):
            ArtifactIdentity.from_path("kernel", artifact, 0x80200000)


if __name__ == "__main__":
    unittest.main()
