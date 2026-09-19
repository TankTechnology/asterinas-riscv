# SPDX-License-Identifier: MPL-2.0

"""Bounded, content-addressed Megrez desktop boot workflow."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


GENERATION_ROOT = "/home/debian/asterinas/boot"
ARTIFACT_NAMES = ("kernel", "initramfs", "megrez_dtb")
BOOTARGS = " ".join(
    (
        "console=ttyS0",
        "loglevel=info",
        "asterinas.klog_capture=info",
        "init=/init",
        "asterinas.mmc_write_partition2",
        "asterinas.reboot_after=300",
        "systemd.mask=asterinas-browser-web-evidence.service",
        "systemd.mask=asterinas-desktop-m5-network.service",
        "systemd.mask=serial-getty@ttyS0.service",
        "systemd.mask=console-getty.service",
        "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1",
        "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy",
        "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1",
        "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=9",
        "--",
        "--root-init=systemd",
        "--debug-console=isolated-root",
        "--volatile-home",
    )
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DesktopBootError(RuntimeError):
    pass


@dataclass(frozen=True)
class DesktopBootArtifact:
    name: str
    source: str
    basename: str
    size: int
    sha256: str
    crc32: str
    load_address: int
    mmc_path: str = ""


@dataclass(frozen=True)
class DesktopBootManifest:
    schema_version: int
    stage1_protocol_version: int
    plan_sha256: str
    expected_root_sha256: str
    bootargs: str
    artifacts: dict[str, DesktopBootArtifact]
    generation_sha256: str
    generation_directory: str

    @classmethod
    def from_plan(cls, plan_path: Path) -> "DesktopBootManifest":
        payload = plan_path.read_bytes()
        try:
            plan: dict[str, Any] = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DesktopBootError(f"invalid plan: {error}") from error
        rows = plan.get("artifacts")
        if plan.get("schema_version") != 2 or not isinstance(rows, list):
            raise DesktopBootError("unsupported desktop boot plan")
        by_name = {row.get("name"): row for row in rows if isinstance(row, dict)}
        missing = set((*ARTIFACT_NAMES, "root_image")) - by_name.keys()
        if missing:
            raise DesktopBootError(f"plan is missing artifacts: {sorted(missing)}")

        artifacts: dict[str, DesktopBootArtifact] = {}
        for name in ARTIFACT_NAMES:
            row = by_name[name]
            source = Path(row.get("path", ""))
            try:
                metadata = source.stat()
                content = source.read_bytes()
            except OSError as error:
                raise DesktopBootError(f"{name}: cannot read source: {error}") from error
            actual_sha = hashlib.sha256(content).hexdigest()
            actual_crc = f"{zlib.crc32(content):08x}"
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != row.get("size")
                or actual_sha != row.get("sha256")
                or actual_crc != row.get("crc32")
            ):
                raise DesktopBootError(f"{name}: identity mismatch")
            suffix = {"kernel": "Image", "initramfs": "cpio", "megrez_dtb": "dtb"}[name]
            basename = f"{name}-{actual_sha[:16]}.{suffix}"
            artifacts[name] = DesktopBootArtifact(
                name=name,
                source=str(source.resolve()),
                basename=basename,
                size=metadata.st_size,
                sha256=actual_sha,
                crc32=actual_crc,
                load_address=row.get("load_address"),
            )
        root_sha = by_name["root_image"].get("sha256")
        if not isinstance(root_sha, str) or _SHA256.fullmatch(root_sha) is None:
            raise DesktopBootError("root image identity is invalid")
        identity = {
            "schema_version": 1,
            "stage1_protocol_version": 1,
            "plan_sha256": hashlib.sha256(payload).hexdigest(),
            "expected_root_sha256": root_sha,
            "bootargs": BOOTARGS,
            "artifacts": {name: asdict(artifacts[name]) for name in ARTIFACT_NAMES},
        }
        generation_sha = hashlib.sha256(_canonical(identity)).hexdigest()
        generation_directory = f"{GENERATION_ROOT}/{generation_sha[:16]}"
        artifacts = {
            name: replace(
                artifact,
                mmc_path=f"{generation_directory}/{artifact.basename}",
            )
            for name, artifact in artifacts.items()
        }
        return cls(
            **identity | {
                "artifacts": artifacts,
                "generation_sha256": generation_sha,
                "generation_directory": generation_directory,
            }
        )

    def canonical_bytes(self) -> bytes:
        return _canonical(
            {
                **asdict(self),
                "artifacts": {
                    name: asdict(self.artifacts[name]) for name in ARTIFACT_NAMES
                },
            }
        )


def _canonical(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def uboot_load_commands(manifest: DesktopBootManifest) -> tuple[str, ...]:
    return tuple(
        f"ext4load mmc 1:3 0x{artifact.load_address:x} {artifact.mmc_path}"
        for artifact in (manifest.artifacts[name] for name in ARTIFACT_NAMES)
    )
