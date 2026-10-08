#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the same diskless Stage1 probe on developer QEMU and RockOS TCG."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
from typing import Protocol
import tty

from tools.riscv.debian.rootfs.gate_runtime import (
    PinnedOutputDirectory,
    SerialConsole,
    launch_process,
)
from tools.riscv.megrez_probe import (
    classify_probe_transcript,
    encode_probe_request,
    qemu_probe_argv,
    validate_probe_names,
)


MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
ROCKOS_CACHE_ROOT = Path("/tmp/asterinas-qemu-probe")
_SAFE_TARGET = re.compile(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+")
_SAFE_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class ArtifactIdentity:
    size: int
    sha256: str


def read_identity(path: Path) -> ArtifactIdentity:
    """Pin and hash one bounded regular artifact without following a symlink."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("artifact must be a nonempty regular file below 64 MiB")
        digest = hashlib.sha256()
        observed_size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            observed_size += len(chunk)
            if observed_size > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact exceeds 64 MiB")
            digest.update(chunk)
        if observed_size != metadata.st_size:
            raise ValueError("artifact changed during hashing")
        return ArtifactIdentity(observed_size, digest.hexdigest())
    finally:
        os.close(descriptor)


def qemu_bootargs() -> str:
    """Select only the fixed Stage1 probe, with a bounded guest reboot."""

    return (
        "console=ttyS0 loglevel=info asterinas.klog_capture=info "
        "init=/init asterinas.reboot_after=180 -- --root-init=probe"
    )


def _seconds(value: int) -> str:
    if type(value) is not int or not 10 <= value <= 600:
        raise ValueError("QEMU timeout must be an integer from 10 to 600 seconds")
    return str(value)


def _qemu(kernel: Path, initramfs: Path) -> tuple[str, ...]:
    return (*qemu_probe_argv(kernel, initramfs, qemu_bootargs()), "-accel", "tcg")


def developer_argv(
    container: str, kernel: Path, initramfs: Path, seconds: int
) -> tuple[str, ...]:
    """Bound QEMU inside the current persistent developer container."""

    if _SAFE_CONTAINER.fullmatch(container) is None:
        raise ValueError("unsafe developer container name")
    return (
        "docker",
        "exec",
        "-it",
        "--workdir",
        "/root/asterinas",
        container,
        "timeout",
        "-k",
        "5",
        _seconds(seconds),
        *_qemu(kernel, initramfs),
    )


def rockos_argv(
    target: str, kernel: Path, initramfs: Path, seconds: int
) -> tuple[str, ...]:
    """Bound a low-priority TCG guest on RockOS through one SSH PTY."""

    if _SAFE_TARGET.fullmatch(target) is None:
        raise ValueError("unsafe RockOS SSH target")
    for artifact in (kernel, initramfs):
        _remote_path(artifact)
    remote = (
        "timeout",
        "-k",
        "5",
        _seconds(seconds),
        "nice",
        "-n",
        "10",
        *_qemu(kernel, initramfs),
    )
    return (
        "ssh",
        "-tt",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        target,
        shlex.join(remote),
    )


def cache_directory(kernel: ArtifactIdentity, initramfs: ArtifactIdentity) -> Path:
    """Name one immutable temporary cache for an exact artifact pair."""

    for identity in (kernel, initramfs):
        if identity.size <= 0 or re.fullmatch(r"[0-9a-f]{64}", identity.sha256) is None:
            raise ValueError("invalid QEMU artifact identity")
    return ROCKOS_CACHE_ROOT / f"{kernel.sha256[:16]}-{initramfs.sha256[:16]}"


def _remote_path(path: Path) -> str:
    if ROCKOS_CACHE_ROOT not in path.parents or any(
        part in {".", ".."} or re.fullmatch(r"[A-Za-z0-9._-]+", part) is None
        for part in path.relative_to(ROCKOS_CACHE_ROOT).parts
    ):
        raise ValueError("unsafe RockOS probe cache path")
    return shlex.quote(str(path))


def _ssh_argv(target: str, command: str) -> tuple[str, ...]:
    if _SAFE_TARGET.fullmatch(target) is None:
        raise ValueError("unsafe RockOS SSH target")
    return (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=5",
        target,
        command,
    )


def remote_identity_argv(target: str, path: Path) -> tuple[str, ...]:
    """Render a no-follow size and SHA-256 check for one RockOS cache file."""

    quoted = _remote_path(path)
    command = (
        f"if test -L {quoted}; then exit 3; fi; "
        f"if test ! -e {quoted}; then exit 4; fi; "
        f"test -f {quoted} || exit 3; "
        f"stat -c %s -- {quoted}; sha256sum -- {quoted}"
    )
    return _ssh_argv(target, command)


class ArtifactTransport(Protocol):
    def ensure_directory(self, path: Path) -> None: ...
    def identity(self, path: Path) -> ArtifactIdentity | None: ...
    def copy(self, source: Path, destination: Path) -> None: ...
    def promote(self, partial: Path, destination: Path) -> None: ...
    def remove_partial(self, path: Path) -> None: ...


def stage_artifact(
    transport: ArtifactTransport,
    source: Path,
    expected: ArtifactIdentity,
    destination: Path,
    *,
    nonce: str,
) -> Path:
    """Reuse verified cached bytes or promote one verified temporary transfer."""

    _remote_path(destination)
    if re.fullmatch(r"[0-9a-f]{16}", nonce) is None:
        raise ValueError("invalid transfer nonce")
    transport.ensure_directory(destination.parent)
    observed = transport.identity(destination)
    if observed is not None:
        if observed != expected:
            raise ValueError("cached artifact identity mismatch")
        return destination
    partial = destination.with_name(f".{destination.name}.{nonce}.part")
    try:
        transport.copy(source, partial)
        if transport.identity(partial) != expected:
            raise ValueError("transferred artifact identity mismatch")
        transport.promote(partial, destination)
        if transport.identity(destination) != expected:
            raise ValueError("promoted artifact identity mismatch")
    finally:
        transport.remove_partial(partial)
    return destination


class RockOsArtifactTransport:
    """Bounded SSH/SCP operations confined to the temporary probe cache."""

    def __init__(self, target: str, *, operation_seconds: int = 60) -> None:
        if _SAFE_TARGET.fullmatch(target) is None:
            raise ValueError("unsafe RockOS SSH target")
        self.target = target
        self.operation_seconds = int(_seconds(operation_seconds))

    def _run(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv, capture_output=True, check=False, timeout=self.operation_seconds
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("RockOS artifact operation timed out") from error

    def _ssh(self, command: str) -> None:
        result = self._run(_ssh_argv(self.target, command))
        if result.returncode != 0:
            raise RuntimeError(
                f"RockOS cache command exited {result.returncode}: "
                f"{result.stderr[:256].decode(errors='replace')}"
            )

    def ensure_directory(self, path: Path) -> None:
        quoted = _remote_path(path)
        root = shlex.quote(str(ROCKOS_CACHE_ROOT))
        self._ssh(
            f"test ! -L {root} || exit 3; mkdir -p -- {quoted}; "
            f"test ! -L {quoted} || exit 3"
        )

    def identity(self, path: Path) -> ArtifactIdentity | None:
        result = self._run(remote_identity_argv(self.target, path))
        if result.returncode == 4:
            return None
        if result.returncode != 0:
            raise RuntimeError(
                f"RockOS identity check exited {result.returncode}: "
                f"{result.stderr[:256].decode(errors='replace')}"
            )
        lines = result.stdout.decode("ascii", errors="strict").splitlines()
        if len(lines) != 2 or re.fullmatch(r"[0-9]+", lines[0]) is None:
            raise ValueError("malformed RockOS artifact identity")
        checksum = lines[1].split("  ", 1)
        if (
            len(checksum) != 2
            or checksum[1] != str(path)
            or re.fullmatch(r"[0-9a-f]{64}", checksum[0]) is None
        ):
            raise ValueError("malformed RockOS SHA-256 output")
        return ArtifactIdentity(int(lines[0]), checksum[0])

    def copy(self, source: Path, destination: Path) -> None:
        _remote_path(destination)
        result = self._run(
            (
                "scp",
                "-q",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=5",
                str(source),
                f"{self.target}:{destination}",
            )
        )
        if result.returncode != 0:
            raise RuntimeError(f"RockOS transfer exited {result.returncode}")

    def promote(self, partial: Path, destination: Path) -> None:
        self._ssh(f"mv -n -- {_remote_path(partial)} {_remote_path(destination)}")

    def remove_partial(self, path: Path) -> None:
        self._ssh(f"rm -f -- {_remote_path(path)}")


@dataclass(frozen=True)
class SessionEvidence:
    passed: bool
    reason: str
    phase: str
    serial: bytes
    elapsed_seconds: float
    exit_status: int | None


def run_session(argv: tuple[str, ...], nonce: str, seconds: int) -> SessionEvidence:
    """Require one nonce-bound Stage1 exchange and a clean QEMU reboot exit."""

    if not argv or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("invalid QEMU probe command or nonce")
    if type(seconds) is not int or not 1 <= seconds <= 600:
        raise ValueError("session timeout must be an integer from 1 to 600 seconds")
    selected = validate_probe_names(("boot", "syscall213"))
    started = time.monotonic()
    deadline = started + seconds
    phase = "launching-qemu"
    master = -1
    slave = -1
    process = None
    serial = None
    reason = ""
    passed = False
    try:
        master, slave = os.openpty()
        tty.setraw(slave)
        process = launch_process(argv, stdio_fd=slave)
        os.close(slave)
        slave = -1
        serial = SerialConsole(master, process=process, max_bytes=256 * 1024)
        phase = "waiting-ready"
        serial.wait_for(b"ASTERINAS_PROBE_READY v=1 pid=1", deadline)
        phase = "running-probes"
        serial.send(encode_probe_request(nonce, selected), deadline)
        reboot_ready = f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}".encode()
        serial.wait_for(reboot_ready, deadline)
        exchange = classify_probe_transcript(
            serial.transcript, nonce, ("boot", "syscall213")
        )
        fatal_markers = (
            b"kernel panic",
            b"uncaught panic:",
            b"not syncing",
            b"oops:",
            b"fatal exception",
        )
        phase = "requesting-reboot"
        serial.send(f"ASTERINAS_PROBE_REBOOT v=1 nonce={nonce}\n".encode(), deadline)
        phase = "waiting-qemu-exit"
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("QEMU reboot exit deadline expired")
            serial.drain(min(deadline, now + 0.05))
        exit_status = process.wait(deadline)
        if time.monotonic() < deadline:
            serial.drain(min(deadline, time.monotonic() + 0.2))
        if exit_status != 0:
            raise RuntimeError(f"QEMU exited with status {exit_status}")
        phase = "qemu-exited"
        if any(marker in serial.transcript.lower() for marker in fatal_markers):
            raise RuntimeError("fatal kernel panic or exception marker in QEMU serial")
        exchange = classify_probe_transcript(
            serial.transcript, nonce, ("boot", "syscall213")
        )
        passed = exchange.passed
        reason = "probe-pass" if passed else "guest probe failed"
    except (
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        EOFError,
        BufferError,
    ) as error:
        reason = str(error)
    finally:
        if process is not None and process.poll() is None:
            now = time.monotonic()
            try:
                process.terminate_group(now + 1, now + 3)
            except TimeoutError as error:
                reason = f"{reason}; cleanup failed: {error}"
                passed = False
        if slave >= 0:
            os.close(slave)
        transcript = serial.transcript if serial is not None else b""
        if master >= 0:
            os.close(master)
    return SessionEvidence(
        passed,
        reason,
        phase,
        transcript,
        round(time.monotonic() - started, 3),
        process.poll() if process is not None else None,
    )


def publish_evidence(
    output_root: Path,
    host: str,
    session: SessionEvidence,
    kernel: ArtifactIdentity,
    initramfs: ArtifactIdentity,
    nonce: str,
) -> Path:
    """Keep each virtual host's bounded serial transcript and result separate."""

    if host not in {"developer-qemu", "rockos-qemu-tcg"}:
        raise ValueError("unsupported QEMU probe host label")
    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("invalid QEMU probe nonce")
    if len(session.serial) > 256 * 1024:
        raise ValueError("QEMU serial evidence exceeds the byte cap")
    directory = Path(output_root) / host
    directory.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1,
        "host": host,
        "physical": False,
        "machine": "qemu-virt-tcg",
        "passed": session.passed,
        "reason": session.reason,
        "phase": session.phase,
        "nonce": nonce,
        "selected_probes": ["boot", "syscall213"],
        "kernel_sha256": kernel.sha256,
        "kernel_size": kernel.size,
        "initramfs_sha256": initramfs.sha256,
        "initramfs_size": initramfs.size,
        "elapsed_seconds": session.elapsed_seconds,
        "qemu_exit_status": session.exit_status,
    }
    with PinnedOutputDirectory(directory) as output:
        output.lock_exclusive()
        output.atomic_write("serial.log", session.serial)
        output.atomic_write(
            "result.json",
            (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
    return directory


def container_path(source: Path, workspace: Path) -> Path:
    """Map an artifact in this worktree to its persistent-container mount."""

    actual = Path(source).absolute()
    try:
        relative = actual.relative_to(Path(workspace).resolve())
    except ValueError as error:
        raise ValueError("QEMU artifact is outside the mounted worktree") from error
    if any(part in {".", ".."} for part in relative.parts):
        raise ValueError("QEMU artifact is outside the mounted worktree")
    return Path("/root/asterinas") / relative


def snapshot_artifacts(
    workspace: Path,
    kernel_source: Path,
    kernel_identity: ArtifactIdentity,
    initramfs_source: Path,
    initramfs_identity: ArtifactIdentity,
    *,
    nonce: str,
) -> tuple[Path, Path]:
    """Copy both sources into a unique read-only run snapshot before either host opens them."""

    if re.fullmatch(r"[0-9a-f]{16}", nonce) is None:
        raise ValueError("invalid artifact snapshot nonce")
    root = Path(workspace) / "target-ubuntu/dual-host-qemu-probe/snapshots"
    root.mkdir(parents=True, exist_ok=True)
    directory = root / nonce
    directory.mkdir(exist_ok=False)
    kernel = directory / "kernel.Image"
    initramfs = directory / "initramfs.cpio"
    with PinnedOutputDirectory(directory) as output:
        output.lock_exclusive()
        output.atomic_copy(kernel.name, kernel_source, mode=0o444)
        output.atomic_copy(initramfs.name, initramfs_source, mode=0o444)
    if (
        read_identity(kernel) != kernel_identity
        or read_identity(initramfs) != initramfs_identity
    ):
        raise ValueError("artifact snapshot identity mismatch after source changed")
    return kernel, initramfs


def parse_args(arguments: tuple[str, ...]) -> argparse.Namespace:
    """Require an explicit immutable artifact pair and RockOS SSH target."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initramfs", type=Path, required=True)
    parser.add_argument("--rockos", required=True)
    parser.add_argument("--container")
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--qemu-seconds", type=int, default=210)
    values = parser.parse_args(arguments)
    if _SAFE_TARGET.fullmatch(values.rockos) is None:
        raise ValueError("unsafe RockOS SSH target")
    _seconds(values.qemu_seconds)
    if (
        values.container is not None
        and _SAFE_CONTAINER.fullmatch(values.container) is None
    ):
        raise ValueError("unsafe developer container name")
    return values


def discover_container(workspace: Path) -> str:
    """Find the already-running persistent container bound to this worktree."""

    launcher = workspace / "tools/docker/run_dev_container.sh"
    result = subprocess.run(
        (str(launcher), "--status"), capture_output=True, check=False, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError("persistent developer container status is unavailable")
    status = json.loads(result.stdout)
    if status.get("workspace") != str(workspace) or status.get("state") != "running":
        raise RuntimeError("start the persistent developer container before the probe")
    container = status.get("container")
    if not isinstance(container, str) or _SAFE_CONTAINER.fullmatch(container) is None:
        raise RuntimeError("invalid persistent developer container identity")
    return container


def _failure(reason: str, phase: str) -> SessionEvidence:
    return SessionEvidence(False, reason, phase, b"", 0.0, None)


def default_output_root(workspace: Path, *, timestamp: str, suffix: str) -> Path:
    """Choose an ignored host-owned tree rather than Docker's root-owned target."""

    if (
        re.fullmatch(r"[0-9]{8}T[0-9]{6}", timestamp) is None
        or re.fullmatch(r"[0-9a-f]{8}", suffix) is None
    ):
        raise ValueError("invalid dual-host evidence run identity")
    return (
        Path(workspace)
        / "target-ubuntu/dual-host-qemu-probe/runs"
        / f"{timestamp}-{suffix}"
    )


def main(arguments: tuple[str, ...] | None = None) -> int:
    """Publish separate results for two virtual hosts using identical bytes."""

    try:
        values = parse_args(tuple(sys.argv[1:] if arguments is None else arguments))
        workspace = Path(__file__).resolve().parents[2]
        kernel_source = values.kernel.absolute()
        initramfs_source = values.initramfs.absolute()
        kernel = read_identity(kernel_source)
        initramfs = read_identity(initramfs_source)
        container_path(kernel_source, workspace)
        container_path(initramfs_source, workspace)
        kernel_snapshot, initramfs_snapshot = snapshot_artifacts(
            workspace,
            kernel_source,
            kernel,
            initramfs_source,
            initramfs,
            nonce=secrets.token_hex(8),
        )
        kernel_container = container_path(kernel_snapshot, workspace)
        initramfs_container = container_path(initramfs_snapshot, workspace)
        output_root = values.output_directory or default_output_root(
            workspace,
            timestamp=time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
            suffix=secrets.token_hex(4),
        )
        output_root.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"dual-host-qemu-probe: {error}", file=sys.stderr)
        return 2

    developer_nonce = secrets.token_hex(16)
    try:
        container = values.container or discover_container(workspace)
        developer = run_session(
            developer_argv(
                container,
                kernel_container,
                initramfs_container,
                values.qemu_seconds,
            ),
            developer_nonce,
            values.qemu_seconds + 10,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        developer = _failure(str(error), "launching-developer-qemu")
    publish_evidence(
        output_root,
        "developer-qemu",
        developer,
        kernel,
        initramfs,
        developer_nonce,
    )

    rockos_nonce = secrets.token_hex(16)
    try:
        transport = RockOsArtifactTransport(values.rockos)
        cached = cache_directory(kernel, initramfs)
        rockos_kernel = stage_artifact(
            transport,
            kernel_snapshot,
            kernel,
            cached / "kernel.Image",
            nonce=secrets.token_hex(8),
        )
        rockos_initramfs = stage_artifact(
            transport,
            initramfs_snapshot,
            initramfs,
            cached / "initramfs.cpio",
            nonce=secrets.token_hex(8),
        )
        rockos = run_session(
            rockos_argv(
                values.rockos,
                rockos_kernel,
                rockos_initramfs,
                values.qemu_seconds,
            ),
            rockos_nonce,
            values.qemu_seconds + 10,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        rockos = _failure(str(error), "staging-or-launching-rockos-qemu")
    publish_evidence(
        output_root,
        "rockos-qemu-tcg",
        rockos,
        kernel,
        initramfs,
        rockos_nonce,
    )
    print(
        f"DUAL_HOST_QEMU_PROBE developer={int(developer.passed)} "
        f"rockos={int(rockos.passed)} kernel_sha256={kernel.sha256} "
        f"initramfs_sha256={initramfs.sha256} evidence={output_root}"
    )
    return 0 if developer.passed and rockos.passed else 1


if __name__ == "__main__":
    sys.exit(main())
