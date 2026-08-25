#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Pure protocol definitions for the Debian two-boot QEMU gate."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MAX_COMMAND_PAYLOAD_BYTES = 64 * 1024
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
GENERIC_SV39_CPU = "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true"
GATE_PACKAGE_NAMES = ("base-files", "libc6", "bash", "coreutils", "util-linux")

_NONCE_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_BASH_VERSION_RE = re.compile(r"\A[0-9]+\.[0-9]+(?:\.[0-9]+)?")
_PROTOCOL_MARKER_RE = re.compile(r"\A__ASTERINAS_DEBIAN_GATE_[A-Z0-9_]+__(?:[0-9]+)?\Z")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FATAL_TRANSCRIPT_MARKERS = (
    ("kernel panic", "kernel panic"),
    ("reboot: restarting system", "unexpected reboot"),
    ("rebooting in", "unexpected reboot"),
    ("ext2-fs error", "ext2 error"),
    ("ext2-fs warning", "ext2 warning"),
    ("buffer i/o error", "block I/O error"),
    ("blk_update_request: i/o error", "block I/O error"),
    ("end_request: i/o error", "block I/O error"),
    ("debian_rootfs_fail reason=", "stage1 failure"),
)
_SYSTEMD_READY_RE = re.compile(
    r"\ADEBIAN_SYSTEMD_M2_READY boot=([0-9]+) arch=([^ ]+) release=([^ ]+)\Z"
)
_SYSTEMD_REQUIRED_MOUNTS = (
    ("run-lock.mount", "Mounted run-lock.mount - Legacy Locks Directory /run/lock."),
    ("tmp.mount", "Mounted tmp.mount - Temporary Directory /tmp."),
)


@dataclass(frozen=True)
class ShellCommand:
    """One serial command framed by unique, line-oriented markers."""

    name: str
    payload: str
    begin_marker: str
    end_marker: str
    status_prefix: str


@dataclass(frozen=True)
class BootEvidence:
    """Identity and persistence evidence extracted from one boot."""

    boot_number: int
    architecture: str
    debian_release: str
    bash_version: str
    packages: tuple[tuple[str, str], ...]
    root_filesystem: str
    persistence_nonce: str
    second_probe: str | None


@dataclass(frozen=True)
class GateResult:
    """Classification outcome for a completely drained boot transcript."""

    passed: bool
    reason: str
    evidence: BootEvidence | None


def _validate_path_text(path: Path, *, role: str) -> None:
    raw_path = os.fspath(path)
    if not path.is_absolute():
        raise ValueError(f"QEMU {role} path must be absolute")
    if "," in raw_path:
        raise ValueError(f"QEMU {role} path must not contain a comma")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_path):
        raise ValueError(f"QEMU {role} path contains a control character")


def _validate_regular_input(path: Path, *, role: str) -> None:
    _validate_path_text(path, role=role)
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"QEMU {role} must be an existing regular file") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"QEMU {role} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"QEMU {role} must be a regular file")


def qemu_argv(
    *,
    uboot: Path,
    boot_disk: Path,
    root_disk: Path,
    monitor_socket: Path,
    smp: int = 4,
    dtb_enabled_cpu_count: int = 4,
    allow_reboot: bool = False,
) -> tuple[str, ...]:
    """Construct the frozen no-network SMP=4 two-disk QEMU contract."""

    if (
        isinstance(smp, bool)
        or isinstance(dtb_enabled_cpu_count, bool)
        or smp != 4
        or dtb_enabled_cpu_count != 4
    ):
        raise ValueError("QEMU and the DTB must expose exactly 4 enabled CPUs")
    _validate_regular_input(uboot, role="U-Boot")
    _validate_regular_input(boot_disk, role="boot disk")
    _validate_regular_input(root_disk, role="root disk")
    _validate_path_text(monitor_socket, role="monitor socket")
    if monitor_socket.exists() or monitor_socket.is_symlink():
        raise ValueError("QEMU monitor socket destination must not already exist")
    try:
        parent_metadata = monitor_socket.parent.lstat()
    except FileNotFoundError as error:
        raise ValueError(
            "QEMU monitor socket parent must be an existing directory"
        ) from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise ValueError("QEMU monitor socket parent must be a non-symlink directory")

    arguments = (
        "qemu-system-riscv64",
        "-machine",
        "virt",
        "-cpu",
        GENERIC_SV39_CPU,
        "-m",
        "2G",
        "-smp",
        "4",
        "-display",
        "none",
        "-nic",
        "none",
        "-serial",
        "stdio",
        "-kernel",
        os.fspath(uboot),
        "-drive",
        f"if=none,format=raw,file={boot_disk},id=bootdisk,readonly=on",
        "-device",
        "virtio-blk-device,drive=bootdisk",
        "-drive",
        f"if=none,format=raw,file={root_disk},id=rootdisk,cache=directsync",
        "-device",
        "virtio-blk-device,drive=rootdisk",
        "-monitor",
        f"unix:{monitor_socket},server=on,wait=off",
    )
    if allow_reboot:
        return arguments
    kernel_index = arguments.index("-kernel")
    return (*arguments[:kernel_index], "-no-reboot", *arguments[kernel_index:])


def _framed_command(
    *,
    boot_number: int,
    index: int,
    name: str,
    body: str,
    nonce: str,
) -> ShellCommand:
    token = (
        hashlib.sha256(f"{nonce}:{boot_number}:{index}:{name}".encode())
        .hexdigest()[:20]
        .upper()
    )
    marker_base = f"__ASTERINAS_DEBIAN_GATE_B{boot_number}_{index}_{token}"
    begin_marker = f"{marker_base}_BEGIN__"
    end_marker = f"{marker_base}_END__"
    status_prefix = f"{marker_base}_STATUS__"
    payload = (
        f"printf '%s\\n' '{begin_marker}'; "
        f"( {body} ); __asterinas_gate_status=$?; "
        f"printf '%s%d\\n' '{status_prefix}' \"$__asterinas_gate_status\"; "
        f"printf '%s\\n' '{end_marker}'"
    )
    if len(payload.encode("utf-8")) > MAX_COMMAND_PAYLOAD_BYTES:
        raise ValueError(f"command {name} payload exceeds 64 KiB")
    return ShellCommand(name, payload, begin_marker, end_marker, status_prefix)


def shell_commands(*, boot_number: int, nonce: str) -> tuple[ShellCommand, ...]:
    """Return the identity and persistence commands for boot one or two."""

    if isinstance(boot_number, bool) or boot_number not in (1, 2):
        raise ValueError("boot number must be 1 or 2")
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError(
            "persistence nonce must be exactly 64 lowercase hex characters"
        )

    command_bodies = [
        ("architecture", "uname -m"),
        ("debian-release", "cat /etc/debian_version"),
        ("bash-version", "printf '%s\\n' \"$BASH_VERSION\""),
        (
            "packages",
            "dpkg-query -W -f='${Package}\\t${Version}\\n' "
            f"{' '.join(GATE_PACKAGE_NAMES)} | LC_ALL=C sort",
        ),
        ("root-filesystem", "stat -f -c '%T' /"),
    ]
    persistence_directory = "/var/lib/asterinas-debian-m1"
    persistence_file = f"{persistence_directory}/persist"
    if boot_number == 1:
        command_bodies.append(
            (
                "persistence",
                f"install -d -m 0755 {persistence_directory} && "
                f"printf '%s\\n' '{nonce}' > {persistence_file} && sync && "
                f"cat {persistence_file}",
            )
        )
    else:
        command_bodies.extend(
            (
                ("persistence", f"cat {persistence_file}"),
                (
                    "second-probe",
                    f"printf '%s\\n' 'boot2-probe-created' > "
                    f"{persistence_directory}/second-probe && sync && "
                    "printf '%s\\n' 'boot2-probe-created'",
                ),
            )
        )

    return tuple(
        _framed_command(
            boot_number=boot_number,
            index=index,
            name=name,
            body=body,
            nonce=nonce,
        )
        for index, (name, body) in enumerate(command_bodies, start=1)
    )


def _failed(reason: str) -> GateResult:
    return GateResult(False, reason, None)


def classify_systemd_m2(
    transcript: str | bytes, *, expected_debian_release: str
) -> GateResult:
    """Classify one complete serial transcript spanning two systemd boots."""

    normalized = _normalize_transcript(transcript)
    if isinstance(normalized, GateResult):
        return normalized
    text, lines = normalized
    lowered = text.lower()
    if "debian_systemd_m2_fail reason=" in lowered:
        return _failed("systemd M2 failure marker")
    if (
        "failed to acquire watch file descriptor" in lowered
        or "failed to drain libmount events" in lowered
    ):
        return _failed("systemd mount monitor failure")
    if "mount process finished, but there is no mount" in lowered:
        return _failed("systemd mount protocol failure")
    for marker, description in _FATAL_TRANSCRIPT_MARKERS:
        if marker in lowered:
            return _failed(f"fatal transcript marker: {description}")
    if "oops:" in lowered:
        return _failed("fatal transcript marker: kernel oops")

    starts = [
        index for index, line in enumerate(lines) if line == "Starting kernel ..."
    ]
    ready: dict[int, list[tuple[int, str, str]]] = {1: [], 2: []}
    for index, line in enumerate(lines):
        match = _SYSTEMD_READY_RE.fullmatch(line)
        if match is None:
            continue
        boot = int(match.group(1), 10)
        if boot not in ready:
            return _failed("third systemd boot marker")
        ready[boot].append((index, match.group(2), match.group(3)))
    pass_positions = [
        index
        for index, line in enumerate(lines)
        if line == "DEBIAN_SYSTEMD_M2_PASS boot=2"
    ]
    firmware_positions = [
        index
        for index, line in enumerate(lines)
        if line.startswith("OpenSBI ") or line.startswith("U-Boot ")
    ]

    if len(ready[1]) != 1:
        qualifier = "duplicate" if len(ready[1]) > 1 else "missing"
        return _failed(f"{qualifier} boot 1 READY marker")
    if len(ready[2]) != 1:
        qualifier = "duplicate" if len(ready[2]) > 1 else "missing"
        return _failed(f"{qualifier} boot 2 READY marker")
    if len(pass_positions) != 1:
        qualifier = "duplicate" if len(pass_positions) > 1 else "missing"
        return _failed(f"{qualifier} PASS marker")
    if len(starts) != 2:
        return _failed("normal reboot evidence requires exactly two kernel starts")

    boot1_position, boot1_arch, boot1_release = ready[1][0]
    boot2_position, boot2_arch, boot2_release = ready[2][0]
    if not (
        starts[0] < boot1_position < starts[1] < boot2_position < pass_positions[0]
    ):
        return _failed("systemd M2 markers are reordered")
    if not any(
        boot1_position < position < starts[1] for position in firmware_positions
    ):
        return _failed("firmware restart evidence is missing")
    if boot1_arch != "riscv64" or boot2_arch != "riscv64":
        return _failed("systemd M2 architecture identity mismatch")
    if (
        boot1_release != expected_debian_release
        or boot2_release != expected_debian_release
    ):
        return _failed("systemd M2 Debian release identity mismatch")
    boot_windows = (
        (1, starts[0], boot1_position),
        (2, starts[1], boot2_position),
    )
    for boot_number, start, ready_position in boot_windows:
        boot_lines = lines[start + 1 : ready_position]
        for unit, marker in _SYSTEMD_REQUIRED_MOUNTS:
            if not any(marker in line for line in boot_lines):
                return _failed(f"missing successful {unit} mount in boot {boot_number}")
    return GateResult(True, "pass", None)


def _normalize_transcript(
    transcript: str | bytes,
) -> tuple[str, tuple[str, ...]] | GateResult:
    if isinstance(transcript, bytes):
        raw_size = len(transcript)
        text = transcript.decode("utf-8", errors="replace")
    elif isinstance(transcript, str):
        raw_size = len(transcript.encode("utf-8"))
        text = transcript
    else:
        return _failed("transcript must be bytes or text")
    if raw_size > MAX_TRANSCRIPT_BYTES:
        return _failed("serial transcript exceeds 8 MiB")
    text = _ANSI_ESCAPE_RE.sub("", text)
    return text, tuple(line.rstrip("\r") for line in text.splitlines())


def _extract_outputs(
    lines: tuple[str, ...], commands: tuple[ShellCommand, ...]
) -> dict[str, str] | GateResult:
    recognized_lines = {
        marker
        for command in commands
        for marker in (command.begin_marker, command.end_marker)
    }
    positions: list[tuple[int, int, int]] = []
    outputs: dict[str, str] = {}
    for command in commands:
        begin_positions = [
            index for index, line in enumerate(lines) if line == command.begin_marker
        ]
        end_positions = [
            index for index, line in enumerate(lines) if line == command.end_marker
        ]
        status_positions = [
            index
            for index, line in enumerate(lines)
            if line.startswith(command.status_prefix)
        ]
        if any(
            len(found) > 1
            for found in (begin_positions, status_positions, end_positions)
        ):
            return _failed(f"duplicate protocol marker for {command.name}")
        if any(
            len(found) == 0
            for found in (begin_positions, status_positions, end_positions)
        ):
            return _failed(f"missing protocol marker for {command.name}")
        begin, status_position, end = (
            begin_positions[0],
            status_positions[0],
            end_positions[0],
        )
        if not begin < status_position < end:
            return _failed(f"reordered protocol markers for {command.name}")
        status_text = lines[status_position][len(command.status_prefix) :]
        if not status_text.isascii() or not status_text.isdigit():
            return _failed(f"invalid command status for {command.name}")
        status = int(status_text, 10)
        if status != 0:
            return _failed(f"command {command.name} exited with status {status}")
        positions.append((begin, status_position, end))
        outputs[command.name] = "\n".join(lines[begin + 1 : status_position]).strip()

    if any(
        previous[2] >= following[0]
        for previous, following in zip(positions, positions[1:])
    ):
        return _failed("reordered protocol command markers")
    status_prefixes = tuple(command.status_prefix for command in commands)
    for line in lines:
        if (
            _PROTOCOL_MARKER_RE.fullmatch(line)
            and line not in recognized_lines
            and not line.startswith(status_prefixes)
        ):
            return _failed("stale protocol marker")
    return outputs


def classify_boot(
    transcript: str | bytes,
    commands: Iterable[ShellCommand],
    *,
    boot_number: int,
    expected_debian_release: str,
    expected_packages: Iterable[tuple[str, str]],
    expected_nonce: str,
) -> GateResult:
    """Classify complete serial evidence without launching or mutating anything."""

    if isinstance(boot_number, bool) or boot_number not in (1, 2):
        return _failed("boot number must be 1 or 2")
    if not _NONCE_RE.fullmatch(expected_nonce):
        return _failed("expected persistence nonce is invalid")
    command_tuple = tuple(commands)
    if not command_tuple:
        return _failed("protocol command list is empty")
    if len({command.name for command in command_tuple}) != len(command_tuple):
        return _failed("protocol command names must be unique")
    for command in command_tuple:
        if len(command.payload.encode("utf-8")) > MAX_COMMAND_PAYLOAD_BYTES:
            return _failed(f"command {command.name} payload exceeds 64 KiB")

    normalized = _normalize_transcript(transcript)
    if isinstance(normalized, GateResult):
        return normalized
    text, lines = normalized
    lowered = text.lower()
    for marker, description in _FATAL_TRANSCRIPT_MARKERS:
        if marker in lowered:
            return _failed(f"fatal transcript marker: {description}")

    extracted = _extract_outputs(lines, command_tuple)
    if isinstance(extracted, GateResult):
        return extracted
    required_names = {
        "architecture",
        "debian-release",
        "bash-version",
        "packages",
        "root-filesystem",
        "persistence",
    }
    if set(extracted) != required_names | (
        {"second-probe"} if boot_number == 2 else set()
    ):
        return _failed("protocol command set does not match boot phase")
    if extracted["architecture"] != "riscv64":
        return _failed("architecture identity mismatch")
    if extracted["debian-release"] != expected_debian_release:
        return _failed("Debian release identity mismatch")
    if not _BASH_VERSION_RE.match(extracted["bash-version"]):
        return _failed("Bash version identity is malformed")

    package_rows: list[tuple[str, str]] = []
    for line in extracted["packages"].splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not all(fields):
            return _failed("package identity output is malformed")
        package_rows.append((fields[0], fields[1]))
    expected_package_tuple = tuple(sorted(expected_packages))
    if tuple(package_rows) != expected_package_tuple:
        return _failed("package versions do not match the frozen manifest")
    if extracted["root-filesystem"] not in ("ext2", "ext2/ext3"):
        return _failed("root filesystem is not ext2")
    if extracted["persistence"] != expected_nonce:
        return _failed("persistence nonce is missing or mismatched")
    second_probe = extracted.get("second-probe")
    if boot_number == 2 and second_probe != "boot2-probe-created":
        return _failed("second probe evidence is missing or mismatched")

    evidence = BootEvidence(
        boot_number=boot_number,
        architecture=extracted["architecture"],
        debian_release=extracted["debian-release"],
        bash_version=extracted["bash-version"],
        packages=tuple(package_rows),
        root_filesystem=extracted["root-filesystem"],
        persistence_nonce=extracted["persistence"],
        second_probe=second_probe,
    )
    return GateResult(True, "pass", evidence)
