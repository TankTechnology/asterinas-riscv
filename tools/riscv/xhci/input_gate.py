#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the bounded RISC-V PCI xHCI USB keyboard-and-mouse evidence gate."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


TOOLS_RISCV = Path(__file__).parents[1]
if os.fspath(TOOLS_RISCV) not in sys.path:
    sys.path.insert(0, os.fspath(TOOLS_RISCV))

from qemu_uboot_artifacts import (  # noqa: E402
    ArtifactExpectations,
    artifact_expectations_from_paths,
    load_artifact_manifest,
)
from qemu_uboot_commands import BootCommand as RegisteredBootCommand  # noqa: E402
from qemu_uboot_commands import boot_commands as registered_boot_commands  # noqa: E402
from qemu_uboot_profiles import GENERIC_SV39_LTP_SMP4  # noqa: E402


QEMU_CPU = "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true"
PCI_MARKER = b"PCI xHCI selected: 0000:00:01.0 1b36:000d irq-parent=9 irq=33"
USB_MARKER = b"USB boot keyboard registered: 0627:0001 bus=usb name=usb_boot_keyboard"
MOUSE_USB_MARKER = b"USB boot mouse registered: 0627:0001 bus=usb name=usb_boot_mouse"
KEYBOARD_READY_MARKER = b"XHCI_INPUT_READY kind=keyboard"
MOUSE_READY_MARKER = b"XHCI_INPUT_READY kind=mouse"
KEYBOARD_PASS_MARKER = b"XHCI_INPUT_KEYBOARD_PASS events=8"
POINTER_PASS_MARKER = b"XHCI_INPUT_POINTER_PASS events=7"
PASS_MARKER = POINTER_PASS_MARKER
KEYBOARD_EVENT_LINES = (
    b"XHCI_INPUT_EVENT source=keyboard type=1 code=30 value=1",
    b"XHCI_INPUT_EVENT source=keyboard type=0 code=0 value=0",
    b"XHCI_INPUT_EVENT source=keyboard type=1 code=30 value=0",
    b"XHCI_INPUT_EVENT source=keyboard type=0 code=0 value=0",
    b"XHCI_INPUT_EVENT source=keyboard type=1 code=2 value=1",
    b"XHCI_INPUT_EVENT source=keyboard type=0 code=0 value=0",
    b"XHCI_INPUT_EVENT source=keyboard type=1 code=2 value=0",
    b"XHCI_INPUT_EVENT source=keyboard type=0 code=0 value=0",
)
MOUSE_EVENT_LINES = (
    b"XHCI_INPUT_EVENT source=mouse type=2 code=0 value=17",
    b"XHCI_INPUT_EVENT source=mouse type=2 code=1 value=-9",
    b"XHCI_INPUT_EVENT source=mouse type=0 code=0 value=0",
    b"XHCI_INPUT_EVENT source=mouse type=1 code=272 value=1",
    b"XHCI_INPUT_EVENT source=mouse type=0 code=0 value=0",
    b"XHCI_INPUT_EVENT source=mouse type=1 code=272 value=0",
    b"XHCI_INPUT_EVENT source=mouse type=0 code=0 value=0",
)
EVENT_LINES = KEYBOARD_EVENT_LINES + MOUSE_EVENT_LINES
EVENT_TRANSCRIPT = b"".join(line + b"\n" for line in EVENT_LINES)
PANIC_MARKERS = (
    b"Kernel panic",
    b"kernel panic",
    b"BUG:",
    b"panic!",
    b"Uncaught panic",
    b"unexpected exception",
    b"Oops:",
)
FALLBACK_MARKERS = (
    b"virtio_keyboard",
    b"virtio-keyboard",
    b"VirtIO keyboard",
    b"i8042",
    b"AT keyboard",
    b"virtio_mouse",
    b"virtio-mouse",
    b"VirtIO mouse",
    b"usb-tablet",
)
KEYBOARD_READY_PATTERN = re.compile(
    rb"XHCI_INPUT_READY kind=keyboard path=/dev/input/event(?:[0-9]|[12][0-9]|3[01]) "
    rb"bustype=3 name=usb_boot_keyboard"
)
MOUSE_READY_PATTERN = re.compile(
    rb"XHCI_INPUT_READY kind=mouse path=/dev/input/event(?:[0-9]|[12][0-9]|3[01]) "
    rb"bustype=3 name=usb_boot_mouse"
)
HMP_MAX_RESPONSE_BYTES = 64 * 1024
SERIAL_MAX_BYTES = 8 * 1024 * 1024
PROCESS_TERM_GRACE_SECONDS = 2.0
HMP_INTER_KEY_DELAY_SECONDS = 0.05


class MonitorError(RuntimeError):
    """Report a bounded HMP failure."""


class EarlyProcessExit(RuntimeError):
    """Report QEMU exiting before terminal evidence."""


class GateTermination(BaseException):
    """Represent an externally requested gate termination."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"received signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class BootCommand:
    """One serial command and the evidence expected before continuing."""

    name: str
    text: str
    expected: bytes


@dataclass(frozen=True)
class GateConfig:
    """Configure one isolated xHCI input-gate run."""

    uboot: Path
    boot_disk: Path
    manifest: Path
    serial_log: Path
    result: Path
    smp: int = 4
    startup_timeout: float = 30.0
    command_timeout: float = 10.0
    input_timeout: float = 30.0


@dataclass(frozen=True)
class Classification:
    """Structured evidence extracted from one complete serial transcript."""

    passed: bool
    reason: str
    pci: dict[str, Any]
    usb: dict[str, Any]
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class GateRunResult:
    """Auditable result published only after teardown and serial drain."""

    passed: bool
    reason: str
    smp: int
    qemu_version: str
    inputs: dict[str, str]
    qemu_argv: list[str]
    pci: dict[str, Any]
    usb: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    cleanup: str
    serial_sha256: str
    manifest_artifacts: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        result = asdict(self)
        result["events"] = list(self.events)
        return result


class BootConsole(Protocol):
    transcript: bytes

    def wait_for(self, marker: bytes, timeout: float) -> None: ...

    def send_line(self, command: str) -> None: ...

    def drain(self, timeout: float) -> None: ...


class Monitor(Protocol):
    def connect(self) -> None: ...

    def send_key(self, key: str) -> None: ...

    def mouse_move(self, x: int, y: int) -> None: ...

    def mouse_button(self, buttons: int) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class GateDependencies:
    validate_artifacts: Callable[[Path, Path, tuple[int, ...]], Any]
    boot_commands: Callable[[Any], tuple[BootCommand, ...]]
    launch_process: Callable[[list[str], tuple[int, ...]], Any]
    boot_console: Callable[[Any], BootConsole]
    monitor: Callable[[Path, float], Monitor]
    cleanup_process: Callable[[Any], None]
    qemu_version: Callable[[], str]


class PinnedOutputDirectory:
    """Pin the evidence parent and provide dirfd-relative operations."""

    def __init__(self, path: Path, file_descriptor: int) -> None:
        self.path = path
        self.file_descriptor = file_descriptor
        metadata = os.fstat(file_descriptor)
        self._identity = (metadata.st_dev, metadata.st_ino)

    @classmethod
    def open(cls, path: Path) -> PinnedOutputDirectory:
        absolute = path.absolute()
        if absolute.is_symlink():
            raise ValueError(f"evidence directory must not be a symlink: {absolute}")
        absolute.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            descriptor = os.open(absolute, flags)
        except OSError as error:
            raise ValueError(
                f"cannot pin evidence directory {absolute}: {error}"
            ) from error
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o022:
            os.close(descriptor)
            raise ValueError("evidence directory must not be group/world writable")
        return cls(absolute, descriptor)

    def __enter__(self) -> PinnedOutputDirectory:
        return self

    def __exit__(self, *unused: object) -> None:
        if self.file_descriptor >= 0:
            os.close(self.file_descriptor)
            self.file_descriptor = -1

    @property
    def proc_path(self) -> Path:
        return Path(f"/proc/self/fd/{self.file_descriptor}")

    def verify_identity(self) -> None:
        pinned = os.fstat(self.file_descriptor)
        current = os.stat(self.path, follow_symlinks=False)
        if (pinned.st_dev, pinned.st_ino) != self._identity or (
            current.st_dev,
            current.st_ino,
        ) != self._identity:
            raise RuntimeError("evidence directory identity changed")

    def make_run_dir(self) -> str:
        for _ in range(100):
            name = f".xhci-input-run-{secrets.token_hex(8)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=self.file_descriptor)
                return name
            except FileExistsError:
                continue
        raise OSError("cannot create private xHCI run directory")

    def make_temp_file(self, prefix: str) -> tuple[int, str]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        for _ in range(100):
            name = f"{prefix}{secrets.token_hex(8)}"
            try:
                return os.open(name, flags, 0o600, dir_fd=self.file_descriptor), name
            except FileExistsError:
                continue
        raise OSError("cannot create temporary evidence file")


class TerminationSignalState:
    """Defer one termination signal across launch, teardown, and publication."""

    def __init__(self, handled: tuple[int, ...]) -> None:
        self.handled = handled
        self.defer_depth = 0
        self.pending: int | None = None
        self.committed = False

    def first_signal(self, signum: int, unused_frame: object) -> None:
        del unused_frame
        if self.committed:
            return
        if self.defer_depth:
            if self.pending is None:
                self.pending = signum
            return
        self._install(self.second_signal)
        raise GateTermination(signum)

    def second_signal(self, signum: int, unused_frame: object) -> None:
        del unused_frame
        os._exit(128 + signum)

    @contextlib.contextmanager
    def defer(self):
        self.defer_depth += 1
        try:
            yield
        finally:
            self.defer_depth -= 1

    def raise_if_pending(self) -> None:
        if self.pending is not None and not self.committed:
            raise GateTermination(self.pending)

    def commit(self) -> None:
        self.committed = True
        self.pending = None
        self._install(self.first_signal)

    def _install(self, handler: Callable[[int, object], None]) -> None:
        for signum in self.handled:
            signal.signal(signum, handler)


@contextlib.contextmanager
def termination_signal_handlers():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("xHCI gate signal handling requires the main thread")
    handled = (signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in handled}
    state = TerminationSignalState(handled)
    try:
        for signum in handled:
            signal.signal(signum, state.first_signal)
        yield state
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def expected_transcript() -> bytes:
    """Return the canonical current-run evidence used by unit tests."""

    return (
        PCI_MARKER
        + b"\n"
        + USB_MARKER
        + b"\n"
        + MOUSE_USB_MARKER
        + b"\n"
        + b"XHCI_INPUT_READY kind=keyboard path=/dev/input/event0 bustype=3 name=usb_boot_keyboard\n"
        + b"XHCI_INPUT_READY kind=mouse path=/dev/input/event1 bustype=3 name=usb_boot_mouse\n"
        + b"".join(line + b"\n" for line in KEYBOARD_EVENT_LINES)
        + KEYBOARD_PASS_MARKER
        + b"\n"
        + b"".join(line + b"\n" for line in MOUSE_EVENT_LINES)
        + POINTER_PASS_MARKER
        + b"\n"
    )


def qemu_argv(
    uboot: Path, boot_disk: Path, monitor_socket: Path, smp: int = 4
) -> list[str]:
    """Build the deterministic, network-isolated PCI xHCI QEMU argv."""

    if isinstance(smp, bool) or smp != 4:
        raise ValueError("the PCI xHCI gate requires SMP=4")
    for name, path in (("boot disk", boot_disk), ("monitor socket", monitor_socket)):
        if "," in os.fspath(path):
            raise ValueError(f"QEMU {name} path must not contain a comma")
    return [
        "qemu-system-riscv64",
        "-machine",
        "virt",
        "-cpu",
        QEMU_CPU,
        "-m",
        "2G",
        "-smp",
        "4",
        "-no-reboot",
        "-kernel",
        os.fspath(uboot),
        "-drive",
        f"if=none,format=raw,file={boot_disk},id=bootdisk",
        "-device",
        "virtio-blk-device,drive=bootdisk",
        "-device",
        "qemu-xhci,id=xhci,msi=off,msix=off",
        "-device",
        "usb-kbd,id=usb-kbd,bus=xhci.0",
        "-device",
        "usb-mouse,id=usb-mouse,bus=xhci.0",
        "-display",
        "none",
        "-monitor",
        f"unix:{monitor_socket},server=on,wait=off",
        "-serial",
        "stdio",
        "-nic",
        "none",
    ]


def classify_transcript(transcript: bytes) -> Classification:
    """Require one exact, ordered, current-attempt xHCI/USB/evdev chain."""

    for marker in PANIC_MARKERS:
        if marker in transcript:
            return _failed(f"panic/oops marker: {marker.decode(errors='replace')}")
    for marker in FALLBACK_MARKERS:
        if marker in transcript:
            return _failed(
                f"fallback keyboard marker: {marker.decode(errors='replace')}"
            )

    positions: list[int] = []
    for marker in (
        PCI_MARKER,
        USB_MARKER,
        MOUSE_USB_MARKER,
        KEYBOARD_READY_MARKER,
        MOUSE_READY_MARKER,
        KEYBOARD_PASS_MARKER,
        POINTER_PASS_MARKER,
    ):
        if transcript.count(marker) != 1:
            return _failed(
                f"expected exactly one marker: {marker.decode(errors='replace')}"
            )
        positions.append(transcript.find(marker))
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        return _failed("xHCI input markers are out of order")
    protocol_prefixes = (
        b"XHCI_INPUT_READY",
        b"XHCI_INPUT_EVENT",
        b"XHCI_INPUT_KEYBOARD_PASS",
        b"XHCI_INPUT_POINTER_PASS",
    )
    protocol_lines = tuple(
        line for line in transcript.splitlines() if line.startswith(protocol_prefixes)
    )
    if len(protocol_lines) < 2 or not KEYBOARD_READY_PATTERN.fullmatch(
        protocol_lines[0]
    ):
        return _failed("READY does not identify the sole BUS_USB keyboard")
    if not MOUSE_READY_PATTERN.fullmatch(protocol_lines[1]):
        return _failed("READY does not identify the sole BUS_USB relative pointer")
    expected_after_ready = (
        *KEYBOARD_EVENT_LINES,
        KEYBOARD_PASS_MARKER,
        *MOUSE_EVENT_LINES,
        POINTER_PASS_MARKER,
    )
    expected_protocol = (
        protocol_lines[0],
        protocol_lines[1],
        *expected_after_ready,
    )
    if len(protocol_lines) != len(expected_protocol):
        return _failed("unexpected, missing, or stale XHCI_INPUT protocol line")
    if protocol_lines != expected_protocol:
        return _failed("normalized evdev protocol differs from the gate contract")

    events = tuple(_parse_event(line) for line in EVENT_LINES)
    return Classification(
        passed=True,
        reason="passed",
        pci={
            "bdf": "0000:00:01.0",
            "vendor_id": "1b36",
            "device_id": "000d",
            "interrupt_parent": 9,
            "interrupt": 33,
        },
        usb={
            "keyboard": {
                "vendor_id": "0627",
                "product_id": "0001",
                "bus": "usb",
                "name": "usb_boot_keyboard",
            },
            "mouse": {
                "vendor_id": "0627",
                "product_id": "0001",
                "bus": "usb",
                "name": "usb_boot_mouse",
            },
        },
        events=events,
    )


def _failed(reason: str) -> Classification:
    return Classification(False, reason, {}, {}, ())


def _parse_event(line: bytes) -> dict[str, Any]:
    match = re.fullmatch(
        rb"XHCI_INPUT_EVENT source=(keyboard|mouse) type=(\d+) code=(\d+) value=(-?\d+)",
        line,
    )
    if match is None:
        raise ValueError("invalid normalized event")
    return {
        "source": match[1].decode("ascii"),
        "type": int(match[2]),
        "code": int(match[3]),
        "value": int(match[4]),
    }


def validate_config(config: GateConfig) -> GateConfig:
    if isinstance(config.smp, bool) or config.smp != 4:
        raise ValueError("the PCI xHCI gate requires SMP=4")
    for name in ("startup_timeout", "command_timeout", "input_timeout"):
        value = getattr(config, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be finite and positive")
    serial_log = config.serial_log.absolute()
    result = config.result.absolute()
    if serial_log == result or serial_log.parent != result.parent:
        raise ValueError(
            "serial-log and result must be distinct files in one directory"
        )
    for path in (serial_log, result):
        if path.name in ("", ".", "..") or path.exists() and path.is_dir():
            raise ValueError(f"evidence path must name a regular output file: {path}")
        if path.is_symlink():
            raise ValueError(f"evidence path must not be a symlink: {path}")
    return GateConfig(
        uboot=config.uboot.absolute(),
        boot_disk=config.boot_disk.absolute(),
        manifest=config.manifest.absolute(),
        serial_log=serial_log,
        result=result,
        smp=config.smp,
        startup_timeout=config.startup_timeout,
        command_timeout=config.command_timeout,
        input_timeout=config.input_timeout,
    )


@contextlib.contextmanager
def snapshot_inputs(config: GateConfig, output: PinnedOutputDirectory):
    run_name = output.make_run_dir()
    run_real = output.path / run_name
    run_proc = output.proc_path / run_name
    snapshots: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    try:
        for name, source in (
            ("uboot", config.uboot),
            ("boot_disk", config.boot_disk),
            ("manifest", config.manifest),
        ):
            source_fd = _open_regular_input(source)
            destination_name = f"{run_name}/{name}"
            destination_fd = os.open(
                destination_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=output.file_descriptor,
            )
            digest = hashlib.sha256()
            try:
                while chunk := os.read(source_fd, 1024 * 1024):
                    digest.update(chunk)
                    _write_all(destination_fd, chunk)
                os.fsync(destination_fd)
            finally:
                os.close(source_fd)
                os.close(destination_fd)
            snapshots[name] = run_proc / name
            hashes[name] = digest.hexdigest()
        os.chmod(run_real / "uboot", 0o500)
        output.verify_identity()
        yield snapshots, hashes, run_proc, run_real
    finally:
        for name in ("monitor.sock", "manifest", "boot_disk", "uboot"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(f"{run_name}/{name}", dir_fd=output.file_descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(run_name, dir_fd=output.file_descriptor)


def _open_regular_input(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError(f"cannot open regular input {path}: {error}") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        os.close(descriptor)
        raise ValueError(f"input must be a nonempty regular file: {path}")
    return descriptor


def _write_all(descriptor: int, contents: bytes) -> None:
    offset = 0
    while offset < len(contents):
        offset += os.write(descriptor, contents[offset:])


def run_gate(
    config: GateConfig, dependencies: GateDependencies | None = None
) -> GateRunResult:
    """Run one isolated gate and publish evidence after complete teardown."""

    config = validate_config(config)
    dependencies = dependencies or default_dependencies()
    with PinnedOutputDirectory.open(config.result.parent) as output:
        _invalidate_evidence(output, config.serial_log.name, config.result.name)
        with termination_signal_handlers() as termination:
            with snapshot_inputs(config, output) as (
                snapshots,
                input_hashes,
                run_proc,
                run_real,
            ):
                artifacts = dependencies.validate_artifacts(
                    snapshots["boot_disk"],
                    snapshots["manifest"],
                    (output.file_descriptor,),
                )
                commands = dependencies.boot_commands(artifacts)
                argv = qemu_argv(
                    snapshots["uboot"],
                    snapshots["boot_disk"],
                    run_proc / "monitor.sock",
                    config.smp,
                )
                return _run_snapshot_gate(
                    config,
                    dependencies,
                    output,
                    termination,
                    argv,
                    commands,
                    input_hashes,
                    run_proc / "monitor.sock",
                    _manifest_evidence(artifacts),
                )


def _run_snapshot_gate(
    config: GateConfig,
    dependencies: GateDependencies,
    output: PinnedOutputDirectory,
    termination: TerminationSignalState,
    argv: list[str],
    commands: tuple[BootCommand, ...],
    input_hashes: dict[str, str],
    monitor_path: Path,
    manifest_artifacts: dict[str, Any],
) -> GateRunResult:
    process: Any | None = None
    boot: BootConsole | None = None
    monitor = dependencies.monitor(monitor_path, config.command_timeout)
    reason = "orchestration failure"
    cleanup = "complete"
    timeout_reason = "timeout: U-Boot prompt"
    try:
        with termination.defer():
            process = dependencies.launch_process(argv, (output.file_descriptor,))
        termination.raise_if_pending()
        boot = dependencies.boot_console(process)
        boot.wait_for(b"=> ", config.startup_timeout)
        for command in commands:
            boot.send_line(command.text)
            timeout_reason = f"timeout: U-Boot command {command.name}"
            boot.wait_for(
                command.expected,
                config.command_timeout
                if command.name != "booti"
                else config.startup_timeout,
            )
        timeout_reason = "timeout: PCI xHCI selection"
        boot.wait_for(PCI_MARKER, config.startup_timeout)
        timeout_reason = "timeout: USB keyboard registration"
        boot.wait_for(USB_MARKER, config.startup_timeout)
        timeout_reason = "timeout: USB mouse registration"
        boot.wait_for(MOUSE_USB_MARKER, config.startup_timeout)
        timeout_reason = "timeout: guest keyboard READY"
        boot.wait_for(KEYBOARD_READY_MARKER, config.startup_timeout)
        timeout_reason = "timeout: guest mouse READY"
        boot.wait_for(MOUSE_READY_MARKER, config.startup_timeout)
        monitor.connect()
        monitor.send_key("a")
        monitor.send_key("1")
        timeout_reason = "timeout: guest keyboard PASS"
        boot.wait_for(KEYBOARD_PASS_MARKER, config.input_timeout)
        monitor.mouse_move(17, -9)
        monitor.mouse_button(1)
        monitor.mouse_button(0)
        timeout_reason = "timeout: guest pointer PASS"
        boot.wait_for(POINTER_PASS_MARKER, config.input_timeout)
        reason = "passed"
    except TimeoutError:
        reason = timeout_reason
    except MonitorError:
        reason = "monitor failure"
    except (EarlyProcessExit, BrokenPipeError, EOFError):
        reason = "early process exit"
    except Exception as error:
        reason = f"orchestration failure: {type(error).__name__}"
    finally:
        failures: list[str] = []
        with termination.defer():
            try:
                monitor.close()
            except Exception:
                failures.append("monitor close failure")
            if process is not None:
                try:
                    dependencies.cleanup_process(process)
                except Exception:
                    failures.append("cleanup failure")
            if boot is not None:
                try:
                    boot.drain(config.command_timeout)
                except TimeoutError:
                    failures.append("serial drain timeout")
                except Exception:
                    failures.append("serial drain failure")
        if failures:
            cleanup = "; ".join(failures)
            reason = cleanup
    termination.raise_if_pending()

    transcript = boot.transcript if boot is not None else b""
    classification = classify_transcript(transcript)
    if reason == "passed" and not classification.passed:
        reason = classification.reason
    passed = reason == "passed" and classification.passed and cleanup == "complete"
    result = GateRunResult(
        passed=passed,
        reason=reason,
        smp=config.smp,
        qemu_version=dependencies.qemu_version(),
        inputs=input_hashes,
        qemu_argv=argv,
        pci=classification.pci,
        usb=classification.usb,
        events=classification.events,
        cleanup=cleanup,
        serial_sha256=hashlib.sha256(transcript).hexdigest(),
        manifest_artifacts=manifest_artifacts,
    )
    _publish_evidence(output, config, transcript, result, termination)
    return result


class SerialBootConsole:
    """Drive U-Boot and the guest through bounded QEMU serial pipes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.search_offset = 0
        self.transcript = b""

    def wait_for(self, marker: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            found = self.transcript.find(marker, self.search_offset)
            if found >= 0:
                self.search_offset = found + len(marker)
                return
            self._read_once(deadline, marker)

    def send_line(self, command: str) -> None:
        if self.process.stdin is None:
            raise EarlyProcessExit("QEMU serial stdin is unavailable")
        try:
            self.process.stdin.write(command.encode("ascii") + b"\n")
            self.process.stdin.flush()
        except BrokenPipeError as error:
            raise EarlyProcessExit("QEMU closed serial input") from error

    def drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self.process.stdout is None:
                raise OSError("QEMU serial stdout is unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("serial drain")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("serial drain")
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                return
            self._append(chunk)

    def _read_once(self, deadline: float, marker: bytes) -> None:
        if self.process.stdout is None:
            raise EarlyProcessExit("QEMU serial stdout is unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(marker.decode(errors="replace"))
        readable, _, _ = select.select([self.process.stdout], [], [], remaining)
        if not readable:
            raise TimeoutError(marker.decode(errors="replace"))
        chunk = os.read(self.process.stdout.fileno(), 4096)
        if not chunk:
            raise EarlyProcessExit(f"QEMU exited with status {self.process.poll()}")
        self._append(chunk)

    def _append(self, chunk: bytes) -> None:
        if len(self.transcript) + len(chunk) > SERIAL_MAX_BYTES:
            raise OSError("serial transcript byte limit exceeded")
        self.transcript += chunk


class HmpMonitor:
    """Send reviewed HMP keyboard and pointer commands with bounded responses."""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.socket: socket.socket | None = None

    def connect(self) -> None:
        deadline = time.monotonic() + self.timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                candidate.settimeout(max(0.001, deadline - time.monotonic()))
                candidate.connect(os.fspath(self.path))
                self.socket = candidate
                self._read_prompt(deadline)
                return
            except (OSError, MonitorError, TimeoutError) as error:
                last_error = error
                candidate.close()
                self.socket = None
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        raise MonitorError(f"could not connect to HMP: {last_error}")

    def send_key(self, key: str) -> None:
        if key not in ("a", "1") or self.socket is None:
            raise MonitorError("unregistered HMP key or disconnected monitor")
        deadline = time.monotonic() + self.timeout
        try:
            self._send_command(f"sendkey {key}", deadline)
            remaining = deadline - time.monotonic()
            if remaining < HMP_INTER_KEY_DELAY_SECONDS:
                raise TimeoutError("HMP key delay exceeds command deadline")
            time.sleep(HMP_INTER_KEY_DELAY_SECONDS)
            if time.monotonic() > deadline:
                raise TimeoutError("HMP key delay exceeds command deadline")
        except (OSError, TimeoutError) as error:
            raise MonitorError(f"failed to send key {key}: {error}") from error

    def mouse_move(self, x: int, y: int) -> None:
        if not all(
            isinstance(value, int) and not isinstance(value, bool) for value in (x, y)
        ):
            raise MonitorError("mouse displacement must be an integer")
        if not all(-(1 << 15) <= value < (1 << 15) for value in (x, y)):
            raise MonitorError("mouse displacement is outside the registered range")
        self._run_pointer_command(f"mouse_move {x} {y}")

    def mouse_button(self, buttons: int) -> None:
        if (
            isinstance(buttons, bool)
            or not isinstance(buttons, int)
            or not 0 <= buttons <= 7
        ):
            raise MonitorError("mouse button mask is outside the registered range")
        self._run_pointer_command(f"mouse_button {buttons}")

    def _run_pointer_command(self, command: str) -> None:
        if self.socket is None:
            raise MonitorError("disconnected HMP monitor")
        deadline = time.monotonic() + self.timeout
        try:
            self._send_command(command, deadline)
        except (OSError, TimeoutError) as error:
            raise MonitorError(
                f"failed HMP pointer command {command}: {error}"
            ) from error

    def _send_command(self, command: str, deadline: float) -> None:
        if self.socket is None:
            raise MonitorError("HMP monitor is disconnected")
        self.socket.sendall(command.encode("ascii") + b"\n")
        self._read_prompt(deadline)

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _read_prompt(self, deadline: float) -> None:
        if self.socket is None:
            raise MonitorError("HMP monitor is disconnected")
        response = b""
        while b"(qemu) " not in response:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HMP prompt")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(4096)
            if not chunk:
                raise MonitorError("HMP closed before command completion")
            if len(response) + len(chunk) > HMP_MAX_RESPONSE_BYTES:
                raise MonitorError("HMP response byte limit exceeded")
            response += chunk


def default_dependencies() -> GateDependencies:
    return GateDependencies(
        validate_artifacts=_validate_artifacts,
        boot_commands=_registered_commands,
        launch_process=_launch_process,
        boot_console=SerialBootConsole,
        monitor=HmpMonitor,
        cleanup_process=_cleanup_process,
        qemu_version=_qemu_version,
    )


def _validate_artifacts(
    boot_disk: Path, manifest: Path, pass_fds: tuple[int, ...]
) -> ArtifactExpectations:
    expected = load_artifact_manifest(manifest)
    with tempfile.TemporaryDirectory(prefix="asterinas-xhci-artifacts-") as temporary:
        directory = Path(temporary)
        payloads = {
            "kernel": directory / "asterinas.booti",
            "dtb": directory / GENERIC_SV39_LTP_SMP4.machine.dtb_filename,
            "initrd": directory / "initramfs.cpio.gz",
        }
        for source, destination in (
            ("asterinas.booti", payloads["kernel"]),
            (GENERIC_SV39_LTP_SMP4.machine.dtb_filename, payloads["dtb"]),
            ("initramfs.cpio.gz", payloads["initrd"]),
        ):
            subprocess.run(
                [
                    "debugfs",
                    "-R",
                    f"dump -p /{source} {destination}",
                    os.fspath(boot_disk),
                ],
                check=True,
                capture_output=True,
                text=True,
                pass_fds=pass_fds,
            )
        actual = artifact_expectations_from_paths(
            kernel=payloads["kernel"], dtb=payloads["dtb"], initrd=payloads["initrd"]
        )
        _validate_dtb_cpu_count(payloads["dtb"])
    if actual != expected:
        raise ValueError("boot disk payloads do not match the artifact manifest")
    return expected


def _validate_dtb_cpu_count(dtb: Path) -> None:
    """Require the payload DTB to describe the four launched harts."""

    result = subprocess.run(
        ["fdtget", "-l", os.fspath(dtb), "/cpus"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    cpu_nodes = tuple(
        node for node in result.stdout.splitlines() if node.startswith("cpu@")
    )
    enabled = 0
    for node in cpu_nodes:
        status = subprocess.run(
            ["fdtget", "-t", "s", os.fspath(dtb), f"/cpus/{node}", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if status.returncode != 0 or status.stdout.strip() in ("okay", "ok"):
            enabled += 1
    if enabled != 4:
        raise ValueError(
            f"payload DTB must describe exactly 4 enabled CPU nodes, got {enabled}"
        )


def _registered_commands(artifacts: ArtifactExpectations) -> tuple[BootCommand, ...]:
    commands: tuple[RegisteredBootCommand, ...] = registered_boot_commands(
        artifacts,
        profile=GENERIC_SV39_LTP_SMP4,
        bootargs_override="console=ttyS0 loglevel=info init=/init",
    )
    return tuple(
        BootCommand(command.name, command.text, command.expected_output.encode())
        for command in commands
    )


def _launch_process(
    argv: list[str], pass_fds: tuple[int, ...]
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
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGKILL)
    if not _wait_for_process_group_exit(process, process_group):
        _reap_process(process)
        raise RuntimeError(f"QEMU process group {process_group} survived SIGKILL")
    _reap_process(process)


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes], process_group: int
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
        time.sleep(min(0.05, remaining))


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ChildProcessError, subprocess.TimeoutExpired):
        process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)


def _qemu_version() -> str:
    result = subprocess.run(
        ["qemu-system-riscv64", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    line = result.stdout.splitlines()[0] if result.stdout else ""
    if not line or len(line) > 512:
        raise ValueError("QEMU version output is missing or too long")
    return line


def _manifest_evidence(artifacts: Any) -> dict[str, Any]:
    if isinstance(artifacts, ArtifactExpectations):
        return asdict(artifacts)
    return dict(artifacts) if isinstance(artifacts, dict) else {}


def _invalidate_evidence(
    output: PinnedOutputDirectory, serial_name: str, result_name: str
) -> None:
    output.verify_identity()
    for name in (result_name, serial_name):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=output.file_descriptor)


def _atomic_write(output: PinnedOutputDirectory, name: str, contents: bytes) -> None:
    descriptor, temporary = output.make_temp_file(f".{name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        output.verify_identity()
        os.replace(
            temporary,
            name,
            src_dir_fd=output.file_descriptor,
            dst_dir_fd=output.file_descriptor,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=output.file_descriptor)


def _publish_evidence(
    output: PinnedOutputDirectory,
    config: GateConfig,
    transcript: bytes,
    result: GateRunResult,
    termination: TerminationSignalState,
) -> None:
    with termination.defer():
        _atomic_write(output, config.serial_log.name, transcript)
        encoded = (
            json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n"
        ).encode()
        _atomic_write(output, config.result.name, encoded)
        termination.commit()


def _parse_args(argv: list[str] | None) -> GateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--serial-log", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--smp", default=4, type=int)
    parser.add_argument("--startup-timeout", default=30.0, type=float)
    parser.add_argument("--command-timeout", default=10.0, type=float)
    parser.add_argument("--input-timeout", default=30.0, type=float)
    return GateConfig(**vars(parser.parse_args(argv)))


def main(argv: list[str] | None = None) -> int:
    try:
        result = run_gate(_parse_args(argv))
    except GateTermination as error:
        return 128 + error.signum
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"xHCI input gate: {error}", file=sys.stderr)
        return 2
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
