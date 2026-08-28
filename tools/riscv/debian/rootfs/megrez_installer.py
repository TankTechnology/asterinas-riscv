# SPDX-License-Identifier: MPL-2.0
"""Build a restart-safe Asterinas Debian installer initramfs for Megrez."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import os
import re
import stat
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from .contract import ContractError, load_manifest, sha256_file, validate_frozen_root

CHUNK_SIZE = 32 * 1024 * 1024
ROOT_IMAGE_SIZE = 1024 * 1024 * 1024
PARTITION_SIZE = 4 * 1024 * 1024 * 1024
BLOCK_SIZE = 4096
_NEWC_HEADER_SIZE = 110
_NEWC_MAGIC = b"070701"
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
_NETWORK_INSTALLER_COMMANDS = (*_INSTALLER_COMMANDS, "tee", "wget")
_INSTALLER_PATH = ("usr/bin", "bin", "usr/sbin", "sbin")


class InstallerError(RuntimeError):
    """The installer archive does not satisfy its frozen contract."""


@dataclass(frozen=True)
class NewcEntry:
    name: str
    mode: int
    ino: int
    nlink: int
    devmajor: int
    devminor: int
    rdevmajor: int
    rdevminor: int
    data: bytes


@dataclass(frozen=True)
class Chunk:
    index: int
    offset: int
    uncompressed_size: int
    uncompressed_sha256: str
    compressed_sha256: str
    compressed: bytes

    @property
    def archive_path(self) -> str:
        return f"installer/chunks/{self.index:04d}.gz"


def _align4(offset: int) -> int:
    return (offset + 3) & ~3


def _safe_name(name: str, seen: set[str]) -> None:
    if not name or name.startswith("/") or "\0" in name:
        raise InstallerError(f"unsafe newc path: {name!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("", "..") for part in parts):
        raise InstallerError(f"unsafe newc path: {name!r}")
    if name in seen:
        raise InstallerError(f"duplicate newc path: {name!r}")


def parse_newc(archive: bytes) -> tuple[NewcEntry, ...]:
    """Parse one raw newc archive without extracting filesystem paths."""
    offset = 0
    entries: list[NewcEntry] = []
    seen: set[str] = set()
    found_trailer = False
    while offset + _NEWC_HEADER_SIZE <= len(archive):
        header = archive[offset : offset + _NEWC_HEADER_SIZE]
        if header[:6] != _NEWC_MAGIC:
            break
        try:
            fields = tuple(
                int(header[index : index + 8], 16)
                for index in range(6, _NEWC_HEADER_SIZE, 8)
            )
        except ValueError as error:
            raise InstallerError("invalid hexadecimal newc header") from error
        (
            ino,
            mode,
            _uid,
            _gid,
            nlink,
            _mtime,
            filesize,
            devmajor,
            devminor,
            rdevmajor,
            rdevminor,
            namesize,
            _check,
        ) = fields
        if namesize < 2:
            raise InstallerError("invalid newc name size")
        name_start = offset + _NEWC_HEADER_SIZE
        name_end = name_start + namesize
        if name_end > len(archive) or archive[name_end - 1] != 0:
            raise InstallerError("truncated newc name")
        try:
            name = archive[name_start : name_end - 1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise InstallerError("newc paths must be UTF-8") from error
        data_start = _align4(name_end)
        data_end = data_start + filesize
        if data_end > len(archive):
            raise InstallerError("truncated newc data")
        offset = _align4(data_end)
        if name == "TRAILER!!!":
            found_trailer = True
            break
        _safe_name(name, seen)
        seen.add(name)
        entries.append(
            NewcEntry(
                name,
                mode,
                ino,
                nlink,
                devmajor,
                devminor,
                rdevmajor,
                rdevminor,
                archive[data_start:data_end],
            )
        )
    if not found_trailer:
        raise InstallerError("newc trailer is missing")
    if any(archive[offset:]):
        raise InstallerError("nonzero data follows newc trailer")
    return tuple(entries)


def _encode_entry(entry: NewcEntry, *, mtime: int = 0) -> bytes:
    name = entry.name.encode() + b"\0"
    fields = (
        entry.ino,
        entry.mode,
        0,
        0,
        entry.nlink,
        mtime,
        len(entry.data),
        entry.devmajor,
        entry.devminor,
        entry.rdevmajor,
        entry.rdevminor,
        len(name),
        0,
    )
    header = _NEWC_MAGIC + b"".join(f"{field:08x}".encode() for field in fields)
    result = header + name
    result += b"\0" * (-len(result) % 4)
    result += entry.data
    result += b"\0" * (-len(result) % 4)
    return result


def _encode_archive(entries: Sequence[NewcEntry]) -> bytes:
    body = b"".join(_encode_entry(entry) for entry in entries)
    trailer = NewcEntry("TRAILER!!!", 0, len(entries) + 1, 1, 0, 0, 0, 0, b"")
    body += _encode_entry(trailer)
    return body + b"\0" * (-len(body) % 512)


def _resolve_entry(entries: dict[str, NewcEntry], path: str) -> NewcEntry | None:
    pending = list(PurePosixPath(path.lstrip("/")).parts)
    resolved: list[str] = []
    symlink_hops = 0
    while pending:
        component = pending.pop(0)
        if component in ("", "."):
            continue
        if component == "..":
            if not resolved:
                return None
            resolved.pop()
            continue

        candidate = "/".join((*resolved, component))
        entry = entries.get(candidate)
        if entry is None:
            return None
        if stat.S_ISLNK(entry.mode):
            symlink_hops += 1
            if symlink_hops > len(entries):
                return None
            try:
                target = entry.data.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not target or "\0" in target:
                return None
            if target.startswith("/"):
                resolved.clear()
            pending = list(PurePosixPath(target.lstrip("/")).parts) + pending
            continue
        if pending and not stat.S_ISDIR(entry.mode):
            return None
        resolved.append(component)
    return entry


def _validate_installer_runtime(
    entries: Sequence[NewcEntry], commands: Sequence[str] = _INSTALLER_COMMANDS
) -> None:
    by_name = {entry.name: entry for entry in entries}

    shell = _resolve_entry(by_name, "/bin/sh")
    if shell is None or not stat.S_ISREG(shell.mode) or not shell.mode & 0o111:
        raise InstallerError("missing executable installer runtime: /bin/sh")

    for command in commands:
        candidates = (
            _resolve_entry(by_name, f"/{directory}/{command}")
            for directory in _INSTALLER_PATH
        )
        if not any(
            entry is not None and stat.S_ISREG(entry.mode) and bool(entry.mode & 0o111)
            for entry in candidates
        ):
            raise InstallerError(
                f"missing executable installer runtime command: {command}"
            )


def plan_chunks(image: Path, *, chunk_size: int = CHUNK_SIZE) -> tuple[Chunk, ...]:
    if chunk_size <= 0 or chunk_size % BLOCK_SIZE:
        raise InstallerError("chunk size must be a positive multiple of 4096")
    chunks: list[Chunk] = []
    with image.open("rb") as source:
        index = 0
        offset = 0
        while data := source.read(chunk_size):
            if len(data) % BLOCK_SIZE:
                raise InstallerError("root image size must be a multiple of 4096")
            compressed = gzip.compress(data, compresslevel=1, mtime=0)
            chunks.append(
                Chunk(
                    index,
                    offset,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(compressed).hexdigest(),
                    compressed,
                )
            )
            index += 1
            offset += len(data)
    if not chunks:
        raise InstallerError("root image must not be empty")
    return tuple(chunks)


def _manifest(chunks: Sequence[Chunk]) -> bytes:
    lines = []
    for chunk in chunks:
        lines.append(
            "\t".join(
                (
                    f"{chunk.index:04d}",
                    str(chunk.offset // BLOCK_SIZE),
                    str(chunk.uncompressed_size // BLOCK_SIZE),
                    chunk.compressed_sha256,
                    chunk.uncompressed_sha256,
                    chunk.archive_path,
                )
            )
        )
    return ("\n".join(lines) + "\n").encode()


def render_init(
    root_sha256: str,
    root_size: int,
    chunks: Sequence[Chunk],
) -> bytes:
    if len(root_sha256) != 64 or any(c not in "0123456789abcdef" for c in root_sha256):
        raise InstallerError("root SHA-256 must be lowercase hexadecimal")
    if root_size <= 0 or root_size % BLOCK_SIZE:
        raise InstallerError("root size must be a positive multiple of 4096")
    if sum(chunk.uncompressed_size for chunk in chunks) != root_size:
        raise InstallerError("chunk sizes do not cover the root image")
    return f"""#!/bin/sh
set -o pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
hold() {{ while :; do sleep 3600; done; }}
fail() {{ echo "DEBIAN_INSTALL_FAIL reason=$1"; sync; hold; }}
mkdir -p /proc /sys /dev
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
cmdline=" $(cat /proc/cmdline) "
case "$cmdline" in *" asterinas.mmc_write_partition2 "*) ;; *) fail write-gate-not-armed ;; esac
case "$cmdline" in *" asterinas.debian_install_sha256={root_sha256} "*) ;; *) fail image-hash-not-armed ;; esac
target=/dev/mmcblk0p2
[ -b "$target" ] || fail target-not-block-device
[ "$(blockdev --getsize64 "$target")" = "{PARTITION_SIZE}" ] || fail target-size-mismatch
tab=$(printf '\t')
while IFS="$tab" read -r index block blocks compressed uncompressed path; do
    set -- $(sha256sum "/$path")
    [ "$1" = "$compressed" ] || fail compressed-hash-$index
    gzip -t "/$path" || fail compressed-stream-$index
done < /installer/chunks.tsv
while IFS="$tab" read -r index block blocks compressed uncompressed path; do
    set -- $(dd if="$target" bs={BLOCK_SIZE} skip="$block" count="$blocks" 2>/dev/null | sha256sum)
    if [ "$1" = "$uncompressed" ]; then
        echo "DEBIAN_INSTALL_CHUNK_SKIP index=$index sha256=$1"
        continue
    fi
    gzip -dc "/$path" | dd of="$target" bs={BLOCK_SIZE} seek="$block" count="$blocks" conv=notrunc || fail write-$index
    sync || fail sync-$index
    set -- $(dd if="$target" bs={BLOCK_SIZE} skip="$block" count="$blocks" 2>/dev/null | sha256sum)
    [ "$1" = "$uncompressed" ] || fail readback-$index
    echo "DEBIAN_INSTALL_CHUNK_OK index=$index sha256=$1"
done < /installer/chunks.tsv
set -- $(dd if="$target" bs={BLOCK_SIZE} count="{root_size // BLOCK_SIZE}" 2>/dev/null | sha256sum)
[ "$1" = "{root_sha256}" ] || fail final-image-hash
echo "DEBIAN_INSTALL_PASS sha256=$1 bytes={root_size}"
sync || fail final-sync
reboot -f
fail reboot-returned
""".encode()


def _canonical_root_url(root_url: str) -> str:
    if not root_url or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in root_url
    ):
        raise InstallerError("root URL contains control characters")
    try:
        parsed = urllib.parse.urlsplit(root_url)
        port = parsed.port
        address = ipaddress.IPv4Address(parsed.hostname or "")
    except ValueError as error:
        raise InstallerError("root URL must contain a literal IPv4 address") from error
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or ".." in PurePosixPath(parsed.path).parts
        or re.fullmatch(r"/[A-Za-z0-9._/-]+", parsed.path) is None
    ):
        raise InstallerError("root URL must be canonical uncredentialed HTTP")
    canonical = f"http://{address}:{port}{parsed.path}"
    if root_url != canonical:
        raise InstallerError("root URL must use canonical IPv4 syntax")
    return canonical


def render_network_init(root_sha256: str, root_size: int, root_url: str) -> bytes:
    """Render an Asterinas-only LAN installer with bounded verified streaming."""
    if len(root_sha256) != 64 or any(c not in "0123456789abcdef" for c in root_sha256):
        raise InstallerError("root SHA-256 must be lowercase hexadecimal")
    if root_size <= 0 or root_size % BLOCK_SIZE:
        raise InstallerError("root size must be a positive multiple of 4096")
    quoted_url = f"'{_canonical_root_url(root_url)}'"
    return f"""#!/bin/sh
set -o pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
hold() {{ while :; do sleep 3600; done; }}
fail() {{ echo "DEBIAN_INSTALL_FAIL reason=$1"; sync; hold; }}
mkdir -p /proc /sys /dev
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
cmdline=" $(cat /proc/cmdline) "
case "$cmdline" in *" asterinas.mmc_write_partition2 "*) ;; *) fail write-gate-not-armed ;; esac
case "$cmdline" in *" asterinas.debian_install_sha256={root_sha256} "*) ;; *) fail image-hash-not-armed ;; esac
target=/dev/mmcblk0p2
[ -b "$target" ] || fail target-not-block-device
[ "$(blockdev --getsize64 "$target")" = "{PARTITION_SIZE}" ] || fail target-size-mismatch
attempt=1
fetched_hash=
while [ "$attempt" -le 3 ]; do
    stream_hash=
    if stream_hash="$(wget -T 30 -O - {quoted_url} | gzip -dc | tee "$target" | sha256sum)"; then
        set -- $stream_hash
        if [ "$#" = 2 ] && [ "$1" = "{root_sha256}" ] && [ "$2" = "-" ]; then
            fetched_hash=$1
            break
        fi
    fi
    echo "DEBIAN_INSTALL_FETCH_RETRY attempt=$attempt"
    attempt=$((attempt + 1))
    sleep 2
done
[ "$fetched_hash" = "{root_sha256}" ] || fail network-fetch
sync || fail network-sync
echo "DEBIAN_INSTALL_FETCH_OK bytes={root_size} sha256=$fetched_hash"
echo "DEBIAN_INSTALL_PASS sha256=$fetched_hash bytes={root_size}"
sync || fail final-sync
reboot -f
fail reboot-returned
""".encode()


def _added_entry(name: str, mode: int, data: bytes, ino: int) -> NewcEntry:
    return NewcEntry(name, mode, ino, 2 if stat.S_ISDIR(mode) else 1, 0, 0, 0, 0, data)


def build_archive(
    base_cpio: Path,
    root_image: Path,
    output: Path,
    root_sha256: str,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> None:
    entries = list(parse_newc(base_cpio.read_bytes()))
    _validate_installer_runtime(entries)
    actual_hash = sha256_file(root_image)
    if actual_hash != root_sha256:
        raise InstallerError("root image SHA-256 mismatch")
    chunks = plan_chunks(root_image, chunk_size=chunk_size)
    names = {entry.name for entry in entries}
    if "init" not in names:
        raise InstallerError("base initramfs has no init")
    reserved = {"installer", "installer/chunks", "installer/chunks.tsv"}
    reserved.update(chunk.archive_path for chunk in chunks)
    if names & reserved:
        raise InstallerError("base initramfs already contains installer paths")
    init_data = render_init(root_sha256, root_image.stat().st_size, chunks)
    entries = [
        NewcEntry(
            entry.name,
            stat.S_IFREG | 0o755 if entry.name == "init" else entry.mode,
            entry.ino,
            entry.nlink,
            entry.devmajor,
            entry.devminor,
            entry.rdevmajor,
            entry.rdevminor,
            init_data if entry.name == "init" else entry.data,
        )
        for entry in entries
    ]
    next_ino = max(entry.ino for entry in entries) + 1
    additions = [
        _added_entry("installer", stat.S_IFDIR | 0o755, b"", next_ino),
        _added_entry("installer/chunks", stat.S_IFDIR | 0o755, b"", next_ino + 1),
        _added_entry(
            "installer/chunks.tsv",
            stat.S_IFREG | 0o644,
            _manifest(chunks),
            next_ino + 2,
        ),
    ]
    additions.extend(
        _added_entry(
            chunk.archive_path,
            stat.S_IFREG | 0o644,
            chunk.compressed,
            next_ino + 3 + chunk.index,
        )
        for chunk in chunks
    )
    archive = _encode_archive((*entries, *additions))
    _publish_archive(output, archive)


def _publish_archive(output: Path, archive: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=f".{output.name}.", delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(archive)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
        temporary = None
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_network_archive(
    base_cpio: Path,
    root_image: Path,
    output: Path,
    root_sha256: str,
    root_url: str,
) -> None:
    """Build a small installer that fetches the frozen root through Asterinas."""
    entries = list(parse_newc(base_cpio.read_bytes()))
    _validate_installer_runtime(entries, _NETWORK_INSTALLER_COMMANDS)
    actual_hash = sha256_file(root_image)
    if actual_hash != root_sha256:
        raise InstallerError("root image SHA-256 mismatch")
    if "init" not in {entry.name for entry in entries}:
        raise InstallerError("base initramfs has no init")
    init_data = render_network_init(root_sha256, root_image.stat().st_size, root_url)
    entries = [
        NewcEntry(
            entry.name,
            stat.S_IFREG | 0o755 if entry.name == "init" else entry.mode,
            entry.ino,
            entry.nlink,
            entry.devmajor,
            entry.devminor,
            entry.rdevmajor,
            entry.rdevminor,
            init_data if entry.name == "init" else entry.data,
        )
        for entry in entries
    ]
    _publish_archive(output, _encode_archive(entries))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-cpio", type=Path, required=True)
    parser.add_argument("--root-image", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--packages-lock", type=Path, required=True)
    parser.add_argument("--root-url")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    namespace = parser.parse_args(arguments)
    try:
        manifest = load_manifest(namespace.manifest)
        validate_frozen_root(namespace.root_image, manifest, namespace.packages_lock)
        if namespace.root_image.stat().st_size != ROOT_IMAGE_SIZE:
            raise InstallerError("Megrez Debian root image must be exactly 1 GiB")
        if namespace.root_url is None:
            build_archive(
                namespace.base_cpio,
                namespace.root_image,
                namespace.output,
                manifest.root_image_sha256,
            )
        else:
            build_network_archive(
                namespace.base_cpio,
                namespace.root_image,
                namespace.output,
                manifest.root_image_sha256,
                namespace.root_url,
            )
    except (ContractError, InstallerError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
