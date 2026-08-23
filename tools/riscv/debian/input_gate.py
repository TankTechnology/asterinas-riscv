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
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


READY_MARKER = b"__DEBIAN_INPUT_GATE_READY__"
PASS_MARKER = b"__DEBIAN_INPUT_GATE_PASS__"
KEY_SEQUENCE = ("a", "shift-b", "backspace", "ctrl-c")
PANIC_MARKERS = (
    b"Kernel panic",
    b"kernel panic",
    b"BUG:",
    b"panic!",
    b"Uncaught panic",
    b"unexpected exception",
)
KERNEL_START_MARKER = b"Starting kernel"
BOOT_DISK_NAME = "boot.ext4"
MONITOR_SOCKET_NAME = "monitor.sock"
SERIAL_LOG_NAME = "serial.log"
RESULT_JSON_NAME = "result.json"
MINIMUM_DISK_SIZE_KIB = 64 * 1024
DISK_OVERHEAD_KIB = 16 * 1024
HMP_INTER_KEY_DELAY_SECONDS = 0.05
HMP_MAX_RESPONSE_BYTES = 64 * 1024
PROCESS_TERM_GRACE_SECONDS = 2.0
QEMU_CPU = "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true"

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


class GateTermination(BaseException):
    """Interrupt a gate run after an external termination signal."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"received signal {signum}")
        self.signum = signum


class PinnedOutputDirectory:
    """Hold and verify one private output directory by file descriptor."""

    def __init__(self, path: Path, file_descriptor: int) -> None:
        self.path = path
        self.file_descriptor = file_descriptor
        metadata = os.fstat(file_descriptor)
        self._identity = (metadata.st_dev, metadata.st_ino)

    @classmethod
    def open(cls, path: Path) -> PinnedOutputDirectory:
        absolute_path = Path(path).absolute()
        if "," in os.fspath(absolute_path):
            raise ValueError("output directory path must not contain a comma")
        if absolute_path.is_symlink():
            raise ValueError(
                f"output directory must not be a symbolic link: {absolute_path}"
            )
        created = not absolute_path.exists()
        if created:
            absolute_path.mkdir(mode=0o700, parents=True)

        flags = os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            file_descriptor = os.open(absolute_path, flags)
        except OSError as error:
            raise ValueError(
                f"cannot pin output directory {absolute_path}: {error}"
            ) from error
        if created:
            os.chmod(f"/proc/self/fd/{file_descriptor}", 0o700)
        metadata = os.fstat(file_descriptor)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.close(file_descriptor)
            raise ValueError(
                f"output directory must have exact mode 0700: {absolute_path}"
            )
        return cls(absolute_path, file_descriptor)

    def __enter__(self) -> PinnedOutputDirectory:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()

    @property
    def proc_path(self) -> Path:
        return Path(f"/proc/self/fd/{self.file_descriptor}")

    def path_for(self, relative_path: str) -> Path:
        return self.proc_path / relative_path

    def verify_identity(self) -> None:
        pinned = os.fstat(self.file_descriptor)
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as error:
            raise RuntimeError("output directory identity changed") from error
        identities = (pinned.st_dev, pinned.st_ino), (current.st_dev, current.st_ino)
        if identities[0] != self._identity or identities[1] != self._identity:
            raise RuntimeError("output directory identity changed")

    def make_temp_dir(self, prefix: str) -> str:
        for _attempt in range(100):
            name = f"{prefix}{secrets.token_hex(8)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=self.file_descriptor)
                return name
            except FileExistsError:
                continue
        raise OSError(f"cannot create temporary directory with prefix {prefix}")

    def make_temp_file(self, prefix: str) -> tuple[int, str]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        for _attempt in range(100):
            name = f"{prefix}{secrets.token_hex(8)}"
            try:
                return os.open(name, flags, 0o600, dir_fd=self.file_descriptor), name
            except FileExistsError:
                continue
        raise OSError(f"cannot create temporary file with prefix {prefix}")

    def close(self) -> None:
        if self.file_descriptor >= 0:
            os.close(self.file_descriptor)
            self.file_descriptor = -1


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

    prepare_boot_disk: Callable[[GateConfig, Path, PinnedOutputDirectory], None]
    launch_process: Callable[[list[str], tuple[int, ...]], Any]
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
        "-cpu",
        QEMU_CPU,
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


def validate_config(
    config: GateConfig,
    *,
    pinned_output: PinnedOutputDirectory | None = None,
) -> GateConfig:
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

    if pinned_output is None:
        output_dir = _validate_output_dir(config.output_dir)
    else:
        pinned_output.verify_identity()
        output_dir = pinned_output.path

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


def _validate_output_dir(path: Path) -> Path:
    with PinnedOutputDirectory.open(path) as output:
        output.verify_identity()
        return output.path


def validate_dtb_cpu_count(
    dtb: Path,
    smp: int,
    pass_fds: tuple[int, ...] = (),
) -> None:
    """Require the DTB's enabled CPU-node count to match QEMU SMP."""

    try:
        child_result = subprocess.run(
            ["fdtget", "-l", str(dtb), "/cpus"],
            check=True,
            capture_output=True,
            pass_fds=pass_fds,
            text=True,
            timeout=10.0,
        )
        enabled_cpu_count = 0
        for child in child_result.stdout.splitlines():
            node = f"/cpus/{child.strip()}"
            device_type = _fdt_string(dtb, node, "device_type", "", pass_fds)
            if device_type != "cpu":
                continue
            status = _fdt_string(dtb, node, "status", "okay", pass_fds)
            if status == "disabled":
                continue
            if status not in {"ok", "okay"}:
                raise ValueError(f"DTB CPU node {node} has invalid status {status!r}")
            enabled_cpu_count += 1
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"cannot inspect DTB CPU nodes: {error}") from error

    if enabled_cpu_count != smp:
        raise ValueError(
            f"DTB enabled CPU count {enabled_cpu_count} does not match SMP {smp}"
        )


def _fdt_string(
    dtb: Path,
    node: str,
    property_name: str,
    default: str,
    pass_fds: tuple[int, ...],
) -> str:
    result = subprocess.run(
        [
            "fdtget",
            "-t",
            "s",
            "-d",
            default,
            str(dtb),
            node,
            property_name,
        ],
        check=True,
        capture_output=True,
        pass_fds=pass_fds,
        text=True,
        timeout=10.0,
    )
    return result.stdout.strip()


@contextlib.contextmanager
def _snapshot_artifacts(
    config: GateConfig,
    output: PinnedOutputDirectory,
):
    snapshot_dir = output.make_temp_dir(".artifacts.")
    names = ("kernel", "uboot", "dtb", "initramfs")
    try:
        snapshots = {}
        for name in names:
            relative_path = f"{snapshot_dir}/{name}"
            _snapshot_regular_file(getattr(config, name), output, relative_path, name)
            snapshots[name] = output.path_for(relative_path)
        yield GateConfig(
            **snapshots,
            output_dir=output.path,
            smp=config.smp,
            startup_timeout=config.startup_timeout,
            command_timeout=config.command_timeout,
            input_timeout=config.input_timeout,
        )
    finally:
        for name in names:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(
                    f"{snapshot_dir}/{name}",
                    dir_fd=output.file_descriptor,
                )
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(snapshot_dir, dir_fd=output.file_descriptor)


def _snapshot_regular_file(
    source: Path,
    output: PinnedOutputDirectory,
    destination: str,
    name: str,
) -> None:
    source_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise ValueError(f"{name} must be a nonempty regular file: {source}")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=output.file_descriptor,
        )
        os.fchmod(destination_fd, 0o600)
        with (
            os.fdopen(source_fd, "rb", closefd=False) as input_file,
            os.fdopen(destination_fd, "wb") as output_file,
        ):
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
    finally:
        os.close(source_fd)


def _invalidate_evidence(output: PinnedOutputDirectory) -> None:
    # Removing the result first ensures no interruption can preserve passed=true.
    for name in (RESULT_JSON_NAME, SERIAL_LOG_NAME):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=output.file_descriptor)


def prepare_boot_disk(
    config: GateConfig,
    boot_disk: Path,
    output: PinnedOutputDirectory | None = None,
    *,
    run_command: Callable[[list[str]], None] | None = None,
) -> None:
    """Atomically build a private ext4 disk containing the three boot files."""

    if output is None:
        with PinnedOutputDirectory.open(config.output_dir) as pinned_output:
            _prepare_boot_disk_pinned(config, boot_disk, pinned_output, run_command)
        return
    _prepare_boot_disk_pinned(config, boot_disk, output, run_command)


def _prepare_boot_disk_pinned(
    config: GateConfig,
    boot_disk: Path,
    output: PinnedOutputDirectory,
    run_command: Callable[[list[str]], None] | None,
) -> None:
    output.verify_identity()
    if run_command is None:

        def run_command(argv: list[str]) -> None:
            _run_checked_command(argv, (output.file_descriptor,))

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

    stage = output.make_temp_dir(f".{BOOT_DISK_NAME}.stage.")
    temporary_disk: str | None = None
    try:
        for source, destination_name in payloads:
            _snapshot_regular_file(
                source,
                output,
                f"{stage}/{destination_name}",
                destination_name,
            )
        file_descriptor, temporary_disk = output.make_temp_file(f".{BOOT_DISK_NAME}.")
        os.close(file_descriptor)
        temporary_path = output.path_for(temporary_disk)
        run_command(
            [
                "mkfs.ext4",
                "-q",
                "-F",
                "-d",
                str(output.path_for(stage)),
                str(temporary_path),
                str(disk_size_kib),
            ]
        )
        output.verify_identity()
        run_command(
            [
                "debugfs",
                "-w",
                "-R",
                "rmdir lost+found",
                str(temporary_path),
            ]
        )
        output.verify_identity()
        os.replace(
            temporary_disk,
            boot_disk.name,
            src_dir_fd=output.file_descriptor,
            dst_dir_fd=output.file_descriptor,
        )
        temporary_disk = None
    finally:
        if temporary_disk is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_disk, dir_fd=output.file_descriptor)
        for _source, destination_name in payloads:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(
                    f"{stage}/{destination_name}",
                    dir_fd=output.file_descriptor,
                )
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(stage, dir_fd=output.file_descriptor)


@contextlib.contextmanager
def _termination_signal_handlers():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("input gate signal handling requires the main thread")

    handled_signals = (signal.SIGTERM, signal.SIGHUP)
    previous_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}

    def second_signal(signum: int, unused_frame: object) -> None:
        del unused_frame
        os._exit(128 + signum)

    def first_signal(signum: int, unused_frame: object) -> None:
        del unused_frame
        for handled_signal in handled_signals:
            signal.signal(handled_signal, second_signal)
        raise GateTermination(signum)

    try:
        for signum in handled_signals:
            signal.signal(signum, first_signal)
        yield
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


@contextlib.contextmanager
def _block_termination_signals():
    handled_signals = {signal.SIGTERM, signal.SIGHUP}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def run_gate(
    config: GateConfig,
    dependencies: GateDependencies | None = None,
) -> GateRunResult:
    """Run the QEMU input gate and atomically persist its evidence."""

    with PinnedOutputDirectory.open(config.output_dir) as output:
        _invalidate_evidence(output)
        config = validate_config(config, pinned_output=output)
        dependencies = dependencies or default_dependencies()
        with _termination_signal_handlers():
            with _snapshot_artifacts(config, output) as snapshot:
                output.verify_identity()
                validate_dtb_cpu_count(
                    snapshot.dtb,
                    snapshot.smp,
                    (output.file_descriptor,),
                )
                output.verify_identity()
                return _run_snapshot_gate(snapshot, dependencies, output)


def _run_snapshot_gate(
    config: GateConfig,
    dependencies: GateDependencies,
    output: PinnedOutputDirectory,
) -> GateRunResult:
    boot_disk = output.path_for(BOOT_DISK_NAME)
    monitor_socket = output.path_for(MONITOR_SOCKET_NAME)
    dependencies.prepare_boot_disk(config, boot_disk, output)
    output.verify_identity()
    argv = qemu_argv(config.uboot, boot_disk, monitor_socket, config.smp)
    identities = _artifact_identities(config, boot_disk)

    process: Any | None = None
    boot: BootConsole | None = None
    monitor = dependencies.monitor(monitor_socket, config.command_timeout)
    terminal_reason = "orchestration failure"
    timeout_reason = "timeout: U-Boot prompt"
    lifecycle_failures: list[str] = []
    try:
        output.verify_identity()
        with _block_termination_signals():
            process = dependencies.launch_process(argv, (output.file_descriptor,))
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
    _write_evidence(output, transcript, result)
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
            remaining = deadline - time.monotonic()
            if remaining < HMP_INTER_KEY_DELAY_SECONDS:
                raise TimeoutError("HMP inter-key delay exceeds command deadline")
            time.sleep(HMP_INTER_KEY_DELAY_SECONDS)
            if time.monotonic() > deadline:
                raise TimeoutError("HMP inter-key delay exceeds command deadline")
        except (OSError, TimeoutError) as error:
            raise MonitorError(f"failed to send HMP key {key}: {error}") from error

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
            if len(response) + len(chunk) > HMP_MAX_RESPONSE_BYTES:
                raise MonitorError("HMP response byte limit exceeded")
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


def _run_checked_command(
    argv: list[str],
    pass_fds: tuple[int, ...] = (),
) -> None:
    subprocess.run(
        argv,
        check=True,
        pass_fds=pass_fds,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )


def _launch_process(
    argv: list[str],
    pass_fds: tuple[int, ...],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        pass_fds=pass_fds,
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


def _atomic_write(
    output: PinnedOutputDirectory,
    name: str,
    contents: bytes,
) -> None:
    file_descriptor, temporary_name = output.make_temp_file(f".{name}.")
    try:
        with os.fdopen(file_descriptor, "wb") as output_file:
            output_file.write(contents)
            output_file.flush()
            os.fsync(output_file.fileno())
        output.verify_identity()
        os.replace(
            temporary_name,
            name,
            src_dir_fd=output.file_descriptor,
            dst_dir_fd=output.file_descriptor,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=output.file_descriptor)


def _write_evidence(
    output: PinnedOutputDirectory,
    transcript: bytes,
    result: GateRunResult,
) -> None:
    output.verify_identity()
    _atomic_write(output, SERIAL_LOG_NAME, transcript)
    encoded_result = (
        json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(output, RESULT_JSON_NAME, encoded_result)


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
    except GateTermination as error:
        return 128 + error.signum
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"input gate: {error}", file=os.sys.stderr)
        return 2
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
