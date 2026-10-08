"""Descriptor-pinned file I/O for reproducible QEMU boot evidence."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


FileIdentity = tuple[int, int]


def file_identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _flag(name: str) -> int:
    try:
        return int(getattr(os, name))
    except AttributeError as error:
        raise RuntimeError(f"secure QEMU evidence I/O requires os.{name}") from error


def _directory_flags() -> int:
    return os.O_RDONLY | _flag("O_DIRECTORY") | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")


def _regular_input_flags() -> int:
    return os.O_RDONLY | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW") | _flag("O_NONBLOCK")


def _exclusive_file_flags(*, read_write: bool) -> int:
    access = os.O_RDWR if read_write else os.O_WRONLY
    return access | os.O_CREAT | os.O_EXCL | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")


def _directory_walk_origin(path: Path) -> tuple[Path, int, tuple[str, ...]]:
    """Open a trusted path origin and return its unresolved components."""

    if path.is_absolute():
        origin = Path(path.anchor)
        components = path.parts[1:]
        display_path = path
    else:
        origin = Path(".")
        components = path.parts
        display_path = Path(os.path.abspath(path))

    root_fd = os.open(origin, _directory_flags())
    return (
        display_path,
        root_fd,
        tuple(component for component in components if component not in ("", ".")),
    )


def _open_directory_components(
    root_fd: int,
    components: tuple[str, ...],
    *,
    create: bool,
) -> int:
    """Open a directory one no-follow component at a time from `root_fd`."""

    current_fd = os.dup(root_fd)
    try:
        for component in components:
            try:
                next_fd = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o777, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=current_fd,
                )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _close_fd(fd: int) -> None:
    if fd >= 0:
        os.close(fd)


def _leaf_name(path: Path) -> str:
    if path.name in ("", ".", "..") or Path(path.name).name != path.name:
        raise ValueError(f"path must end in one ordinary file name: {path}")
    return path.name


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing QEMU evidence")
        view = view[written:]


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


@dataclass
class PinnedPublication:
    """An inode kept open across publication and final identity checks."""

    identity: FileIdentity
    _fd: int

    def duplicate(self) -> PinnedPublication:
        """Retain an independent handle for failure-time revocation."""

        if self._fd < 0:
            raise RuntimeError("publication is already closed")
        return PinnedPublication(self.identity, os.dup(self._fd))

    def overwrite(self, payload: bytes) -> None:
        """Replace this held inode without resolving its published path."""

        if self._fd < 0:
            raise RuntimeError("publication is already closed")
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        _write_all(self._fd, payload)
        os.fsync(self._fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> PinnedPublication:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class PreparedPublication:
    """A durable temporary inode awaiting atomic publication."""

    identity: FileIdentity
    _directory_fd: int
    _temporary_name: str | None
    _destination_name: str
    _fd: int
    _is_retained: bool = False

    def retain(self) -> PinnedPublication:
        """Return an independent inode handle that survives publication."""

        if self._fd < 0:
            raise RuntimeError("prepared publication is already closed")
        retained_fd = os.dup(self._fd)
        try:
            publication = PinnedPublication(self.identity, retained_fd)
        except BaseException:
            os.close(retained_fd)
            raise
        self._is_retained = True
        return publication

    def publish(self) -> None:
        """Atomically replace the destination without syncing its parent."""

        if not self._is_retained:
            raise RuntimeError("prepared publication has no retained inode handle")
        if self._temporary_name is None:
            raise RuntimeError("prepared publication is already published")
        os.replace(
            self._temporary_name,
            self._destination_name,
            src_dir_fd=self._directory_fd,
            dst_dir_fd=self._directory_fd,
        )
        self._temporary_name = None

    def close(self) -> None:
        fd = self._fd
        self._fd = -1
        try:
            if fd >= 0:
                os.close(fd)
        finally:
            temporary_name = self._temporary_name
            self._temporary_name = None
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=self._directory_fd)
                except FileNotFoundError:
                    pass


@dataclass
class PinnedRegularInput:
    """An opened regular input whose bytes no longer depend on its path."""

    path: Path
    label: str
    _parent_fd: int
    _fd: int
    identity: FileIdentity
    _initial_sha256: str

    @classmethod
    def open(cls, path: Path, *, label: str) -> PinnedRegularInput:
        leaf = _leaf_name(path)
        root_fd = -1
        parent_fd = -1
        try:
            parent, root_fd, components = _directory_walk_origin(path.parent)
            parent_fd = _open_directory_components(
                root_fd,
                components,
                create=False,
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as error:
            _close_fd(parent_fd)
            _close_fd(root_fd)
            raise ValueError(f"{label} must be an existing regular file") from error

        try:
            parent_metadata = os.fstat(parent_fd)
            fd = os.open(leaf, _regular_input_flags(), dir_fd=parent_fd)
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"{label} must be an existing regular file")
                current_entry = os.stat(
                    leaf,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if file_identity(metadata) != file_identity(current_entry):
                    raise RuntimeError(f"{label} path changed while opening")

                initial_sha256 = _sha256_fd(fd)
                hashed_metadata = os.fstat(fd)
                if any(
                    getattr(metadata, field) != getattr(hashed_metadata, field)
                    for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
                ):
                    raise RuntimeError(f"{label} changed while it was opened")

                try:
                    current_parent_fd = _open_directory_components(
                        root_fd,
                        components,
                        create=False,
                    )
                except OSError as error:
                    raise RuntimeError(
                        f"{label} parent directory changed while opening"
                    ) from error
                try:
                    current_parent = os.fstat(current_parent_fd)
                finally:
                    os.close(current_parent_fd)
                if file_identity(parent_metadata) != file_identity(current_parent):
                    raise RuntimeError(
                        f"{label} parent directory changed while opening"
                    )
            except BaseException:
                os.close(fd)
                raise
        except BaseException as error:
            os.close(parent_fd)
            os.close(root_fd)
            if isinstance(error, (ValueError, RuntimeError)):
                raise
            raise ValueError(f"{label} must be an existing regular file") from error

        os.close(root_fd)

        return cls(
            path=parent / leaf,
            label=label,
            _parent_fd=parent_fd,
            _fd=fd,
            identity=file_identity(metadata),
            _initial_sha256=initial_sha256,
        )

    def copy_to(self, destination: Path) -> None:
        """Copy the held inode into a new private staging file."""

        flags = _exclusive_file_flags(read_write=False)
        destination_fd = os.open(destination, flags, 0o600)
        try:
            digest = hashlib.sha256()
            offset = 0
            while True:
                chunk = os.pread(self._fd, 1024 * 1024, offset)
                if not chunk:
                    break
                _write_all(destination_fd, chunk)
                digest.update(chunk)
                offset += len(chunk)
            os.fsync(destination_fd)
            if digest.hexdigest() != self._initial_sha256:
                raise RuntimeError(f"{self.label} changed while it was staged")
        except BaseException:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(destination_fd)

    def verify_unchanged(self) -> None:
        """Verify that the held input inode still has its pinned bytes."""

        if self._fd < 0:
            raise RuntimeError(f"{self.label} is already closed")
        if _sha256_fd(self._fd) != self._initial_sha256:
            raise RuntimeError(f"{self.label} changed during the run")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._parent_fd >= 0:
            os.close(self._parent_fd)
            self._parent_fd = -1

    def __enter__(self) -> PinnedRegularInput:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class PinnedOutputDirectory:
    """A held output directory used for no-follow, relative publication."""

    path: Path
    _root_fd: int
    _components: tuple[str, ...]
    _fd: int
    identity: FileIdentity

    @classmethod
    def open(cls, path: Path) -> PinnedOutputDirectory:
        display_path, root_fd, components = _directory_walk_origin(path)
        fd = -1
        try:
            fd = _open_directory_components(root_fd, components, create=True)
            metadata = os.fstat(fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"output parent must be a directory: {path}")
            try:
                current_fd = _open_directory_components(
                    root_fd,
                    components,
                    create=False,
                )
            except OSError as error:
                raise RuntimeError(
                    "output directory path changed while opening"
                ) from error
            try:
                current = os.fstat(current_fd)
            finally:
                os.close(current_fd)
            if file_identity(metadata) != file_identity(current):
                raise RuntimeError("output directory path changed while opening")
        except BaseException:
            _close_fd(fd)
            os.close(root_fd)
            raise
        return cls(
            path=display_path,
            _root_fd=root_fd,
            _components=components,
            _fd=fd,
            identity=file_identity(metadata),
        )

    def entry_metadata(self, name: str) -> os.stat_result | None:
        _leaf_name(Path(name))
        try:
            return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def remove_entry(self, name: str) -> None:
        _leaf_name(Path(name))
        try:
            os.unlink(name, dir_fd=self._fd)
        except FileNotFoundError:
            pass

    def create_exclusive(self, name: str) -> BinaryIO:
        _leaf_name(Path(name))
        fd = os.open(
            name,
            _exclusive_file_flags(read_write=True),
            0o644,
            dir_fd=self._fd,
        )
        return os.fdopen(fd, "w+b")

    def atomic_write(self, name: str, payload: bytes) -> PinnedPublication:
        """Publish bytes without ever opening the destination entry."""

        publication: PinnedPublication | None = None
        try:
            with self.prepare_atomic_write(name, payload) as prepared:
                publication = prepared.retain()
                prepared.publish()
                self.sync()
            return publication
        except BaseException:
            if publication is not None:
                publication.close()
            raise

    @contextmanager
    def prepare_atomic_write(
        self,
        name: str,
        payload: bytes,
    ) -> Iterator[PreparedPublication]:
        """Prepare durable bytes and clean them unless explicitly published."""

        _leaf_name(Path(name))
        temporary_name: str | None = None
        temporary_fd: int | None = None
        prepared: PreparedPublication | None = None
        try:
            for _attempt in range(32):
                candidate = f".{name}.tmp-{secrets.token_hex(12)}"
                try:
                    temporary_fd = os.open(
                        candidate,
                        _exclusive_file_flags(read_write=False),
                        0o600,
                        dir_fd=self._fd,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_fd is None or temporary_name is None:
                raise FileExistsError("could not reserve an evidence temporary file")
            _write_all(temporary_fd, payload)
            os.fchmod(temporary_fd, 0o644)
            os.fsync(temporary_fd)
            published_identity = file_identity(os.fstat(temporary_fd))
            prepared = PreparedPublication(
                identity=published_identity,
                _directory_fd=self._fd,
                _temporary_name=temporary_name,
                _destination_name=name,
                _fd=temporary_fd,
            )
            yield prepared
        finally:
            if prepared is not None:
                prepared.close()
            else:
                try:
                    if temporary_fd is not None:
                        os.close(temporary_fd)
                finally:
                    if temporary_name is not None:
                        try:
                            os.unlink(temporary_name, dir_fd=self._fd)
                        except FileNotFoundError:
                            pass

    def sync(self) -> None:
        """Persist publications made through this held directory."""

        os.fsync(self._fd)

    def verify_current(self) -> None:
        try:
            current_fd = _open_directory_components(
                self._root_fd,
                self._components,
                create=False,
            )
        except OSError as error:
            raise RuntimeError(
                "output directory path changed during the run"
            ) from error
        try:
            current_identity = file_identity(os.fstat(current_fd))
        finally:
            os.close(current_fd)
        if current_identity != self.identity:
            raise RuntimeError("output directory path changed during the run")

    def verify_open_file(self, name: str, stream: BinaryIO) -> None:
        self.verify_entry(name, file_identity(os.fstat(stream.fileno())))

    def verify_entry(self, name: str, identity: FileIdentity) -> None:
        current = self.entry_metadata(name)
        if current is None or file_identity(current) != identity:
            raise RuntimeError(f"output entry changed during the run: {name}")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> PinnedOutputDirectory:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
