# SPDX-License-Identifier: MPL-2.0

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.riscv.debian.rootfs import dev_overlay
from tools.riscv.debian.rootfs.dev_overlay import (
    OverlayError,
    build_derived_documents,
    load_overlay_spec,
    materialize_image,
    materialize_rootfs,
)


REPOSITORY_ROOT = Path(__file__).parents[3]
ROOTFS_DIRECTORY = REPOSITORY_ROOT / "tools/riscv/debian/rootfs"
BROWSER_WEB_SPEC = ROOTFS_DIRECTORY / "browser_web_dev_overlay.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_spec(directory: Path, files: list[dict[str, object]]) -> Path:
    path = directory / "overlay.json"
    path.write_text(
        json.dumps({"schema_version": 1, "profile": "browser-web", "files": files}),
        encoding="utf-8",
    )
    return path


def _debugfs(image: Path, command: str) -> str:
    return subprocess.run(
        ["debugfs", "-R", command, str(image)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@unittest.skipUnless(
    shutil.which("debugfs") and shutil.which("mke2fs"),
    "debugfs and mke2fs are required",
)
class DevelopmentOverlayTests(unittest.TestCase):
    def test_loads_exact_safe_overlay_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
            spec = _write_spec(
                root,
                [
                    {
                        "source": "runtime.sh",
                        "destination": "/usr/lib/asterinas/runtime",
                        "mode": "0755",
                    }
                ],
            )

            loaded = load_overlay_spec(spec, expected_profile="browser-web")

            self.assertEqual(loaded.profile, "browser-web")
            self.assertEqual(loaded.files[0].source, source)
            self.assertEqual(loaded.files[0].destination, "/usr/lib/asterinas/runtime")
            self.assertEqual(loaded.files[0].mode, 0o755)
            self.assertEqual(loaded.files[0].sha256, _sha256(source))
            self.assertFalse(loaded.files[0].create)

    def test_create_flag_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            for invalid in ("true", "false", 1, 0, None, [], {}):
                with self.subTest(create=invalid):
                    path = _write_spec(
                        root,
                        [
                            {
                                "source": "runtime.sh",
                                "destination": "/runtime",
                                "mode": "0755",
                                "create": invalid,
                            }
                        ],
                    )
                    with self.assertRaisesRegex(
                        OverlayError, "create must be a boolean"
                    ):
                        load_overlay_spec(path)

    def test_schema_version_must_be_exact_integer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            for version in (True, 1.0):
                with self.subTest(version=version):
                    path = _write_spec(
                        root,
                        [
                            {
                                "source": "runtime.sh",
                                "destination": "/runtime",
                                "mode": "0755",
                            }
                        ],
                    )
                    document = json.loads(path.read_text(encoding="utf-8"))
                    document["schema_version"] = version
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(OverlayError, "schema version"):
                        load_overlay_spec(path)

    def test_symlink_target_text_cannot_spoof_inode_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            staged = root / "staged"
            (staged / "Type: directory").mkdir(parents=True)
            (staged / "directory-alias").symlink_to("/Type: directory")
            (staged / "Type: regular").write_text("old", encoding="utf-8")
            (staged / "file-alias").symlink_to("/Type: regular")
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(staged),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            base_hash = _sha256(base)
            output = root / "published.ext2"
            for destination, message in (
                ("/directory-alias/runtime", "parent is not a directory"),
                ("/file-alias", "destination is not a regular file"),
            ):
                with self.subTest(destination=destination):
                    output.write_bytes(b"published")
                    spec = load_overlay_spec(
                        _write_spec(
                            root,
                            [
                                {
                                    "source": "runtime.sh",
                                    "destination": destination,
                                    "mode": "0755",
                                    "create": True,
                                }
                            ],
                        )
                    )
                    with self.assertRaisesRegex(OverlayError, message):
                        materialize_image(base, output, spec)
                    self.assertEqual(_sha256(base), base_hash)
                    self.assertEqual(output.read_bytes(), b"published")

    def test_replacement_is_deterministic_with_an_earlier_free_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            staged = root / "staged"
            staged.mkdir()
            (staged / "a-freed").write_text("unused", encoding="utf-8")
            (staged / "z-runtime").write_text("old", encoding="utf-8")
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(staged),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            earlier = re.search(r"Inode: (\d+)", _debugfs(base, "stat /a-freed"))
            original = re.search(r"Inode: (\d+)", _debugfs(base, "stat /z-runtime"))
            self.assertIsNotNone(earlier)
            self.assertIsNotNone(original)
            self.assertLess(int(earlier[1]), int(original[1]))
            subprocess.run(
                ["debugfs", "-w", "-R", "rm /a-freed", str(base)],
                check=True,
                capture_output=True,
            )
            base_hash = _sha256(base)
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/z-runtime",
                            "mode": "0755",
                        }
                    ],
                )
            )
            first = root / "first.ext2"
            first_hash = materialize_image(base, first, spec)
            current = re.search(r"Inode: (\d+)", _debugfs(first, "stat /z-runtime"))
            self.assertIsNotNone(current)
            self.assertEqual(int(current[1]), int(earlier[1]))
            time.sleep(1.1)
            second = root / "second.ext2"
            second_hash = materialize_image(base, second, spec)
            self.assertEqual(_sha256(base), base_hash)
            first_deleted = _debugfs(first, f"stat <{original[1]}>")
            second_deleted = _debugfs(second, f"stat <{original[1]}>")
            first_dtime = re.search(r"dtime: (0x[0-9a-f]+)", first_deleted)
            second_dtime = re.search(r"dtime: (0x[0-9a-f]+)", second_deleted)
            self.assertIsNotNone(first_dtime)
            self.assertIsNotNone(second_dtime)
            self.assertEqual(int(first_dtime[1], 16), 1)
            self.assertEqual(first_dtime[1], second_dtime[1])
            self.assertEqual(first_hash, second_hash)

    def test_creation_keeps_source_path_restrictions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            (root / "link.sh").symlink_to(source)
            for source_name, message in (
                (str(source), "source must remain beneath"),
                ("../runtime.sh", "source must remain beneath"),
                ("link.sh", "source must not use symlinks"),
            ):
                with self.subTest(source=source_name):
                    path = _write_spec(
                        root,
                        [
                            {
                                "source": source_name,
                                "destination": "/runtime",
                                "mode": "0755",
                                "create": True,
                            }
                        ],
                    )
                    with self.assertRaisesRegex(OverlayError, message):
                        load_overlay_spec(path)

    def test_creates_deterministic_regular_file_without_changing_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/usr/lib/asterinas/runtime",
                            "mode": "0755",
                            "create": True,
                        }
                    ],
                )
            )
            staged = root / "staged/usr/lib/asterinas"
            staged.mkdir(parents=True)
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(root / "staged"),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            base_hash = _sha256(base)
            first = root / "first.ext2"
            second = root / "second.ext2"

            first_hash = materialize_image(base, first, spec)
            time.sleep(1.1)
            second_hash = materialize_image(base, second, spec)

            self.assertEqual(_sha256(base), base_hash)
            self.assertNotIn(
                "Inode:", _debugfs(base, "stat /usr/lib/asterinas/runtime")
            )
            self.assertEqual(first_hash, _sha256(first))
            self.assertEqual(first_hash, second_hash)
            output_stat = _debugfs(first, "stat /usr/lib/asterinas/runtime")
            self.assertIn("Type: regular", output_stat)
            self.assertRegex(output_stat, r"Mode:\s+0755\b")
            self.assertRegex(output_stat, r"User:\s+0\s+Group:\s+0\b")
            dumped = root / "dumped"
            _debugfs(first, f"dump /usr/lib/asterinas/runtime {dumped}")
            self.assertEqual(dumped.read_bytes(), source.read_bytes())
            # An explicitly permitted addition also works with a newer base
            # that already installed the same regular guest file.
            replaced = root / "replaced.ext2"
            materialize_image(first, replaced, spec)
            _debugfs(replaced, f"dump /usr/lib/asterinas/runtime {dumped}")
            self.assertEqual(dumped.read_bytes(), source.read_bytes())

    def test_creation_rejects_unsafe_destinations_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            staged = root / "staged"
            (staged / "real/nested").mkdir(parents=True)
            (staged / "real/nested/runtime").write_text("old", encoding="utf-8")
            (staged / "alias").symlink_to("real", target_is_directory=True)
            (staged / "dangling").symlink_to("absent", target_is_directory=True)
            (staged / "file").write_text("file", encoding="utf-8")
            (staged / "link").symlink_to("real/nested/runtime")
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(staged),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            base_hash = _sha256(base)
            output = root / "published.ext2"
            output.write_bytes(b"published")
            for destination, message in (
                ("/missing/runtime", "parent does not exist"),
                ("/alias/nested/new", "parent is not a directory"),
                ("/dangling/new", "parent is not a directory"),
                ("/file/new", "parent is not a directory"),
                ("/real/nested", "destination is not a regular file"),
                ("/link", "destination is not a regular file"),
            ):
                with self.subTest(destination=destination):
                    spec = load_overlay_spec(
                        _write_spec(
                            root,
                            [
                                {
                                    "source": "runtime.sh",
                                    "destination": "/created-before-failure",
                                    "mode": "0755",
                                    "create": True,
                                },
                                {
                                    "source": "runtime.sh",
                                    "destination": destination,
                                    "mode": "0755",
                                    "create": True,
                                },
                            ],
                        )
                    )
                    with self.assertRaisesRegex(OverlayError, message):
                        materialize_image(base, output, spec)
                    self.assertEqual(_sha256(base), base_hash)
                    self.assertEqual(output.read_bytes(), b"published")
                    self.assertEqual(list(root.glob(".published.ext2.*.tmp")), [])

    def test_rejects_non_root_ownership_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                            "create": True,
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext2", "-F", str(base), "16M"], check=True
            )
            base_hash = _sha256(base)
            output = root / "published.ext2"
            run_debugfs = dev_overlay._run_debugfs
            for field in ("uid", "gid"):
                with self.subTest(field=field):
                    output.write_bytes(b"published")

                    def write_wrong_owner(
                        image: Path,
                        command: str,
                        *,
                        writable: bool = False,
                        allow_missing: bool = False,
                    ) -> str | None:
                        # Inject an incorrect ownership write into the real
                        # image; subsequent stat readback still uses debugfs.
                        if command == f"set_inode_field /runtime {field} 0":
                            command = f"set_inode_field /runtime {field} 1000"
                        return run_debugfs(
                            image,
                            command,
                            writable=writable,
                            allow_missing=allow_missing,
                        )

                    with patch.object(
                        dev_overlay, "_run_debugfs", side_effect=write_wrong_owner
                    ):
                        with self.assertRaisesRegex(
                            OverlayError, "ownership verification failed"
                        ):
                            materialize_image(base, output, spec)
                    self.assertEqual(_sha256(base), base_hash)
                    self.assertEqual(output.read_bytes(), b"published")
                    self.assertEqual(list(root.glob(".published.ext2.*.tmp")), [])

    def test_rejects_failed_or_ignored_timestamp_writes_without_publishing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                            "create": True,
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext2", "-F", str(base), "16M"], check=True
            )
            base_hash = _sha256(base)
            output = root / "published.ext2"
            run = subprocess.run
            for field, command, message in (
                (
                    "mtime",
                    "set_inode_field /runtime invalid_timestamp 0",
                    "debugfs failed",
                ),
                ("atime", "stat /runtime", "timestamp verification failed"),
                ("ctime", "stat /runtime", "timestamp verification failed"),
                ("mtime", "stat /runtime", "timestamp verification failed"),
                ("crtime", "stat /runtime", "timestamp verification failed"),
            ):
                with self.subTest(field=field, command=command):
                    output.write_bytes(b"published")

                    def inject_timestamp_failure(arguments, **kwargs):
                        if arguments[0] == "debugfs" and "-R" in arguments:
                            index = arguments.index("-R") + 1
                            if (
                                arguments[index]
                                == f"set_inode_field /runtime {field} 0"
                            ):
                                arguments = list(arguments)
                                arguments[index] = command
                        return run(arguments, **kwargs)

                    with patch.object(
                        dev_overlay.subprocess,
                        "run",
                        side_effect=inject_timestamp_failure,
                    ):
                        with self.assertRaisesRegex(OverlayError, message):
                            materialize_image(base, output, spec)
                    self.assertEqual(_sha256(base), base_hash)
                    self.assertEqual(output.read_bytes(), b"published")
                    self.assertEqual(list(root.glob(".published.ext2.*.tmp")), [])

    def test_rejects_stat_io_error_instead_of_creating_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                            "create": True,
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext2", "-F", str(base), "16M"], check=True
            )
            base_hash = _sha256(base)
            output = root / "published.ext2"
            output.write_bytes(b"published")
            run = subprocess.run
            injected = False

            def inject_lookup_error(arguments, **kwargs):
                nonlocal injected
                if not injected and arguments[0] == "debugfs" and "-R" in arguments:
                    if arguments[arguments.index("-R") + 1] == "stat /runtime":
                        injected = True
                        return subprocess.CompletedProcess(
                            arguments,
                            0,
                            "",
                            "debugfs 1.47.0 (5-Feb-2023)\n/runtime: Input/output error\n",
                        )
                return run(arguments, **kwargs)

            with patch.object(
                dev_overlay.subprocess, "run", side_effect=inject_lookup_error
            ):
                with self.assertRaisesRegex(OverlayError, "debugfs failed"):
                    materialize_image(base, output, spec)
            self.assertEqual(_sha256(base), base_hash)
            self.assertEqual(output.read_bytes(), b"published")

    def test_rejects_metadata_checksums_without_changing_base_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                            "create": True,
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-O",
                    "metadata_csum",
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            self.assertIn("metadata_csum", _debugfs(base, "stats"))
            base_hash = _sha256(base)
            output = root / "published.ext2"
            output.write_bytes(b"published")
            with self.assertRaisesRegex(
                OverlayError, "metadata checksums are unsupported"
            ):
                materialize_image(base, output, spec)
            self.assertEqual(_sha256(base), base_hash)
            self.assertEqual(output.read_bytes(), b"published")
            self.assertEqual(list(root.glob(".published.ext2.*.tmp")), [])

    def test_accepts_tmpdir_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                            "create": True,
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext2", "-F", str(base), "16M"], check=True
            )
            spaced = root / "temporary directory"
            spaced.mkdir()
            output = root / "output.ext2"
            with patch.object(tempfile, "tempdir", str(spaced)):
                materialize_image(base, output, spec)
            self.assertEqual(_debugfs(output, "cat /runtime"), "new")

    def test_missing_destination_requires_explicit_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            base = root / "base.ext2"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext2", "-F", str(base), "16M"], check=True
            )
            base_hash = _sha256(base)
            output = root / "published.ext2"
            output.write_bytes(b"published")
            for extra in ({}, {"create": False}):
                with self.subTest(extra=extra):
                    spec = load_overlay_spec(
                        _write_spec(
                            root,
                            [
                                {
                                    "source": "runtime.sh",
                                    "destination": "/runtime",
                                    "mode": "0755",
                                    **extra,
                                }
                            ],
                        )
                    )
                    with self.assertRaisesRegex(
                        OverlayError, "destination does not exist"
                    ):
                        materialize_image(base, output, spec)
                    self.assertEqual(_sha256(base), base_hash)
                    self.assertEqual(output.read_bytes(), b"published")

    def test_provenance_records_creation_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                            "create": True,
                        }
                    ],
                )
            )
            _, companion = build_derived_documents(
                {
                    "profile": "browser-web",
                    "root_image_sha256": "1" * 64,
                    "tool_versions": {},
                },
                base_manifest_sha256="2" * 64,
                derived_image_sha256="3" * 64,
                spec=spec,
            )
            self.assertIs(companion["files"][0]["create"], True)

    def test_rejects_unsafe_or_ambiguous_overlay_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            outside = root.parent / f"{root.name}-outside"
            outside.write_text("outside", encoding="utf-8")
            self.addCleanup(outside.unlink)
            cases = (
                (
                    {"schema_version": 1, "profile": "browser-web", "files": []},
                    "at least one file",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [],
                        "unknown": True,
                    },
                    "unexpected overlay fields",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": f"../{outside.name}",
                                "destination": "/safe",
                                "mode": "0644",
                            }
                        ],
                    },
                    "source must remain beneath",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": "runtime.sh",
                                "destination": "/usr/../etc/passwd",
                                "mode": "0644",
                            }
                        ],
                    },
                    "canonical absolute path",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": "runtime.sh",
                                "destination": "/safe",
                                "mode": "4755",
                            }
                        ],
                    },
                    "mode must be 0",
                ),
                (
                    {
                        "schema_version": 1,
                        "profile": "browser-web",
                        "files": [
                            {
                                "source": "runtime.sh",
                                "destination": "/safe;rm",
                                "mode": "0644",
                            }
                        ],
                    },
                    "safe debugfs path characters",
                ),
            )
            for payload, message in cases:
                with self.subTest(message=message):
                    spec = root / "case.json"
                    spec.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(OverlayError, message):
                        load_overlay_spec(spec, expected_profile="browser-web")

            duplicate = _write_spec(
                root,
                [
                    {
                        "source": "runtime.sh",
                        "destination": "/same",
                        "mode": "0644",
                    },
                    {
                        "source": "runtime.sh",
                        "destination": "/same",
                        "mode": "0755",
                    },
                ],
            )
            with self.assertRaisesRegex(OverlayError, "duplicate destination"):
                load_overlay_spec(duplicate, expected_profile="browser-web")

            with self.assertRaisesRegex(OverlayError, "profile does not match"):
                load_overlay_spec(duplicate, expected_profile="minimal-m1")

    def test_materializes_deterministic_image_without_changing_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("#!/bin/sh\necho new\n", encoding="utf-8")
            source.chmod(0o755)
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/usr/lib/asterinas/runtime",
                            "mode": "0755",
                        }
                    ],
                )
            )
            staged = root / "staged/usr/lib/asterinas"
            staged.mkdir(parents=True)
            (staged / "runtime").write_text("old\n", encoding="utf-8")
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(root / "staged"),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            base_hash = _sha256(base)
            first = root / "first.ext2"
            second = root / "second.ext2"

            first_hash = materialize_image(base, first, spec)
            self.assertIn(
                "Type: regular",
                _debugfs(first, "stat /usr/lib/asterinas/runtime"),
            )
            time.sleep(1.1)
            second_hash = materialize_image(base, second, spec)

            self.assertEqual(_sha256(base), base_hash)
            self.assertEqual(first_hash, _sha256(first))
            self.assertEqual(first_hash, second_hash)
            with tempfile.TemporaryDirectory() as dump_directory:
                base_dump = Path(dump_directory) / "base"
                output_dump = Path(dump_directory) / "output"
                _debugfs(base, f"dump /usr/lib/asterinas/runtime {base_dump}")
                _debugfs(first, f"dump /usr/lib/asterinas/runtime {output_dump}")
                self.assertEqual(base_dump.read_bytes(), b"old\n")
                self.assertEqual(output_dump.read_bytes(), source.read_bytes())
            self.assertRegex(
                _debugfs(first, "stat /usr/lib/asterinas/runtime"),
                r"Mode:\s+0755\b",
            )

    def test_failure_preserves_published_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/missing",
                            "mode": "0644",
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext2", "-F", str(base), "16M"], check=True
            )
            output = root / "published.ext2"
            output.write_bytes(b"published")

            with self.assertRaisesRegex(OverlayError, "destination does not exist"):
                materialize_image(base, output, spec)

            self.assertEqual(output.read_bytes(), b"published")

    def test_rejects_symlinked_base_image_before_debugfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0644",
                        }
                    ],
                )
            )
            base = root / "base.ext2"
            base.write_bytes(b"not-an-ext-image")
            symlink = root / "base-link.ext2"
            symlink.symlink_to(base)

            with self.assertRaisesRegex(OverlayError, "non-symlink regular file"):
                materialize_image(symlink, root / "output.ext2", spec)

    def test_rejects_base_image_as_output_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0644",
                        }
                    ],
                )
            )
            staged = root / "staged"
            staged.mkdir()
            (staged / "runtime").write_text("old", encoding="utf-8")
            base = root / "base.ext2"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-t",
                    "ext2",
                    "-d",
                    str(staged),
                    "-F",
                    str(base),
                    "16M",
                ],
                check=True,
            )
            base_hash = _sha256(base)

            with self.assertRaisesRegex(OverlayError, "must differ from the base"):
                materialize_image(base, base, spec)

            self.assertEqual(_sha256(base), base_hash)

    def test_derived_documents_preserve_package_identity_and_record_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "runtime.sh"
            source.write_text("new", encoding="utf-8")
            spec = load_overlay_spec(
                _write_spec(
                    root,
                    [
                        {
                            "source": "runtime.sh",
                            "destination": "/runtime",
                            "mode": "0755",
                        }
                    ],
                )
            )
            base_document = {
                "schema_version": 7,
                "profile": "browser-web",
                "packages_lock_sha256": "1" * 64,
                "downloaded_packages": [{"name": "firefox-esr"}],
                "tool_versions": {
                    "browser-web-runtime": "2" * 64,
                    "debootstrap": "test",
                },
                "root_image_sha256": "3" * 64,
            }

            derived, companion = build_derived_documents(
                base_document,
                base_manifest_sha256="4" * 64,
                derived_image_sha256="5" * 64,
                spec=spec,
            )

            self.assertEqual(derived["root_image_sha256"], "5" * 64)
            self.assertEqual(
                derived["downloaded_packages"], base_document["downloaded_packages"]
            )
            self.assertRegex(
                derived["tool_versions"]["asterinas-dev-overlay"], r"\A[0-9a-f]{64}\Z"
            )
            self.assertEqual(companion["base_root_image_sha256"], "3" * 64)
            self.assertEqual(companion["base_manifest_sha256"], "4" * 64)
            self.assertEqual(companion["derived_root_image_sha256"], "5" * 64)
            self.assertEqual(companion["files"][0]["sha256"], _sha256(source))
            self.assertIs(companion["files"][0]["create"], False)

    def test_browser_web_spec_maps_only_guest_runtime_files(self) -> None:
        spec = load_overlay_spec(BROWSER_WEB_SPEC, expected_profile="browser-web")
        destinations = {entry.destination for entry in spec.files}
        self.assertEqual(
            destinations,
            {
                "/usr/lib/asterinas/desktop-m5-network-evidence",
                "/usr/lib/asterinas/megrez-safe-reboot",
                "/usr/lib/asterinas/browser-web-marionette-gate",
                "/usr/lib/asterinas/browser_m5_marionette_gate.py",
                "/usr/lib/asterinas/physical-graphics-gate",
                "/usr/share/asterinas/physical-graphics/index.html",
                "/usr/lib/asterinas/firefox-diagnostic-snapshot",
                "/usr/lib/asterinas/browser-web-firefox",
                "/usr/lib/asterinas/browser-web-evidence",
                "/usr/lib/asterinas/browser-web-timeline",
                "/usr/share/asterinas/browser-web-trust-check.py",
                "/usr/share/asterinas/browser-web-online-rootfs-check.py",
                "/usr/lib/asterinas/desktop-m5-session",
                "/usr/lib/asterinas/desktop-m5-device-access",
                "/usr/lib/asterinas/desktop-m5-evidence",
                "/etc/systemd/system/asterinas-browser-web.service",
                "/etc/systemd/system/asterinas-browser-web-evidence.service",
                "/etc/systemd/system/asterinas-browser-web-timeline-begin.service",
                "/etc/systemd/system/asterinas-browser-web-timeline-basic.service",
            },
        )
        self.assertNotIn(
            "desktop_m5_network_gate.py", {entry.source.name for entry in spec.files}
        )
        self.assertEqual(
            {entry.destination for entry in spec.files if entry.create},
            {"/usr/lib/asterinas/firefox-diagnostic-snapshot"},
        )

    def test_make_default_publishes_beneath_user_writable_target(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(
            "DEBIAN_BROWSER_WEB_DEV_ROOTFS ?= "
            "$(CURDIR)/target/dev-overlays/browser-web/rootfs",
            makefile,
        )

    def test_rejects_overlapping_base_and_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime.sh").write_text("new", encoding="utf-8")
            spec = _write_spec(
                root,
                [
                    {
                        "source": "runtime.sh",
                        "destination": "/runtime",
                        "mode": "0644",
                    }
                ],
            )

            with self.assertRaisesRegex(OverlayError, "must not overlap"):
                materialize_rootfs(root, spec, root / "derived")


if __name__ == "__main__":
    unittest.main()
