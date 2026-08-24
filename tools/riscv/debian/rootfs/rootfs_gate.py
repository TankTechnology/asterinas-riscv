"""Orchestration boundary for the persistent Debian rootfs gate.

This module deliberately keeps the lifecycle independent from QEMU and filesystem
details.  Concrete operations are injected so the ordering and failure contract can
be tested without launching a guest.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class GateConfig:
    kernel: Path
    u_boot: Path
    dtb: Path
    stage1_initramfs: Path
    root_image: Path
    manifest: Path
    packages_lock: Path
    package_checksums: Path
    output_directory: Path
    smp: int = 4
    boot_timeout: float = 120.0
    command_timeout: float = 30.0
    cleanup_timeout: float = 10.0


class GateFailure(RuntimeError):
    """A gate failure with a stable, machine-readable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Operations(Protocol):
    """Dependencies used by :func:`orchestrate_gate`."""

    def invalidate(self, config: GateConfig) -> None: ...

    def snapshot_inputs(self, config: GateConfig) -> Mapping[str, str]: ...

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]: ...

    def prepare(
        self,
        config: GateConfig,
        snapshots: Mapping[str, str],
        identity: Mapping[str, object],
    ) -> Any: ...

    def launch(self, config: GateConfig, prepared: Any, boot_number: int) -> Any: ...

    def drive_uboot(self, session: Any, config: GateConfig) -> None: ...

    def enter_debian(self, session: Any, config: GateConfig) -> None: ...

    def execute_checks(
        self,
        session: Any,
        config: GateConfig,
        identity: Mapping[str, object],
        nonce: str,
    ) -> None: ...

    def request_quit(self, session: Any, config: GateConfig) -> None: ...

    def close_monitor(self, session: Any) -> None: ...

    def cleanup_process(self, session: Any, config: GateConfig) -> None: ...

    def drain_serial(self, session: Any, config: GateConfig) -> bytes: ...

    def hash_final_root(self, config: GateConfig, prepared: Any) -> str: ...

    def publish(
        self,
        config: GateConfig,
        prepared: Any | None,
        transcripts: tuple[bytes, bytes],
        result: dict[str, object],
    ) -> None: ...


def _failure_reason(error: BaseException, fallback: str) -> str:
    if isinstance(error, GateFailure) and error.reason:
        return error.reason
    return fallback


def _attempt(operation: Any, fallback: str) -> str | None:
    try:
        operation()
    except BaseException as error:
        return _failure_reason(error, fallback)
    return None


def _run_boot(
    config: GateConfig,
    operations: Operations,
    prepared: Any,
    identity: Mapping[str, object],
    nonce: str,
    boot_number: int,
) -> tuple[bytes, tuple[object, ...], str | None]:
    stage = f"launch{boot_number}"
    try:
        session = operations.launch(config, prepared, boot_number)
    except BaseException as error:
        return b"", (), _failure_reason(error, stage)

    argv = tuple(session.get("argv", ())) if isinstance(session, Mapping) else ()
    reason: str | None = None
    body = (
        (f"uboot{boot_number}", lambda: operations.drive_uboot(session, config)),
        (f"shell{boot_number}", lambda: operations.enter_debian(session, config)),
        (
            f"commands{boot_number}",
            lambda: operations.execute_checks(session, config, identity, nonce),
        ),
        (f"quit{boot_number}", lambda: operations.request_quit(session, config)),
    )
    for fallback, operation in body:
        reason = _attempt(operation, fallback)
        if reason is not None:
            break

    transcript = b""
    teardown = (
        (f"close{boot_number}", lambda: operations.close_monitor(session)),
        (
            f"cleanup{boot_number}",
            lambda: operations.cleanup_process(session, config),
        ),
    )
    for fallback, operation in teardown:
        teardown_reason = _attempt(operation, fallback)
        if reason is None:
            reason = teardown_reason
    try:
        transcript = operations.drain_serial(session, config)
    except BaseException as error:
        if reason is None:
            reason = _failure_reason(error, f"drain{boot_number}")
    return transcript, argv, reason


def orchestrate_gate(
    config: GateConfig, operations: Operations, *, nonce: str
) -> dict[str, object]:
    """Run the two-boot gate while preserving fail-closed publication semantics."""

    # Publication is unsafe if stale passing evidence cannot first be removed.
    operations.invalidate(config)

    prepared: Any | None = None
    identity: Mapping[str, object] = {}
    transcripts = [b"", b""]
    qemu_argv: list[tuple[object, ...]] = []
    phase_durations: dict[str, float] = {}
    reason: str | None = None
    snapshots: Mapping[str, str] = {}

    setup = (
        ("snapshot", lambda: operations.snapshot_inputs(config)),
        ("validate", lambda: operations.validate_inputs(config, snapshots)),
        ("prepare", lambda: operations.prepare(config, snapshots, identity)),
    )
    for fallback, operation in setup:
        started = time.monotonic()
        try:
            value = operation()
            if fallback == "snapshot":
                snapshots = value
            elif fallback == "validate":
                identity = value
            else:
                prepared = value
        except BaseException as error:
            reason = _failure_reason(error, fallback)
            break
        finally:
            phase_durations[fallback] = max(0.0, time.monotonic() - started)

    if reason is None:
        for boot_number in (1, 2):
            started = time.monotonic()
            transcript, argv, reason = _run_boot(
                config,
                operations,
                prepared,
                identity,
                nonce,
                boot_number,
            )
            phase_durations[f"boot{boot_number}"] = max(0.0, time.monotonic() - started)
            transcripts[boot_number - 1] = transcript
            if argv:
                qemu_argv.append(argv)
            if reason is not None:
                break

    final_root_sha256: str | None = None
    if reason is None:
        started = time.monotonic()
        try:
            final_root_sha256 = operations.hash_final_root(config, prepared)
        except BaseException as error:
            reason = _failure_reason(error, "hash-final-root")
        finally:
            phase_durations["hash-final-root"] = max(0.0, time.monotonic() - started)

    manifest_identity = {
        name: value for name, value in identity.items() if name != "packages"
    }
    result: dict[str, object] = {
        "passed": reason is None,
        "reason": reason or "pass",
        "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
        "qemu_argv": qemu_argv,
        "input_sha256": dict(snapshots),
        "final_root_sha256": final_root_sha256,
        "manifest_identity": manifest_identity,
        "package_identity": identity.get("packages", ()),
        "phase_durations_seconds": phase_durations,
    }
    operations.publish(config, prepared, (transcripts[0], transcripts[1]), result)
    return result


def _open_regular(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise GateFailure(f"unsafe input: {path}") from error
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise GateFailure(f"input is not a regular file: {path}")
    return descriptor


def _hash_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _copy_fd(descriptor: int, destination: Path) -> None:
    with destination.open("xb") as stream:
        offset = 0
        while chunk := os.pread(descriptor, 1024 * 1024, offset):
            stream.write(chunk)
            offset += len(chunk)


def _sync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_boot_image(kernel: Path, dtb: Path, initramfs: Path, output: Path) -> None:
    """Atomically build the fixed 64 MiB ext4 U-Boot payload disk."""

    sources = (Path(kernel), Path(dtb), Path(initramfs))
    output = Path(output)
    descriptors: list[int] = []
    try:
        for path in sources:
            descriptors.append(_open_regular(path))
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    temporary_image: Path | None = None
    try:
        if output.exists() and any(
            os.path.samestat(output.stat(), os.fstat(descriptor))
            for descriptor in descriptors
        ):
            raise GateFailure("boot image output aliases an input")
        before = [_hash_fd(descriptor) for descriptor in descriptors]
        with tempfile.TemporaryDirectory(
            prefix=f".{output.name}.payloads.", dir=output.parent
        ) as staging_name:
            staging = Path(staging_name)
            for descriptor, name in zip(
                descriptors,
                ("asterinas.booti", "qemu-virt.dtb", "stage1-initramfs.cpio"),
            ):
                _copy_fd(descriptor, staging / name)
            image_fd, image_name = tempfile.mkstemp(
                prefix=f".{output.name}.", dir=output.parent
            )
            temporary_image = Path(image_name)
            try:
                os.ftruncate(image_fd, 64 * 1024 * 1024)
            finally:
                os.close(image_fd)
            subprocess.run(
                ["mke2fs", "-q", "-t", "ext4", "-F", "-d", staging, temporary_image],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["debugfs", "-w", "-R", "rmdir /lost+found", temporary_image],
                check=True,
                capture_output=True,
            )
            listing = subprocess.run(
                ["debugfs", "-R", "ls -p /", temporary_image],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            names = {
                fields[5]
                for line in listing.splitlines()
                if len(fields := line.split("/")) >= 6
                and fields[5] not in ("", ".", "..")
            }
            expected = {
                "asterinas.booti",
                "qemu-virt.dtb",
                "stage1-initramfs.cpio",
            }
            if names != expected:
                raise GateFailure("boot image has unexpected root entries")
            if before != [_hash_fd(descriptor) for descriptor in descriptors]:
                raise GateFailure("boot image input changed during preparation")
            with temporary_image.open("rb") as stream:
                os.fsync(stream.fileno())
            os.chmod(temporary_image, 0o600)
            os.replace(temporary_image, output)
            temporary_image = None
            _sync_parent(output)
    except GateFailure:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise GateFailure("failed to build boot image") from error
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        if temporary_image is not None:
            temporary_image.unlink(missing_ok=True)


def copy_sparse_root(source_fd: int, destination: Path) -> tuple[str, str, str]:
    """Copy a pinned root descriptor sparsely and publish it atomically."""

    destination = Path(destination)
    source_stat = os.fstat(source_fd)
    if not stat.S_ISREG(source_stat.st_mode):
        raise GateFailure("root image descriptor is not a regular file")
    if destination.exists() and os.path.samestat(destination.stat(), source_stat):
        raise GateFailure("root copy output aliases its source")
    before = _hash_fd(source_fd)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            [
                "cp",
                "--reflink=auto",
                "--sparse=always",
                "--",
                f"/proc/self/fd/{source_fd}",
                temporary,
            ],
            check=True,
            capture_output=True,
            pass_fds=(source_fd,),
        )
        after = _hash_fd(source_fd)
        copied_fd = _open_regular(temporary)
        try:
            copied = _hash_fd(copied_fd)
            os.fchmod(copied_fd, 0o600)
            os.fsync(copied_fd)
        finally:
            os.close(copied_fd)
        if before != after or copied != before:
            raise GateFailure("root image changed or copied incorrectly")
        os.replace(temporary, destination)
        _sync_parent(destination)
        return before, after, copied
    except GateFailure:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise GateFailure("failed to copy root image") from error
    finally:
        temporary.unlink(missing_ok=True)


def _positive_timeout(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive")
    return parsed


def parse_gate_args(arguments: list[str] | None = None) -> GateConfig:
    parser = argparse.ArgumentParser(description="Run the Debian persistent-root gate")
    for option in (
        "kernel",
        "uboot",
        "dtb",
        "stage1-initramfs",
        "root-image",
        "root-manifest",
        "packages-lock",
        "package-checksums",
        "output-directory",
    ):
        parser.add_argument(f"--{option}", required=True, type=Path)
    parser.add_argument("--smp", type=int, choices=(4,), default=4)
    parser.add_argument("--boot-timeout", type=_positive_timeout, default=120.0)
    parser.add_argument("--command-timeout", type=_positive_timeout, default=30.0)
    parser.add_argument("--cleanup-timeout", type=_positive_timeout, default=10.0)
    values = parser.parse_args(arguments)
    return GateConfig(
        values.kernel,
        values.uboot,
        values.dtb,
        values.stage1_initramfs,
        values.root_image,
        values.root_manifest,
        values.packages_lock,
        values.package_checksums,
        values.output_directory,
        values.smp,
        values.boot_timeout,
        values.command_timeout,
        values.cleanup_timeout,
    )


def verify_four_hart_dtb(dtb: Path) -> int:
    """Return four only when the real DTB has exactly four enabled CPU nodes."""

    descriptor = _open_regular(Path(dtb))
    dtb_fd_path = f"/proc/self/fd/{descriptor}"
    try:
        children = subprocess.run(
            ["fdtget", "-l", dtb_fd_path, "/cpus"],
            check=True,
            text=True,
            capture_output=True,
            pass_fds=(descriptor,),
        ).stdout.splitlines()
        enabled = 0
        for child in children:
            node = f"/cpus/{child}"
            device_type = subprocess.run(
                ["fdtget", "-d", "", dtb_fd_path, node, "device_type"],
                check=True,
                text=True,
                capture_output=True,
                pass_fds=(descriptor,),
            ).stdout.strip()
            if device_type != "cpu":
                continue
            status = subprocess.run(
                ["fdtget", "-d", "okay", dtb_fd_path, node, "status"],
                check=True,
                text=True,
                capture_output=True,
                pass_fds=(descriptor,),
            ).stdout.strip()
            if status in ("ok", "okay"):
                enabled += 1
    except (OSError, subprocess.SubprocessError) as error:
        raise GateFailure("failed to inspect DTB with fdtget") from error
    finally:
        os.close(descriptor)
    if enabled != 4:
        raise GateFailure("DTB must contain exactly 4 enabled CPU nodes")
    return enabled


def main(arguments: list[str] | None = None) -> int:
    """Run the concrete gate while keeping the backend an internal module."""

    from tools.riscv.debian.rootfs.rootfs_gate_backend import main as backend_main

    return backend_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
