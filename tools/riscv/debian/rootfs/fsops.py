#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Descriptor-pinned filesystem mutations for the Debian rootfs builder."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import secrets
import signal
import stat
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path


_CHUNK_SIZE_BYTES = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_PUBLISHED_PATHS = (
    "debian-root.ext2",
    "rootfs-manifest.json",
    "packages.lock",
    "source-metadata/InRelease",
    "source-metadata/package-checksums",
)


class FsOpsError(ValueError):
    """Raised when a pinned filesystem operation cannot be completed safely."""


class PublishInterrupted(InterruptedError):
    """Raised after the first termination signal during publication."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"publication interrupted by signal {signum}")
        self.signum = signum


@dataclass
class _PublishFile:
    source: int
    directory: int
    name: str
    temporary: str | None = None
    backup: str | None = None
    existed: bool = False


@contextlib.contextmanager
def _open_directory(
    path: Path,
    *,
    create: bool,
    create_mode: int = 0o700,
) -> Iterator[int]:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            created = False
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, create_mode, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            if created:
                os.fchmod(child, create_mode)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _open_child_directory(parent: int, name: str, *, create_mode: int = 0o700) -> int:
    if not name or name in {".", ".."} or "/" in name:
        raise FsOpsError(f"unsafe cache directory component: {name!r}")
    created = False
    try:
        os.mkdir(name, create_mode, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    if created:
        os.fchmod(descriptor, create_mode)
    return descriptor


def _open_regular_file(path: Path) -> int:
    with _open_directory(path.parent, create=False) as parent:
        descriptor = os.open(path.name, _READ_FLAGS, dir_fd=parent)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise FsOpsError(f"source is not a regular file: {path}")
    return descriptor


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, _CHUNK_SIZE_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("short write while admitting cache entry")
        view = view[written:]


def _verify_cached_file(directory: int, name: str, expected_sha256: str) -> None:
    descriptor = os.open(name, _READ_FLAGS, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FsOpsError(f"cache entry is not a regular file: {name}")
        actual_sha256 = _hash_descriptor(descriptor)
    finally:
        os.close(descriptor)
    if actual_sha256 != expected_sha256:
        raise FsOpsError(f"content-addressed cache hash mismatch: {name}")


def admit_cache_entry(
    cache_directory: Path, source: Path, expected_sha256: str
) -> None:
    """Admits one verified file without following cache-path symlinks."""

    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise FsOpsError("expected SHA-256 must be 64 lowercase hexadecimal characters")

    source_descriptor = _open_regular_file(source)
    try:
        with _open_directory(cache_directory, create=True) as cache:
            sha256_directory = _open_child_directory(cache, "sha256")
            try:
                prefix_directory = _open_child_directory(
                    sha256_directory,
                    expected_sha256[:2],
                )
            finally:
                os.close(sha256_directory)
            try:
                destination_name = f"{expected_sha256}.deb"
                try:
                    _verify_cached_file(
                        prefix_directory,
                        destination_name,
                        expected_sha256,
                    )
                    return
                except FileNotFoundError:
                    pass

                temporary_name = f".{destination_name}.{secrets.token_hex(8)}"
                temporary = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o400,
                    dir_fd=prefix_directory,
                )
                try:
                    try:
                        digest = hashlib.sha256()
                        while chunk := os.read(source_descriptor, _CHUNK_SIZE_BYTES):
                            digest.update(chunk)
                            _write_all(temporary, chunk)
                        if digest.hexdigest() != expected_sha256:
                            raise FsOpsError("package changed while entering cache")
                        os.fchmod(temporary, 0o444)
                        os.fsync(temporary)
                    finally:
                        os.close(temporary)

                    try:
                        os.link(
                            temporary_name,
                            destination_name,
                            src_dir_fd=prefix_directory,
                            dst_dir_fd=prefix_directory,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        _verify_cached_file(
                            prefix_directory,
                            destination_name,
                            expected_sha256,
                        )
                    os.fsync(prefix_directory)
                finally:
                    try:
                        os.unlink(temporary_name, dir_fd=prefix_directory)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(prefix_directory)
    finally:
        os.close(source_descriptor)


def _open_publication_sources(source_root: Path) -> list[int]:
    sources: list[int] = []
    try:
        for relative_path in _PUBLISHED_PATHS:
            sources.append(_open_regular_file(source_root / relative_path))
    except BaseException:
        for descriptor in sources:
            os.close(descriptor)
        raise
    return sources


def _require_directory_still_pinned(path: Path, descriptor: int) -> None:
    current = os.stat(path, follow_symlinks=False)
    pinned = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != pinned.st_dev
        or current.st_ino != pinned.st_ino
    ):
        raise FsOpsError(f"output directory changed during publication: {path}")


def _unlink_if_present(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _prepare_publication_file(entry: _PublishFile) -> None:
    entry.temporary = f".{entry.name}.{secrets.token_hex(8)}"
    output = os.open(
        entry.temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=entry.directory,
    )
    try:
        while chunk := os.read(entry.source, _CHUNK_SIZE_BYTES):
            _write_all(output, chunk)
        os.fchmod(output, 0o644)
        os.fsync(output)
    finally:
        os.close(output)

    try:
        destination = os.stat(
            entry.name,
            dir_fd=entry.directory,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if not stat.S_ISREG(destination.st_mode):
        raise FsOpsError(f"published destination is not a regular file: {entry.name}")
    entry.existed = True
    entry.backup = f".{entry.name}.rollback.{secrets.token_hex(8)}"
    os.link(
        entry.name,
        entry.backup,
        src_dir_fd=entry.directory,
        dst_dir_fd=entry.directory,
        follow_symlinks=False,
    )


def _fsync_directories(entries: Sequence[_PublishFile]) -> None:
    for descriptor in dict.fromkeys(entry.directory for entry in entries):
        os.fsync(descriptor)


def _rollback_publication(entries: Sequence[_PublishFile]) -> None:
    rollback_error: OSError | None = None
    for entry in reversed(entries):
        try:
            if entry.existed and entry.backup is not None:
                os.replace(
                    entry.backup,
                    entry.name,
                    src_dir_fd=entry.directory,
                    dst_dir_fd=entry.directory,
                )
                entry.backup = None
            elif not entry.existed:
                _unlink_if_present(entry.directory, entry.name)
        except OSError as error:
            rollback_error = rollback_error or error
    try:
        _fsync_directories(entries)
    except OSError as error:
        rollback_error = rollback_error or error
    if rollback_error is not None:
        raise FsOpsError(f"failed to roll back published artifacts: {rollback_error}")


def _cleanup_publication_files(entries: Sequence[_PublishFile]) -> None:
    directories: set[int] = set()
    for entry in entries:
        directories.add(entry.directory)
        if entry.temporary is not None:
            _unlink_if_present(entry.directory, entry.temporary)
            entry.temporary = None
        if entry.backup is not None:
            _unlink_if_present(entry.directory, entry.backup)
            entry.backup = None
    for descriptor in directories:
        try:
            os.fsync(descriptor)
        except OSError:
            pass


def publish_set(output_directory: Path, source_root: Path) -> None:
    """Publishes the exact rootfs artifact set with rollback on process failure."""

    sources = _open_publication_sources(source_root)
    entries: list[_PublishFile] = []
    previous_handlers: dict[int, signal.Handlers] = {}
    rolling_back = False
    committed = False

    def handle_signal(signum: int, _frame: object) -> None:
        if rolling_back:
            os._exit(128 + signum)
        if committed:
            return
        raise PublishInterrupted(signum)

    try:
        with _open_directory(
            output_directory,
            create=True,
            create_mode=0o755,
        ) as output:
            metadata = _open_child_directory(
                output,
                "source-metadata",
                create_mode=0o755,
            )
            try:
                for source, relative_path in zip(
                    sources,
                    _PUBLISHED_PATHS,
                    strict=True,
                ):
                    path = Path(relative_path)
                    directory = metadata if len(path.parts) == 2 else output
                    entries.append(_PublishFile(source, directory, path.name))

                for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signum] = signal.signal(signum, handle_signal)
                try:
                    _require_directory_still_pinned(output_directory, output)
                    for entry in entries:
                        _prepare_publication_file(entry)
                    _fsync_directories(entries)

                    try:
                        for entry in entries:
                            _require_directory_still_pinned(output_directory, output)
                            assert entry.temporary is not None
                            os.replace(
                                entry.temporary,
                                entry.name,
                                src_dir_fd=entry.directory,
                                dst_dir_fd=entry.directory,
                            )
                            entry.temporary = None
                        _fsync_directories(entries)
                        _require_directory_still_pinned(output_directory, output)
                        committed = True
                    except BaseException as error:
                        rolling_back = True
                        try:
                            _rollback_publication(entries)
                        except FsOpsError as rollback_error:
                            raise rollback_error from error
                        raise
                finally:
                    _cleanup_publication_files(entries)
                    for signum, handler in previous_handlers.items():
                        signal.signal(signum, handler)
            finally:
                os.close(metadata)
    finally:
        for source in sources:
            os.close(source)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fsops")
    subparsers = parser.add_subparsers(dest="command", required=True)
    cache = subparsers.add_parser("cache-admit")
    cache.add_argument("--cache-dir", required=True, type=Path)
    cache.add_argument("--source", required=True, type=Path)
    cache.add_argument("--sha256", required=True)
    publication = subparsers.add_parser("publish-set")
    publication.add_argument("--output-dir", required=True, type=Path)
    publication.add_argument("--source-root", required=True, type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    namespace = parser.parse_args(sys.argv[1:] if arguments is None else arguments)
    try:
        if namespace.command == "cache-admit":
            admit_cache_entry(namespace.cache_dir, namespace.source, namespace.sha256)
        else:
            publish_set(namespace.output_dir, namespace.source_root)
    except PublishInterrupted as error:
        return 128 + error.signum
    except (FsOpsError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
