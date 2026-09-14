#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Collect guest Firefox runtime identity and bind it to a rootfs manifest."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from typing import Any, Mapping


FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
MAX_IDENTITY_FILE_BYTES = 8 * 1024 * 1024
MAX_FRAMEBUFFER_BYTES = 64 * 1024 * 1024
REQUIRED_XORG_PACKAGES = (
    "xserver-xorg-core",
    "xserver-xorg-video-fbdev",
)
RUNTIME_FIELDS = {
    "schema_version",
    "display_provider",
    "framebuffer",
    "firefox",
    "packages",
}


class ProvenanceError(ValueError):
    """Firefox performance provenance violated its bounded schema."""


def _read_regular(path: Path, *, maximum: int = MAX_IDENTITY_FILE_BYTES) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ProvenanceError(f"identity input is missing: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise ProvenanceError(
                f"identity input is not a bounded regular file: {path}"
            )
        contents = bytearray()
        while len(contents) <= metadata.st_size:
            remaining = metadata.st_size + 1 - len(contents)
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            contents.extend(chunk)
        if len(contents) != metadata.st_size:
            raise ProvenanceError(f"identity input changed while reading: {path}")
        return bytes(contents)
    except OSError as error:
        raise ProvenanceError(f"identity input is unreadable: {path}") from error
    finally:
        os.close(descriptor)


def _parse_framebuffer_layout(variable: bytes, fixed: bytes) -> dict[str, int]:
    if len(variable) != 160 or len(fixed) != 80:
        raise ProvenanceError("framebuffer metadata size is invalid")
    width, height, virtual_width, virtual_height, xoffset, yoffset, bpp, grayscale = (
        struct.unpack_from("=8I", variable)
    )
    red = struct.unpack_from("=3I", variable, 32)
    green = struct.unpack_from("=3I", variable, 44)
    blue = struct.unpack_from("=3I", variable, 56)
    transparency = struct.unpack_from("=3I", variable, 68)
    nonstandard = struct.unpack_from("=I", variable, 80)[0]
    memory_bytes = struct.unpack_from("=I", fixed, 24)[0]
    visual = struct.unpack_from("=I", fixed, 36)[0]
    stride = struct.unpack_from("=I", fixed, 48)[0]
    if (
        not 0 < width <= 8192
        or not 0 < height <= 8192
        or virtual_width != width
        or virtual_height != height
        or xoffset != 0
        or yoffset != 0
        or bpp != 32
        or grayscale != 0
        or nonstandard != 0
        or red != (16, 8, 0)
        or green != (8, 8, 0)
        or blue != (0, 8, 0)
        or transparency != (24, 8, 0)
        or visual != 2
        or stride < width * 4
        or stride > width * 4 + 65536
        or stride * height > MAX_FRAMEBUFFER_BYTES
        or memory_bytes < stride * height
    ):
        raise ProvenanceError("framebuffer layout is unsupported")
    return {
        "width": width,
        "height": height,
        "stride_bytes": stride,
        "bits_per_pixel": bpp,
    }


def _parse_package_versions(contents: bytes) -> dict[str, str]:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProvenanceError("dpkg status is not UTF-8") from error
    installed: dict[str, str] = {}
    for paragraph in text.split("\n\n"):
        if not paragraph:
            continue
        fields: dict[str, str] = {}
        for line in paragraph.splitlines():
            if not line or line[0].isspace() or ": " not in line:
                continue
            name, value = line.split(": ", 1)
            if name in fields:
                raise ProvenanceError("dpkg status contains a duplicate field")
            fields[name] = value
        package = fields.get("Package")
        if package not in REQUIRED_XORG_PACKAGES:
            continue
        version = fields.get("Version", "")
        if (
            fields.get("Status") != "install ok installed"
            or not version
            or len(version) > 128
            or any(character.isspace() for character in version)
            or package in installed
        ):
            raise ProvenanceError(f"Xorg package identity is invalid: {package}")
        installed[package] = version
    if set(installed) != set(REQUIRED_XORG_PACKAGES):
        raise ProvenanceError("required Xorg package identity is missing")
    return {name: installed[name] for name in REQUIRED_XORG_PACKAGES}


def _require_runtime_executable(root: Path, relative: str) -> None:
    path = root / relative.lstrip("/")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        metadata = resolved.stat()
    except (OSError, ValueError) as error:
        raise ProvenanceError(f"Firefox executable is missing: {relative}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise ProvenanceError(f"Firefox executable is invalid: {relative}")


def build_runtime_provenance(
    *,
    root: Path,
    display_provider: str,
    framebuffer_variable: bytes,
    framebuffer_fixed: bytes,
) -> dict[str, object]:
    """Builds the exact guest-observed browser performance identity."""

    root = Path(root)
    if display_provider not in {"fbdev", "drm"}:
        raise ProvenanceError("display provider is invalid")
    packages = _parse_package_versions(
        _read_regular(root / "var/lib/dpkg/status")
    )
    marker = root / "usr/share/asterinas/firefox-riscv-jit-overlay.json"
    has_jit_overlay = marker.exists() or marker.is_symlink()
    executable = "/usr/bin/firefox" if has_jit_overlay else "/usr/bin/firefox-esr"
    if has_jit_overlay:
        _read_regular(marker, maximum=256 * 1024)
    _require_runtime_executable(root, executable)
    runtime = {
        "schema_version": 1,
        "display_provider": display_provider,
        "framebuffer": _parse_framebuffer_layout(
            framebuffer_variable, framebuffer_fixed
        ),
        "firefox": {
            "executable": executable,
            "jit_overlay": has_jit_overlay,
        },
        "packages": packages,
    }
    validate_runtime_provenance(runtime)
    return runtime


def _exact_mapping(
    value: object, fields: set[str], description: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProvenanceError(f"{description} fields are invalid")
    return value


def validate_runtime_provenance(runtime: object) -> dict[str, object]:
    """Validates and copies one exact guest runtime provenance object."""

    document = _exact_mapping(runtime, RUNTIME_FIELDS, "runtime provenance")
    if document["schema_version"] != 1 or type(document["schema_version"]) is not int:
        raise ProvenanceError("runtime provenance schema version is invalid")
    if document["display_provider"] not in {"fbdev", "drm"}:
        raise ProvenanceError("runtime provenance display provider is invalid")
    framebuffer = _exact_mapping(
        document["framebuffer"],
        {"width", "height", "stride_bytes", "bits_per_pixel"},
        "framebuffer provenance",
    )
    for name in ("width", "height", "stride_bytes", "bits_per_pixel"):
        value = framebuffer[name]
        if type(value) is not int or value <= 0:
            raise ProvenanceError(f"framebuffer provenance is invalid: {name}")
    if (
        framebuffer["width"] > 8192
        or framebuffer["height"] > 8192
        or framebuffer["bits_per_pixel"] != 32
        or framebuffer["stride_bytes"] < framebuffer["width"] * 4
        or framebuffer["stride_bytes"] * framebuffer["height"]
        > MAX_FRAMEBUFFER_BYTES
    ):
        raise ProvenanceError("framebuffer provenance is outside bounds")
    firefox = _exact_mapping(
        document["firefox"], {"executable", "jit_overlay"}, "Firefox provenance"
    )
    if type(firefox["jit_overlay"]) is not bool:
        raise ProvenanceError("Firefox overlay identity is invalid")
    expected_executable = (
        "/usr/bin/firefox" if firefox["jit_overlay"] else "/usr/bin/firefox-esr"
    )
    if firefox["executable"] != expected_executable:
        raise ProvenanceError("Firefox executable and overlay identity disagree")
    packages = _exact_mapping(
        document["packages"], set(REQUIRED_XORG_PACKAGES), "Xorg package provenance"
    )
    for name in REQUIRED_XORG_PACKAGES:
        version = packages[name]
        if (
            not isinstance(version, str)
            or not version
            or len(version) > 128
            or any(character.isspace() for character in version)
        ):
            raise ProvenanceError(f"Xorg package version is invalid: {name}")
    return {
        "schema_version": 1,
        "display_provider": document["display_provider"],
        "framebuffer": dict(framebuffer),
        "firefox": dict(firefox),
        "packages": dict(packages),
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ProvenanceError(f"duplicate JSON field: {name}")
        result[name] = value
    return result


def bind_runtime_provenance(
    runtime_contents: bytes, manifest: Path
) -> dict[str, object]:
    """Binds validated guest runtime identity to the final rootfs manifest."""

    if not 0 < len(runtime_contents) <= 256 * 1024:
        raise ProvenanceError("runtime provenance JSON size is invalid")
    try:
        runtime = json.loads(runtime_contents, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProvenanceError("runtime provenance JSON is invalid") from error
    result = validate_runtime_provenance(runtime)
    manifest_contents = _read_regular(Path(manifest))
    result["rootfs_manifest_sha256"] = hashlib.sha256(manifest_contents).hexdigest()
    return result


def _framebuffer_records(device: Path) -> tuple[bytes, bytes]:
    try:
        descriptor = os.open(
            device,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ProvenanceError("framebuffer device cannot be opened") from error
    try:
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise ProvenanceError("framebuffer device is not a character device")
        variable = bytearray(160)
        fixed = bytearray(80)
        fcntl.ioctl(descriptor, FBIOGET_VSCREENINFO, variable, True)
        fcntl.ioctl(descriptor, FBIOGET_FSCREENINFO, fixed, True)
        return bytes(variable), bytes(fixed)
    except OSError as error:
        raise ProvenanceError("framebuffer metadata ioctl failed") from error
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    path = Path(path)
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ProvenanceError("provenance output path is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect bounded Firefox runtime performance provenance"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument(
        "--display-provider",
        choices=("fbdev", "drm"),
        default=os.environ.get("ASTERINAS_DISPLAY_PROVIDER", "fbdev"),
    )
    values = parser.parse_args(arguments)
    try:
        variable, fixed = _framebuffer_records(values.root / "dev/fb0")
        runtime = build_runtime_provenance(
            root=values.root,
            display_provider=values.display_provider,
            framebuffer_variable=variable,
            framebuffer_fixed=fixed,
        )
        _atomic_write_json(values.output, runtime)
    except ProvenanceError as error:
        print(f"browser-performance-provenance: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
