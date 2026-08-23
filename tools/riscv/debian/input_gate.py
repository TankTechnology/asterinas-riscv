#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Define the host-side protocol for the Debian RISC-V input gate."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


READY_MARKER = b"__DEBIAN_INPUT_GATE_READY__"
PASS_MARKER = b"__DEBIAN_INPUT_GATE_PASS__"
KEY_SEQUENCE = ("a", "shift-b", "backspace", "ctrl-c")
PANIC_MARKERS = (b"Kernel panic", b"kernel panic", b"BUG:", b"panic!")
KERNEL_START_MARKER = b"Starting kernel"
BOOT_DISK_NAME = "boot.ext4"
MONITOR_SOCKET_NAME = "monitor.sock"
SERIAL_LOG_NAME = "serial.log"
RESULT_JSON_NAME = "result.json"
MINIMUM_DISK_SIZE_KIB = 64 * 1024
DISK_OVERHEAD_KIB = 16 * 1024
HMP_INTER_KEY_DELAY_SECONDS = 0.05
PROCESS_TERM_GRACE_SECONDS = 2.0

BOOT_COMMANDS = (
    "version",
    "virtio scan",
    "ext4ls virtio 0:0 /",
    "ext4load virtio 0:0 0x80200000 /asterinas.booti",
    "ext4load virtio 0:0 0x90000000 /qemu-virt.dtb",
    "fdt addr 0x90000000",
    "setenv bootargs console=ttyS0 loglevel=warn init=/init",
    "ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz",
    "setenv initrd_size ${filesize}",
    "booti 0x80200000 0x83000000:${initrd_size} 0x90000000",
)


class MonitorError(RuntimeError):
    """Report a failure to connect to or command the QEMU monitor."""


class EarlyProcessExit(RuntimeError):
    """Report that QEMU exited before the gate reached a terminal marker."""


class BootConsole(Protocol):
    """Describe the serial operations needed by the boot orchestration."""

    transcript: bytes

    def wait_for(self, marker: bytes, timeout: float) -> None: ...

    def send_line(self, command: str) -> None: ...

    def drain(self, timeout: float) -> None: ...


class Monitor(Protocol):
    """Describe the HMP operations needed by the input gate."""

    def connect(self) -> None: ...

    def send_key(self, key: str) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class GateConfig:
    """Configure one isolated Debian input-gate run."""

    kernel: Path
    uboot: Path
    dtb: Path
    initramfs: Path
    output_dir: Path
    smp: int = 4
    startup_timeout: float = 30.0
    command_timeout: float = 10.0
    input_timeout: float = 30.0


@dataclass(frozen=True)
class GateRunResult:
    """Record auditable evidence from one input-gate run."""

    smp: int
    ready: bool
    complete: bool
    panics: tuple[str, ...]
    passed: bool
    qemu_argv: list[str]
    sha256: dict[str, str]
    terminal_reason: str

    def to_json(self) -> dict[str, Any]:
        """Return the stable JSON representation of this result."""

        result = asdict(self)
        result["panics"] = list(self.panics)
        return result


@dataclass(frozen=True)
class GateDependencies:
    """Provide replaceable system boundaries for gate orchestration."""

    prepare_boot_disk: Callable[[GateConfig, Path], None]
    launch_process: Callable[[list[str]], Any]
    boot_console: Callable[[Any], BootConsole]
    monitor: Callable[[Path, float], Monitor]
    cleanup_process: Callable[[Any], None]


@dataclass(frozen=True)
class GateResult:
    """Summarize marker evidence found in a gate transcript."""

    ready: bool
    complete: bool
    panics: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Report whether the transcript proves successful completion."""

        return self.ready and self.complete and not self.panics


def qemu_argv(
    uboot: Path,
    boot_disk: Path,
    monitor_socket: Path,
    smp: int = 4,
) -> list[str]:
    """Build the deterministic, network-isolated QEMU command line."""

    if isinstance(smp, bool) or not isinstance(smp, int) or smp <= 0:
        raise ValueError("SMP must be a strictly positive integer")
    if "," in os.fspath(boot_disk):
        raise ValueError("QEMU boot disk path must not contain a comma")
    if "," in os.fspath(monitor_socket):
        raise ValueError("monitor_socket must not contain a comma")

    return [
        "qemu-system-riscv64",
        "-machine",
        "virt",
        "-m",
        "2G",
        "-smp",
        str(smp),
        "-no-reboot",
        "-kernel",
        str(uboot),
        "-drive",
        f"if=none,format=raw,file={boot_disk},id=bootdisk",
        "-device",
        "virtio-blk-device,drive=bootdisk",
        "-device",
        "virtio-tablet-device",
        "-device",
        "virtio-keyboard-device",
        "-display",
        "none",
        "-monitor",
        f"unix:{monitor_socket},server=on,wait=off",
        "-serial",
        "stdio",
        "-nic",
        "none",
    ]


def classify_transcript(transcript: bytes) -> GateResult:
    """Classify readiness, completion, and panic evidence in a transcript."""

    panics = tuple(
        marker.decode("ascii") for marker in PANIC_MARKERS if marker in transcript
    )
    return GateResult(
        ready=READY_MARKER in transcript,
        complete=PASS_MARKER in transcript,
        panics=panics,
    )


def validate_config(config: GateConfig) -> GateConfig:
    """Validate paths and numeric limits, returning resolved paths."""

    for name in ("startup_timeout", "command_timeout", "input_timeout"):
        timeout = getattr(config, name)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{name} must be finite and positive")

    resolved_artifacts = {}
    for name in ("kernel", "uboot", "dtb", "initramfs"):
        path = Path(getattr(config, name)).absolute()
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise ValueError(
                f"{name} must be a nonempty regular file: {path}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{name} must not be a symbolic link: {path}")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise ValueError(f"{name} must be a nonempty regular file: {path}")
        resolved_artifacts[name] = path.resolve(strict=True)

    output_dir = Path(config.output_dir).absolute()
    if "," in os.fspath(output_dir):
        raise ValueError("output directory path must not contain a comma")
    if output_dir.exists() or output_dir.is_symlink():
        metadata = output_dir.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(
                f"output directory must not be a symbolic link: {output_dir}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"output directory must be a directory: {output_dir}")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError(
                f"output directory must have exact mode 0700: {output_dir}"
            )
    else:
        output_dir.mkdir(mode=0o700, parents=True)
        output_dir.chmod(0o700)
        if stat.S_IMODE(output_dir.lstat().st_mode) != 0o700:
            raise ValueError(
                f"output directory must have exact mode 0700: {output_dir}"
            )
    output_dir = output_dir.resolve(strict=True)

    # Reuse the command builder as the single authority for SMP validation.
    qemu_argv(
        resolved_artifacts["uboot"],
        output_dir / BOOT_DISK_NAME,
        output_dir / MONITOR_SOCKET_NAME,
        config.smp,
    )
    return GateConfig(
        **resolved_artifacts,
        output_dir=output_dir,
        smp=config.smp,
        startup_timeout=float(config.startup_timeout),
        command_timeout=float(config.command_timeout),
        input_timeout=float(config.input_timeout),
    )


def prepare_boot_disk(
    config: GateConfig,
    boot_disk: Path,
    *,
    run_command: Callable[[list[str]], None] | None = None,
) -> None:
    """Atomically build a private ext4 disk containing the three boot files."""

    if run_command is None:
        run_command = _run_checked_command

    payloads = (
        (config.kernel, "asterinas.booti"),
        (config.initramfs, "initramfs.cpio.gz"),
        (config.dtb, "qemu-virt.dtb"),
    )
    payload_size = sum(source.stat().st_size for source, _ in payloads)
    payload_size_kib = (payload_size + 1023) // 1024
    disk_size_kib = max(
        MINIMUM_DISK_SIZE_KIB,
        payload_size_kib + DISK_OVERHEAD_KIB,
    )

    temporary_disk: Path | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{BOOT_DISK_NAME}.stage.",
            dir=config.output_dir,
        ) as temporary_directory:
            stage = Path(temporary_directory)
            for source, destination_name in payloads:
                shutil.copyfile(source, stage / destination_name)

            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{BOOT_DISK_NAME}.",
                dir=config.output_dir,
            )
            os.close(file_descriptor)
            temporary_disk = Path(temporary_name)
            run_command(
                [
                    "mkfs.ext4",
                    "-q",
                    "-F",
                    "-d",
                    str(stage),
                    str(temporary_disk),
                    str(disk_size_kib),
                ]
            )
            run_command(
                [
                    "debugfs",
                    "-w",
                    "-R",
                    "rmdir lost+found",
                    str(temporary_disk),
                ]
            )
            os.replace(temporary_disk, boot_disk)
            temporary_disk = None
    finally:
        if temporary_disk is not None:
            temporary_disk.unlink(missing_ok=True)


def run_gate(
    config: GateConfig,
    dependencies: GateDependencies | None = None,
) -> GateRunResult:
    """Run the QEMU input gate and atomically persist its evidence."""

    config = validate_config(config)
    dependencies = dependencies or default_dependencies()
    boot_disk = config.output_dir / BOOT_DISK_NAME
    monitor_socket = config.output_dir / MONITOR_SOCKET_NAME
    dependencies.prepare_boot_disk(config, boot_disk)
    argv = qemu_argv(config.uboot, boot_disk, monitor_socket, config.smp)
    identities = _artifact_identities(config, boot_disk)

    process: Any | None = None
    boot: BootConsole | None = None
    monitor = dependencies.monitor(monitor_socket, config.command_timeout)
    terminal_reason = "orchestration failure"
    timeout_reason = "timeout: U-Boot prompt"
    lifecycle_failures: list[str] = []
    try:
        process = dependencies.launch_process(argv)
        boot = dependencies.boot_console(process)
        boot.wait_for(b"=> ", config.startup_timeout)
        for command in BOOT_COMMANDS[:-1]:
            boot.send_line(command)
            timeout_reason = "timeout: U-Boot command"
            boot.wait_for(b"=> ", config.command_timeout)
        boot.send_line(BOOT_COMMANDS[-1])
        timeout_reason = "timeout: kernel start"
        boot.wait_for(KERNEL_START_MARKER, config.startup_timeout)
        timeout_reason = "timeout: guest READY"
        boot.wait_for(READY_MARKER, config.startup_timeout)

        monitor.connect()
        for key in KEY_SEQUENCE:
            monitor.send_key(key)
        timeout_reason = "timeout: guest PASS"
        boot.wait_for(PASS_MARKER, config.input_timeout)
        terminal_reason = "passed"
    except TimeoutError:
        terminal_reason = timeout_reason
    except MonitorError:
        terminal_reason = "monitor failure"
    except EarlyProcessExit:
        terminal_reason = "early process exit"
    except (BrokenPipeError, EOFError):
        terminal_reason = "early process exit"
    except Exception as error:  # Evidence is more valuable than hiding system failures.
        terminal_reason = f"orchestration failure: {type(error).__name__}"
    finally:
        try:
            monitor.close()
        except Exception:
            lifecycle_failures.append("monitor close failure")
        if process is not None:
            try:
                dependencies.cleanup_process(process)
            except Exception:
                lifecycle_failures.append("cleanup failure")
        if boot is not None:
            try:
                boot.drain(config.command_timeout)
            except TimeoutError:
                lifecycle_failures.append("serial drain timeout")
            except Exception:
                lifecycle_failures.append("serial drain failure")

    if lifecycle_failures:
        terminal_reason = "; ".join(lifecycle_failures)

    transcript = boot.transcript if boot is not None else b""
    classification = classify_transcript(transcript)
    if classification.panics:
        if terminal_reason == "passed":
            terminal_reason = "panic detected"
        else:
            terminal_reason += "; panic detected"
    passed = classification.passed and terminal_reason == "passed"
    result = GateRunResult(
        smp=config.smp,
        ready=classification.ready,
        complete=classification.complete,
        panics=classification.panics,
        passed=passed,
        qemu_argv=argv,
        sha256=identities,
        terminal_reason=terminal_reason,
    )
    _write_evidence(config.output_dir, transcript, result)
    return result


class SerialBootConsole:
    """Drive U-Boot and the guest through QEMU's serial pipes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._search_offset = 0
        self.transcript = b""

    def wait_for(self, marker: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            marker_offset = self.transcript.find(marker, self._search_offset)
            if marker_offset >= 0:
                self._search_offset = marker_offset + len(marker)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(marker.decode("ascii", errors="replace"))
            stdout = self._process.stdout
            if stdout is None:
                raise EarlyProcessExit("QEMU stdout is unavailable")
            readable, _, _ = select.select([stdout], [], [], remaining)
            if not readable:
                raise TimeoutError(marker.decode("ascii", errors="replace"))
            chunk = os.read(stdout.fileno(), 4096)
            if not chunk:
                status = self._process.poll()
                raise EarlyProcessExit(f"QEMU exited with status {status}")
            self.transcript += chunk

    def send_line(self, command: str) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise EarlyProcessExit("QEMU stdin is unavailable")
        try:
            stdin.write(command.encode("ascii") + b"\n")
            stdin.flush()
        except BrokenPipeError as error:
            raise EarlyProcessExit("QEMU closed its serial input") from error

    def drain(self, timeout: float) -> None:
        """Read all remaining serial bytes, requiring EOF within the bound."""

        stdout = self._process.stdout
        if stdout is None:
            raise OSError("QEMU stdout is unavailable")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("serial drain")
            readable, _, _ = select.select([stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("serial drain")
            chunk = os.read(stdout.fileno(), 4096)
            if not chunk:
                return
            self.transcript += chunk


class HmpMonitor:
    """Send reviewed key commands through a bounded HMP connection."""

    def __init__(self, path: Path, timeout: float) -> None:
        self._path = path
        self._timeout = timeout
        self._socket: socket.socket | None = None

    def connect(self) -> None:
        deadline = time.monotonic() + self._timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                candidate.settimeout(max(0.001, deadline - time.monotonic()))
                candidate.connect(os.fspath(self._path))
                self._socket = candidate
                self._read_prompt(deadline)
                return
            except (MonitorError, OSError) as error:
                last_error = error
                candidate.close()
                self._socket = None
                time.sleep(
                    min(
                        HMP_INTER_KEY_DELAY_SECONDS, max(0, deadline - time.monotonic())
                    )
                )
        raise MonitorError(f"could not connect to HMP monitor: {last_error}")

    def send_key(self, key: str) -> None:
        if self._socket is None:
            raise MonitorError("HMP monitor is not connected")
        deadline = time.monotonic() + self._timeout
        try:
            self._socket.sendall(f"sendkey {key}\n".encode("ascii"))
            self._read_prompt(deadline)
            # HMP accepts commands before emulated key delivery completes; this
            # brief spacing keeps modifier and editing events in contract order.
            time.sleep(HMP_INTER_KEY_DELAY_SECONDS)
        except (OSError, TimeoutError) as error:
            raise MonitorError(f"failed to send HMP key {key}") from error

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _read_prompt(self, deadline: float) -> None:
        if self._socket is None:
            raise MonitorError("HMP monitor is not connected")
        response = b""
        while b"(qemu) " not in response:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HMP command prompt")
            self._socket.settimeout(remaining)
            chunk = self._socket.recv(4096)
            if not chunk:
                raise MonitorError("HMP monitor closed before command completion")
            response += chunk


def default_dependencies() -> GateDependencies:
    """Return the real host dependencies for a QEMU gate run."""

    return GateDependencies(
        prepare_boot_disk=prepare_boot_disk,
        launch_process=_launch_process,
        boot_console=SerialBootConsole,
        monitor=HmpMonitor,
        cleanup_process=_cleanup_process,
    )


def _run_checked_command(argv: list[str]) -> None:
    subprocess.run(
        argv,
        check=True,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )


def _launch_process(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        start_new_session=True,
    )


def _cleanup_process(process: subprocess.Popen[bytes]) -> None:
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        _reap_process(process)
        return

    if _wait_for_process_group_exit(process, process_group):
        _reap_process(process)
        return

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        _reap_process(process)
        return
    if not _wait_for_process_group_exit(process, process_group):
        _reap_process(process)
        raise RuntimeError(f"QEMU process group {process_group} survived SIGKILL")
    _reap_process(process)


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes],
    process_group: int,
) -> bool:
    deadline = time.monotonic() + PROCESS_TERM_GRACE_SECONDS
    while True:
        process.poll()
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(HMP_INTER_KEY_DELAY_SECONDS, remaining))


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ChildProcessError, subprocess.TimeoutExpired):
        process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)


def _artifact_identities(config: GateConfig, boot_disk: Path) -> dict[str, str]:
    return {
        "uboot": _sha256(config.uboot),
        "boot_disk": _sha256(boot_disk),
        "kernel": _sha256(config.kernel),
        "dtb": _sha256(config.dtb),
        "initramfs": _sha256(config.initramfs),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, contents: bytes) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as output_file:
            output_file.write(contents)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_evidence(
    output_dir: Path,
    transcript: bytes,
    result: GateRunResult,
) -> None:
    _atomic_write(output_dir / SERIAL_LOG_NAME, transcript)
    encoded_result = (
        json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(output_dir / RESULT_JSON_NAME, encoded_result)


def _parse_args(argv: list[str] | None) -> GateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--dtb", required=True, type=Path)
    parser.add_argument("--initramfs", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--smp", default=4, type=int)
    parser.add_argument("--startup-timeout", default=30.0, type=float)
    parser.add_argument("--command-timeout", default=10.0, type=float)
    parser.add_argument("--input-timeout", default=30.0, type=float)
    return GateConfig(**vars(parser.parse_args(argv)))


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return zero only for evidence-backed success."""

    try:
        result = run_gate(_parse_args(argv))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"input gate: {error}", file=os.sys.stderr)
        return 2
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
