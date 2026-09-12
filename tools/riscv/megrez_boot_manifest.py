#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict reset-safe extlinux generations for the Megrez board."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
import zlib


MAX_EXTLINUX_BYTES = 16 * 1024
ARTIFACT_ORDER = ("kernel", "initramfs", "megrez_dtb")
_DIRECTIVE_TO_ARTIFACT = {
    "linux": "kernel",
    "initrd": "initramfs",
    "fdt": "megrez_dtb",
}
_DIRECTIVE_ORDER = ("default", "label", "linux", "initrd", "fdt", "append")
_SAFE_LABEL = re.compile(r"[a-z0-9][a-z0-9.-]*")
_SAFE_PATH = re.compile(
    r"/[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*"
)
_SAFE_BASE_URL = re.compile(
    r"http://(?:[0-9]{1,3}\.){3}[0-9]{1,3}:[1-9][0-9]{0,4}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*"
)
_NONCE = re.compile(r"[0-9a-f]{32}")


class BootManifestError(ValueError):
    """One extlinux generation is unsafe, stale, or internally inconsistent."""


@dataclass(frozen=True)
class ExtlinuxGeneration:
    """The deterministic extlinux subset used by the Megrez Asterinas entry."""

    default: str
    label: str
    linux: str
    initrd: str
    fdt: str
    append: str

    @classmethod
    def from_bytes(cls, data: bytes) -> ExtlinuxGeneration:
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_EXTLINUX_BYTES:
            raise BootManifestError("extlinux configuration has an invalid size")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BootManifestError("extlinux configuration is not UTF-8") from error

        directives: dict[str, str] = {}
        for line in text.splitlines():
            if not line:
                continue
            name, separator, value = line.partition(" ")
            if name not in _DIRECTIVE_ORDER:
                raise BootManifestError(f"unknown extlinux directive: {name}")
            if not separator or not value or value != value.strip():
                raise BootManifestError(f"invalid {name} directive")
            if name in directives:
                raise BootManifestError(f"duplicate {name} directive")
            directives[name] = value

        for name in _DIRECTIVE_ORDER:
            if name not in directives:
                raise BootManifestError(f"missing {name} directive")
        if directives["default"] != directives["label"]:
            raise BootManifestError("default and label differ")
        if _SAFE_LABEL.fullmatch(directives["label"]) is None:
            raise BootManifestError("unsafe extlinux label")
        for name in _DIRECTIVE_TO_ARTIFACT:
            if _SAFE_PATH.fullmatch(directives[name]) is None:
                raise BootManifestError(f"unsafe {name} path")

        return cls(**directives)

    @property
    def artifact_paths(self) -> dict[str, str]:
        return {
            artifact: getattr(self, directive)
            for directive, artifact in _DIRECTIVE_TO_ARTIFACT.items()
        }

    def canonical_bytes(self) -> bytes:
        return "".join(
            f"{name} {getattr(self, name)}\n" for name in _DIRECTIVE_ORDER
        ).encode()

    def validate_against_plan(self, plan: Any) -> None:
        try:
            plan.validate()
        except (AttributeError, TypeError, ValueError) as error:
            raise BootManifestError(f"invalid debug plan: {error}") from error
        if self.label != f"asterinas-{plan.plan_sha256[:12]}":
            raise BootManifestError("extlinux label does not identify the debug plan")
        if self.append != plan.bootargs:
            raise BootManifestError("extlinux append does not match debug plan bootargs")

        identities = {identity.name: identity for identity in plan.artifacts}
        if any(name not in identities for name in ARTIFACT_ORDER):
            raise BootManifestError("debug plan lacks a Megrez boot artifact")
        for name, path in self.artifact_paths.items():
            identity = identities[name]
            if identity.sha256[:12] not in Path(path).name:
                raise BootManifestError(
                    f"{name}: immutable SHA-256 prefix is absent from the basename"
                )

    def validate_staged_directory(self, root: Path, plan: Any) -> tuple[str, ...]:
        try:
            root_metadata = root.lstat()
        except OSError as error:
            raise BootManifestError(f"staged directory is unavailable: {error}") from error
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise BootManifestError("staged directory is not a non-symlink directory")

        paths: dict[str, Path] = {}
        metadata: dict[str, os.stat_result] = {}
        for name in ARTIFACT_ORDER:
            path = root / self.artifact_paths[name].removeprefix("/")
            paths[name] = path
            try:
                current = path.lstat()
            except FileNotFoundError as error:
                raise BootManifestError(f"{name}: staged file is missing") from error
            except OSError as error:
                raise BootManifestError(f"{name}: staged file is unavailable") from error
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise BootManifestError(
                    f"{name}: staged file is not a regular non-symlink file"
                )
            metadata[name] = current

        self.validate_against_plan(plan)
        identities = {identity.name: identity for identity in plan.artifacts}
        for name in ARTIFACT_ORDER:
            identity = identities[name]
            current = metadata[name]
            if current.st_size != identity.size:
                raise BootManifestError(f"{name}: staged size mismatch")
            payload = _read_regular_unchanged(paths[name], current, identity.size, name)
            if hashlib.sha256(payload).hexdigest() != identity.sha256:
                raise BootManifestError(f"{name}: staged SHA-256 mismatch")
            if f"{zlib.crc32(payload):08x}" != identity.crc32:
                raise BootManifestError(f"{name}: staged CRC32 mismatch")
        return ARTIFACT_ORDER


def _read_regular_unchanged(
    path: Path, before: os.stat_result, expected_size: int, name: str
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BootManifestError(f"{name}: staged file changed while opening") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != expected_size
        ):
            raise BootManifestError(f"{name}: staged file changed while opening")
        payload = bytearray()
        while len(payload) <= expected_size:
            chunk = os.read(descriptor, expected_size + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(payload) != expected_size
            or after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise BootManifestError(f"{name}: staged file changed while reading")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _publication_inputs(
    generation: ExtlinuxGeneration, plan: Any, base_url: str, nonce: str
) -> dict[str, Any]:
    generation.validate_against_plan(plan)
    if not isinstance(base_url, str) or _SAFE_BASE_URL.fullmatch(base_url) is None:
        raise BootManifestError("RockOS publication base URL is unsafe")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise BootManifestError("RockOS publication nonce is invalid")
    identities = {identity.name: identity for identity in plan.artifacts}
    return identities


def rockos_publication_commands(
    generation: ExtlinuxGeneration, plan: Any, base_url: str, nonce: str
) -> tuple[str, ...]:
    """Build a nonce-bound, config-last RockOS publication transaction."""

    identities = _publication_inputs(generation, plan, base_url, nonce)
    config = generation.canonical_bytes()
    required = sum(identities[name].size for name in ARTIFACT_ORDER) + len(config)
    commands = [
        "_partition=$(findmnt -n -o SOURCE -- /boot); "
        "_available=$(df -B1 --output=avail /boot | tail -n 1); "
        f"if [ \"$_partition\" = /dev/mmcblk1p1 ] && [ \"$_available\" -ge {required} ]; "
        "then _asterinas_publish_ok=1; printf '__ASTERINAS_ROCKOS_PUBLISH_BEGIN__ "
        f"nonce={nonce} partition=%s status=0\\n' \"$_partition\"; "
        "else _asterinas_publish_ok=0; printf '__ASTERINAS_ROCKOS_PUBLISH_BEGIN__ "
        f"nonce={nonce} partition=%s status=1\\n' \"$_partition\"; fi"
    ]
    for name in ARTIFACT_ORDER:
        identity = identities[name]
        relative = generation.artifact_paths[name].removeprefix("/")
        destination = f"/boot/{relative}"
        temporary = f"/tmp/{Path(relative).name}.{nonce}.part"
        source = f"{base_url}/{relative}"
        commands.append(
            f"_destination={destination}; _temporary={temporary}; "
            "if [ \"$_asterinas_publish_ok\" = 1 ] "
            f"&& curl --fail --silent --show-error --location --output \"$_temporary\" {source} "
            f"&& [ \"$(stat -c %s -- \"$_temporary\")\" = {identity.size} ] "
            f"&& printf '%s  %s\\n' {identity.sha256} \"$_temporary\" | sha256sum -c - >/dev/null "
            "&& { if [ -e \"$_destination\" ]; then "
            f"printf '%s  %s\\n' {identity.sha256} \"$_destination\" | sha256sum -c - >/dev/null; "
            "else install -D -m 0444 -- \"$_temporary\" \"$_destination\"; fi; } "
            "&& sync; then printf '%s\\n' '__ASTERINAS_ROCKOS_PUBLISH_ITEM__ "
            f"nonce={nonce} name={name} status=0'; "
            "else _asterinas_publish_ok=0; printf '%s\\n' '__ASTERINAS_ROCKOS_PUBLISH_ITEM__ "
            f"nonce={nonce} name={name} status=1'; fi"
        )

    config_sha256 = hashlib.sha256(config).hexdigest()
    config_path = "/boot/extlinux/asterinas.conf"
    config_temporary = f"/boot/extlinux/.asterinas.conf.{nonce}.part"
    config_source = f"{base_url}/extlinux/asterinas.conf"
    commands.append(
        f"_destination={config_path}; _temporary={config_temporary}; "
        "mkdir -p -- /boot/extlinux; "
        "if [ \"$_asterinas_publish_ok\" = 1 ] "
        f"&& curl --fail --silent --show-error --location --output \"$_temporary\" {config_source} "
        f"&& [ \"$(stat -c %s -- \"$_temporary\")\" = {len(config)} ] "
        f"&& printf '%s  %s\\n' {config_sha256} \"$_temporary\" | sha256sum -c - >/dev/null "
        "&& sync && mv -f -- \"$_temporary\" \"$_destination\" && sync "
        f"&& printf '%s  %s\\n' {config_sha256} \"$_destination\" | sha256sum -c - >/dev/null; "
        "then printf '%s\\n' '__ASTERINAS_ROCKOS_PUBLISH_ITEM__ "
        f"nonce={nonce} name=extlinux status=0'; "
        "else _asterinas_publish_ok=0; printf '%s\\n' '__ASTERINAS_ROCKOS_PUBLISH_ITEM__ "
        f"nonce={nonce} name=extlinux status=1'; fi"
    )
    commands.append(
        "printf '%s\\n' \"__ASTERINAS_ROCKOS_PUBLISH_END__ "
        f"nonce={nonce} artifacts=3 config=1 "
        'status=$((1 - _asterinas_publish_ok))"'
    )
    return tuple(commands)


def publication_manifest_bytes(
    generation: ExtlinuxGeneration, plan: Any, base_url: str, nonce: str
) -> bytes:
    """Return the canonical host receipt input for one publication attempt."""

    identities = _publication_inputs(generation, plan, base_url, nonce)
    config = generation.canonical_bytes()
    document = {
        "schema_version": 1,
        "plan_sha256": plan.plan_sha256,
        "nonce": nonce,
        "base_url": base_url,
        "artifacts": [
            {
                "name": name,
                "path": generation.artifact_paths[name],
                "size": identities[name].size,
                "sha256": identities[name].sha256,
                "crc32": identities[name].crc32,
                "load_address": identities[name].load_address,
            }
            for name in ARTIFACT_ORDER
        ],
        "extlinux": {
            "path": "/extlinux/asterinas.conf",
            "size": len(config),
            "sha256": hashlib.sha256(config).hexdigest(),
            "contents": config.decode(),
        },
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
