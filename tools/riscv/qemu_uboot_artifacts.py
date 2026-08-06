"""Artifact identity and guest-memory validation for QEMU U-Boot boots."""

from __future__ import annotations

import binascii
import hashlib
import json
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactExpectations:
    """Host-derived sizes and CRC32 values required from U-Boot."""

    kernel_size: int
    kernel_crc32: str
    dtb_size: int
    dtb_crc32: str
    initrd_size: int
    initrd_crc32: str
    kernel_sha256: str | None = None
    dtb_sha256: str | None = None
    initrd_sha256: str | None = None


DEFAULT_ARTIFACTS = ArtifactExpectations(
    kernel_size=11_326_096,
    kernel_crc32="57c40418",
    dtb_size=5_048,
    dtb_crc32="6e7844b8",
    initrd_size=3_411,
    initrd_crc32="153879f1",
    kernel_sha256="0" * 64,
    dtb_sha256="0" * 64,
    initrd_sha256="0" * 64,
)

DRAM_RANGE = range(0x8000_0000, 0x1_0000_0000)
KERNEL_LOAD_ADDRESS = 0x8020_0000
INITRD_LOAD_ADDRESS = 0x8300_0000
DTB_LOAD_ADDRESS = 0x8800_0000
DTB_EXPANSION_SIZE = 0x1000
LINUX_IMAGE_HEADER_SIZE = 64
LINUX_IMAGE_ENTRY_JUMP = 0x0400_006F
LINUX_IMAGE_TEXT_OFFSET = 0x20_0000
LINUX_IMAGE_HEADER_VERSION = 2
LINUX_IMAGE_MAGIC_TEXT = b"RISCV\0\0\0"
RISCV_IMAGE_MAGIC = 0x0543_5352


def _validate_linux_image(image: bytes) -> None:
    if len(image) < LINUX_IMAGE_HEADER_SIZE:
        raise ValueError("Linux Image is shorter than its 64-byte header")
    image_size = struct.unpack_from("<Q", image, 0x10)[0]
    if image_size != len(image):
        raise ValueError(
            f"Linux Image size is {len(image)} bytes but header declares {image_size}"
        )
    entry_jump = struct.unpack_from("<I", image, 0x00)[0]
    if entry_jump != LINUX_IMAGE_ENTRY_JUMP:
        raise ValueError(f"Linux Image has invalid entry jump: {entry_jump:#x}")
    if struct.unpack_from("<I", image, 0x04)[0] != 0:
        raise ValueError("Linux Image second entry instruction must be zero")
    text_offset = struct.unpack_from("<Q", image, 0x08)[0]
    if text_offset != LINUX_IMAGE_TEXT_OFFSET:
        raise ValueError(
            f"Linux Image text offset must be {LINUX_IMAGE_TEXT_OFFSET:#x}: "
            f"{text_offset:#x}"
        )
    if struct.unpack_from("<Q", image, 0x18)[0] != 0:
        raise ValueError("Linux Image flags must be zero")
    version = struct.unpack_from("<I", image, 0x20)[0]
    if version != LINUX_IMAGE_HEADER_VERSION:
        raise ValueError(
            f"Linux Image header version must be {LINUX_IMAGE_HEADER_VERSION}: "
            f"{version}"
        )
    if image[0x24:0x30] != bytes(12):
        raise ValueError("Linux Image reserved fields must be zero")
    if image[0x30:0x38] != LINUX_IMAGE_MAGIC_TEXT:
        raise ValueError("Linux Image has invalid RISCV text magic")
    magic = struct.unpack_from("<I", image, 0x38)[0]
    if magic != RISCV_IMAGE_MAGIC:
        raise ValueError(f"Linux Image has invalid RISC-V magic: {magic:#x}")
    if struct.unpack_from("<I", image, 0x3C)[0] != 0:
        raise ValueError("Linux Image final reserved field must be zero")


def payload_ranges(artifacts: ArtifactExpectations) -> dict[str, range]:
    """Return the occupied guest ranges, including DTB expansion space."""

    return {
        "kernel": range(
            KERNEL_LOAD_ADDRESS,
            KERNEL_LOAD_ADDRESS + artifacts.kernel_size,
        ),
        "initrd": range(
            INITRD_LOAD_ADDRESS,
            INITRD_LOAD_ADDRESS + artifacts.initrd_size,
        ),
        "dtb": range(
            DTB_LOAD_ADDRESS,
            DTB_LOAD_ADDRESS + artifacts.dtb_size + DTB_EXPANSION_SIZE,
        ),
    }


def _ranges_overlap(left: range, right: range) -> bool:
    return left.start < right.stop and right.start < left.stop


def validate_fixed_payload_layout(artifacts: ArtifactExpectations) -> None:
    """Reject payload ranges that overlap or fall outside modeled DRAM."""

    payloads = payload_ranges(artifacts)
    for name, payload in payloads.items():
        if payload.start < DRAM_RANGE.start or payload.stop > DRAM_RANGE.stop:
            raise ValueError(f"{name} falls outside modeled DRAM")
    names = tuple(payloads)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            if _ranges_overlap(payloads[left_name], payloads[right_name]):
                raise ValueError(f"{left_name} overlaps {right_name}")


def validate_bdinfo_memory_layout(
    serial_log: str,
    artifacts: ArtifactExpectations,
) -> None:
    """Validate payload ranges against U-Boot's live LMB memory map."""

    validate_fixed_payload_layout(artifacts)
    ranges: dict[str, list[range]] = {"memory": [], "reserved": []}
    pattern = re.compile(
        r"\b(memory|reserved)\[\d+\]\s+"
        r"\[(0x[0-9a-fA-F]+)-(0x[0-9a-fA-F]+)\]"
    )
    for kind, start_text, inclusive_end_text in pattern.findall(serial_log):
        start = int(start_text, 16)
        inclusive_end = int(inclusive_end_text, 16)
        if inclusive_end < start:
            raise ValueError(f"reversed {kind} range in bdinfo")
        stop = inclusive_end + 1
        ranges[kind].append(range(start, stop))
    if not ranges["memory"]:
        raise ValueError("bdinfo did not report an LMB memory range")
    if not ranges["reserved"]:
        raise ValueError("bdinfo did not report an LMB reserved range")

    for name, payload in payload_ranges(artifacts).items():
        if not any(
            memory.start <= payload.start and payload.stop <= memory.stop
            for memory in ranges["memory"]
        ):
            raise ValueError(f"{name} is outside U-Boot LMB memory")
        for reserved in ranges["reserved"]:
            if _ranges_overlap(payload, reserved):
                raise ValueError(f"{name} overlaps U-Boot reserved memory")


def artifact_expectations_from_paths(
    *, kernel: Path, dtb: Path, initrd: Path
) -> ArtifactExpectations:
    """Calculate exact U-Boot size and CRC gates from three host artifacts."""

    kernel_bytes = kernel.read_bytes()
    dtb_bytes = dtb.read_bytes()
    initrd_bytes = initrd.read_bytes()
    _validate_linux_image(kernel_bytes)
    artifacts = ArtifactExpectations(
        kernel_size=len(kernel_bytes),
        kernel_crc32=f"{binascii.crc32(kernel_bytes) & 0xFFFF_FFFF:08x}",
        dtb_size=len(dtb_bytes),
        dtb_crc32=f"{binascii.crc32(dtb_bytes) & 0xFFFF_FFFF:08x}",
        initrd_size=len(initrd_bytes),
        initrd_crc32=f"{binascii.crc32(initrd_bytes) & 0xFFFF_FFFF:08x}",
        kernel_sha256=hashlib.sha256(kernel_bytes).hexdigest(),
        dtb_sha256=hashlib.sha256(dtb_bytes).hexdigest(),
        initrd_sha256=hashlib.sha256(initrd_bytes).hexdigest(),
    )
    validate_fixed_payload_layout(artifacts)
    return artifacts


def verify_boot_disk_artifacts(
    *,
    boot_disk: Path,
    dtb_filename: str,
    expected: ArtifactExpectations,
) -> ArtifactExpectations:
    """Extract and identify every payload that U-Boot will read."""

    with tempfile.TemporaryDirectory(prefix="asterinas-qemu-payloads-") as tmp:
        directory = Path(tmp)
        paths = {
            "kernel": directory / "asterinas.booti",
            "dtb": directory / dtb_filename,
            "initrd": directory / "initramfs.cpio.gz",
        }
        for source, destination in (
            ("asterinas.booti", paths["kernel"]),
            (dtb_filename, paths["dtb"]),
            ("initramfs.cpio.gz", paths["initrd"]),
        ):
            try:
                subprocess.run(
                    [
                        "debugfs",
                        "-R",
                        f"dump -p /{source} {destination}",
                        str(boot_disk),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise ValueError(f"cannot extract boot payload: {source}") from error
        actual = artifact_expectations_from_paths(**paths)
    if actual != expected:
        raise ValueError("boot disk payload identities do not match the manifest")
    return actual


def load_artifact_manifest(path: Path) -> ArtifactExpectations:
    """Load host-derived artifact gates from a preparation manifest."""

    data = json.loads(path.read_text())
    artifacts = ArtifactExpectations(**data)
    for name in ("kernel_size", "dtb_size", "initrd_size"):
        if getattr(artifacts, name) <= 0:
            raise ValueError(f"{name} must be positive")
    for name in ("kernel_crc32", "dtb_crc32", "initrd_crc32"):
        if re.fullmatch(r"[0-9a-f]{8}", getattr(artifacts, name)) is None:
            raise ValueError(f"{name} must be eight lowercase hex digits")
    for name in ("kernel_sha256", "dtb_sha256", "initrd_sha256"):
        value = getattr(artifacts, name)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"{name} must be 64 lowercase hex digits")
    validate_fixed_payload_layout(artifacts)
    return artifacts
