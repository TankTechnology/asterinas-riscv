"""Bounded subprocess and serial-stream control for QEMU U-Boot boots."""

from __future__ import annotations

import ctypes
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Sequence

from qemu_uboot_commands import BootCommand, USERSPACE_MARKER
from qemu_uboot_profiles import BootMilestone, MilestoneExpectation
from qemu_process_cleanup import (
    _DeferredTermination,
    _defer_termination_until_cleanup,
    _TerminationRequested as _TerminationRequested,
)
from qemu_uboot_secure_io import PinnedOutputDirectory


_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
SERIAL_OUTPUT_LIMIT = 4 * 1024 * 1024
# A final quiet-window drain for bytes buffered while capture runs, not a QMP timeout.
TERMINAL_ACTION_DRAIN_TIMEOUT_SECONDS = 0.01
# QMP capture uses a five-second request bound; retain controller headroom.
TERMINAL_ACTION_TIMEOUT_SECONDS = 6.0


class SerialOutputLimitExceeded(RuntimeError):
    """The bounded serial evidence budget was exhausted."""


@dataclass(frozen=True)
class MilestoneEvent:
    """One monotonic boot milestone and its time since process launch."""

    stage: BootMilestone
    elapsed_seconds: float


class MilestoneTracker:
    """Recognize one immutable marker sequence without skipping a stage."""

    def __init__(self, expectations: Sequence[MilestoneExpectation]) -> None:
        self._expectations = tuple(expectations)
        self._events: list[MilestoneEvent] = []
        self._buffer = bytearray()
        self._search_start = 0
        self._current_occurrences = 0

    @property
    def events(self) -> tuple[MilestoneEvent, ...]:
        return tuple(self._events)

    @property
    def last_stage(self) -> BootMilestone | None:
        return self._events[-1].stage if self._events else None

    def observe(self, chunk: bytes, *, elapsed_seconds: float) -> None:
        """Consume serial bytes and record every newly completed stage."""

        if elapsed_seconds < 0 or (
            self._events and elapsed_seconds < self._events[-1].elapsed_seconds
        ):
            raise ValueError("milestone elapsed time must be monotonic")
        self._buffer.extend(chunk)
        while len(self._events) < len(self._expectations):
            remaining = self._expectations[len(self._events) :]
            positions = tuple(
                self._buffer.find(item.line, self._search_start) for item in remaining
            )
            present = tuple(
                (position, index)
                for index, position in enumerate(positions)
                if position >= 0
            )
            if not present:
                return
            position, relative_index = min(present)
            if relative_index != 0:
                unexpected = remaining[relative_index].stage.value
                expected = remaining[0].stage.value
                raise ValueError(
                    f"out-of-order milestone {unexpected}; expected {expected}"
                )
            expectation = remaining[0]
            self._search_start = position + len(expectation.line)
            self._current_occurrences += 1
            if self._current_occurrences < expectation.expected_occurrences:
                continue
            self._events.append(MilestoneEvent(expectation.stage, elapsed_seconds))
            self._current_occurrences = 0


def _validate_session_timeouts(
    *,
    startup_timeout: float,
    command_timeout: float,
    boot_timeout: float,
    termination_grace: float,
    post_terminal_timeout: float,
    reboot_expectation: RebootExpectation | None,
) -> None:
    for name, value in (
        ("startup_timeout", startup_timeout),
        ("command_timeout", command_timeout),
        ("boot_timeout", boot_timeout),
        ("termination_grace", termination_grace),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if not math.isfinite(post_terminal_timeout) or post_terminal_timeout < 0:
        raise ValueError("post_terminal_timeout must be finite and non-negative")
    if reboot_expectation is not None and (
        not math.isfinite(reboot_expectation.recovery_timeout)
        or reboot_expectation.recovery_timeout <= 0
    ):
        raise ValueError("recovery_timeout must be finite and positive")


def append_bounded_serial_chunk(
    *,
    raw_log: BinaryIO,
    transcript: bytearray,
    chunk: bytes,
) -> None:
    """Append one chunk while keeping both memory and disk evidence bounded."""

    remaining = SERIAL_OUTPUT_LIMIT - len(transcript)
    accepted = chunk[: max(remaining, 0)]
    if accepted:
        raw_log.write(accepted)
        raw_log.flush()
        transcript.extend(accepted)
    if len(accepted) != len(chunk):
        raise SerialOutputLimitExceeded(
            f"serial output limit {SERIAL_OUTPUT_LIMIT} bytes exceeded"
        )


@dataclass(frozen=True)
class RebootExpectation:
    """Post-trigger firmware milestones that prove a guest-requested reset."""

    trigger_marker: bytes
    recovery_timeout: float
    milestones: tuple[tuple[str, bytes], ...] = (
        ("opensbi", b"OpenSBI v"),
        ("uboot", b"U-Boot 2026.07"),
        ("prompt", b"\n=> "),
    )


@dataclass(frozen=True)
class SerialInputStep:
    """One input write guarded by optional preceding serial output."""

    input_bytes: bytes
    ready_line: bytes | None = None
    ready_token: bytes | None = None

    def __post_init__(self) -> None:
        if not self.input_bytes:
            raise ValueError("input_bytes must not be empty")
        if self.ready_line is not None and self.ready_token is not None:
            raise ValueError("a serial input step has more than one readiness guard")
        if self.ready_line is not None and (
            not self.ready_line
            or b"\n" in self.ready_line
            or b"\r" in self.ready_line
        ):
            raise ValueError("ready_line must be one non-empty unterminated line")
        if self.ready_token is not None and not self.ready_token:
            raise ValueError("ready_token must not be empty")


@dataclass(frozen=True)
class SerialInteraction:
    """One guarded post-boot sequence of serial input/output exchanges."""

    ready_line: bytes
    input_steps: tuple[SerialInputStep, ...]
    completion_line: bytes

    def __post_init__(self) -> None:
        for name, line in (
            ("ready_line", self.ready_line),
            ("completion_line", self.completion_line),
        ):
            if not line or b"\n" in line or b"\r" in line:
                raise ValueError(f"{name} must be one non-empty unterminated line")
        if not self.input_steps:
            raise ValueError("input_steps must not be empty")


@dataclass(frozen=True)
class SessionResult:
    """Outcome and cleanup state for one QEMU serial session."""

    marker_seen: bool
    booti_sent_count: int
    timed_out: bool
    killed: bool
    cleanup_complete: bool
    returncode: int | None
    failure: str | None
    termination_action: str
    recovery_complete: bool = False
    recovery_elapsed_seconds: float | None = None
    boot_to_recovery_elapsed_seconds: float | None = None
    milestones: tuple[MilestoneEvent, ...] = ()


@contextmanager
def _adopt_orphaned_descendants():
    """Make orphaned QEMU descendants reapable for the session lifetime."""

    if sys.platform != "linux":
        yield
        return

    libc = ctypes.CDLL(None, use_errno=True)
    state = ctypes.c_int()
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(state), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    changed = state.value == 0
    if changed and libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    try:
        yield
    finally:
        if changed and libc.prctl(_PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _reap_process_group(process: subprocess.Popen[bytes], process_group: int) -> None:
    """Reap any exited descendants adopted from one process group."""

    while True:
        try:
            child, status = os.waitpid(-process_group, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            return
        if child == 0:
            return
        if child == process.pid:
            process.returncode = os.waitstatus_to_exitcode(status)


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes],
    process_group: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        process.poll()
        _reap_process_group(process, process_group)
        if not _process_group_exists(process_group):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 0.01))


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, grace: float
) -> tuple[bool, bool, bool]:
    """Return SIGTERM/SIGKILL provenance and whether the group disappeared."""

    process_group = process.pid
    process.poll()
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return False, False, True
    if _wait_for_process_group_exit(process, process_group, grace):
        return True, False, True
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True, False, True
    cleaned = _wait_for_process_group_exit(process, process_group, grace)
    return True, True, cleaned


def _force_kill_process_group(
    process: subprocess.Popen[bytes], *, grace: float
) -> tuple[bool, bool]:
    """Best-effort fallback when normal process-group termination raises."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False, True
    except OSError:
        return False, False
    try:
        return True, _wait_for_process_group_exit(process, process.pid, grace)
    except OSError:
        return True, False


@dataclass
class _SerialProtocol:
    """Advance one guarded U-Boot command and reboot-recovery epoch."""

    process: subprocess.Popen[bytes]
    selector: selectors.BaseSelector
    raw_log: BinaryIO
    commands: Sequence[BootCommand]
    startup_timeout: float
    command_timeout: float
    boot_timeout: float
    command_observer: Callable[[BootCommand, str], None] | None
    reboot_expectation: RebootExpectation | None
    serial_interaction: SerialInteraction | None
    completion_line: bytes
    allow_completion_token: bool
    termination_checkpoint: Callable[[], None]
    post_terminal_timeout: float = 0.0
    milestone_expectations: Sequence[MilestoneExpectation] = ()
    stage: str = "startup"
    marker_seen: bool = False
    booti_sent_count: int = 0
    timed_out: bool = False
    recovery_complete: bool = False
    recovery_elapsed_seconds: float | None = None
    boot_to_recovery_elapsed_seconds: float | None = None
    failure: str | None = None
    _pending: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _boot_line_buffer: bytearray = field(
        default_factory=bytearray,
        init=False,
        repr=False,
    )
    _transcript: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _started_at: float = field(default_factory=time.monotonic, init=False, repr=False)
    _milestone_tracker: MilestoneTracker = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._milestone_tracker = MilestoneTracker(self.milestone_expectations)

    def _read_serial_chunk(
        self,
        *,
        deadline: float,
        needle: bytes,
    ) -> bytes | None:
        self.termination_checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {needle!r}")
        events = self.selector.select(min(remaining, 0.1))
        self.termination_checkpoint()
        if not events:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"serial process exited with {self.process.returncode} "
                    f"while waiting for {needle!r}"
                )
            return None
        assert self.process.stdout is not None
        chunk = os.read(self.process.stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError(
                f"serial process closed output while waiting for {needle!r}"
            )
        append_bounded_serial_chunk(
            raw_log=self.raw_log,
            transcript=self._transcript,
            chunk=chunk,
        )
        self._milestone_tracker.observe(
            chunk,
            elapsed_seconds=time.monotonic() - self._started_at,
        )
        return chunk

    def _read_until(self, needle: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while needle not in self._pending:
            chunk = self._read_serial_chunk(deadline=deadline, needle=needle)
            if chunk is not None:
                self._pending.extend(chunk)
        self.termination_checkpoint()
        end = self._pending.index(needle) + len(needle)
        del self._pending[:end]

    def _read_until_complete_line(self, line: bytes, timeout: float) -> None:
        """Wait until one terminal-terminated boot line exactly matches ``line``."""

        self._boot_line_buffer.extend(self._pending)
        self._pending.clear()
        deadline = time.monotonic() + timeout
        while True:
            newline = self._boot_line_buffer.find(b"\n")
            while newline >= 0:
                candidate = bytes(self._boot_line_buffer[:newline])
                del self._boot_line_buffer[: newline + 1]
                candidate = candidate.rstrip(b"\r")
                if candidate == line:
                    self.termination_checkpoint()
                    return
                newline = self._boot_line_buffer.find(b"\n")
            chunk = self._read_serial_chunk(deadline=deadline, needle=line)
            if chunk is not None:
                self._boot_line_buffer.extend(chunk)

    def _observe_post_terminal(self) -> None:
        deadline = time.monotonic() + self.post_terminal_timeout
        while True:
            try:
                self._read_serial_chunk(
                    deadline=deadline,
                    needle=b"post-terminal observation",
                )
            except TimeoutError:
                return

    def drain_after_terminal_action(self) -> None:
        """Drain bytes buffered during a bounded host-side terminal action."""

        deadline = time.monotonic() + TERMINAL_ACTION_DRAIN_TIMEOUT_SECONDS
        while True:
            try:
                self._read_serial_chunk(deadline=deadline, needle=b"terminal action drain")
            except TimeoutError:
                return

    def run(self) -> None:
        booti_started: float | None = None
        try:
            self.termination_checkpoint()
            self._read_until(b"=> ", self.startup_timeout)
            for command in self.commands:
                self.termination_checkpoint()
                self.stage = command.name
                is_booti = command.text.startswith("booti ")
                if is_booti:
                    self.booti_sent_count += 1
                    if self.booti_sent_count != 1:
                        raise RuntimeError("refusing to send a second booti command")
                    booti_started = time.monotonic()
                    self._pending.clear()
                    self._boot_line_buffer.clear()
                assert self.process.stdin is not None
                self.termination_checkpoint()
                self.process.stdin.write((command.text + "\n").encode())
                self.process.stdin.flush()
                self.termination_checkpoint()
                if is_booti:
                    if self.serial_interaction is not None:
                        interaction_deadline = time.monotonic() + self.boot_timeout
                        self._read_until_complete_line(
                            self.serial_interaction.ready_line,
                            interaction_deadline - time.monotonic(),
                        )
                        for input_step in self.serial_interaction.input_steps:
                            if input_step.ready_line is not None:
                                self._read_until_complete_line(
                                    input_step.ready_line,
                                    interaction_deadline - time.monotonic(),
                                )
                            elif input_step.ready_token is not None:
                                self._pending.extend(self._boot_line_buffer)
                                self._boot_line_buffer.clear()
                                self._read_until(
                                    input_step.ready_token,
                                    interaction_deadline - time.monotonic(),
                                )
                            self._pending.clear()
                            self._boot_line_buffer.clear()
                            self.termination_checkpoint()
                            self.process.stdin.write(input_step.input_bytes)
                            self.process.stdin.flush()
                            self.termination_checkpoint()
                        self._read_until_complete_line(
                            self.serial_interaction.completion_line,
                            interaction_deadline - time.monotonic(),
                        )
                    elif (
                        self.reboot_expectation is None
                        and not self.allow_completion_token
                    ):
                        self._read_until_complete_line(
                            self.completion_line,
                            self.boot_timeout,
                        )
                    elif self.reboot_expectation is None:
                        self._read_until(
                            self.completion_line,
                            self.boot_timeout,
                        )
                    else:
                        self._read_until(
                            self.reboot_expectation.trigger_marker,
                            self.boot_timeout,
                        )
                    self.marker_seen = True
                    if (
                        self.reboot_expectation is None
                        and self.post_terminal_timeout > 0
                    ):
                        self.stage = "post-terminal"
                        self._observe_post_terminal()
                    if self.reboot_expectation is not None:
                        recovery_started = time.monotonic()
                        recovery_deadline = (
                            recovery_started + self.reboot_expectation.recovery_timeout
                        )
                        for (
                            milestone_name,
                            milestone,
                        ) in self.reboot_expectation.milestones:
                            self.stage = f"recovery-{milestone_name}"
                            remaining = recovery_deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError(
                                    f"timed out waiting for {milestone!r}"
                                )
                            self._read_until(milestone, remaining)
                        recovery_finished = time.monotonic()
                        self.recovery_elapsed_seconds = (
                            recovery_finished - recovery_started
                        )
                        assert booti_started is not None
                        self.boot_to_recovery_elapsed_seconds = (
                            recovery_finished - booti_started
                        )
                        self.recovery_complete = True
                    break
                expected = command.expected_output.encode()
                self._read_until(expected, self.command_timeout)
                if expected not in (b"=>", b"=> "):
                    self._read_until(b"=> ", self.command_timeout)
                if self.command_observer is not None:
                    self.termination_checkpoint()
                    self.command_observer(
                        command,
                        self._transcript.decode(errors="replace"),
                    )
                    self.termination_checkpoint()
            self.termination_checkpoint()
        except TimeoutError:
            self.timed_out = True
            if self.stage == "startup":
                self.failure = "startup-timeout"
            elif self.stage == "booti":
                self.failure = "boot-timeout"
            elif self.stage.startswith("recovery-"):
                self.failure = (
                    f"recovery-timeout:{self.stage.removeprefix('recovery-')}"
                )
            else:
                self.failure = f"command-timeout:{self.stage}"
        except SerialOutputLimitExceeded:
            self.failure = f"serial-output-limit:{self.stage}"
        except ValueError:
            self.failure = f"command-validation:{self.stage}"
        except (BrokenPipeError, OSError, RuntimeError):
            self.failure = f"process-error:{self.stage}"


@dataclass(frozen=True)
class _ProcessCleanupResult:
    sigterm_sent: bool
    killed: bool
    cleanup_complete: bool
    failure: str | None


def _cleanup_serial_process(
    process: subprocess.Popen[bytes],
    *,
    selector: selectors.BaseSelector | None,
    owned_raw_log: BinaryIO | None,
    grace: float,
    stage: str,
    failure: str | None,
) -> _ProcessCleanupResult:
    """Close session resources and terminate every process-group member."""

    deferred_error: BaseException | None = None

    def record_error(error: BaseException, label: str) -> None:
        nonlocal deferred_error, failure
        if isinstance(error, Exception):
            if failure is None:
                failure = f"{label}:{stage}"
        elif deferred_error is None:
            deferred_error = error

    if owned_raw_log is not None:
        try:
            owned_raw_log.close()
        except BaseException as error:
            record_error(error, "serial-log-cleanup")
    if selector is not None:
        try:
            selector.close()
        except BaseException as error:
            record_error(error, "selector-cleanup")

    killed = False
    sigterm_sent = False
    cleanup_complete = False
    try:
        sigterm_sent, killed, cleanup_complete = _terminate_process_group(
            process,
            grace=grace,
        )
    except BaseException as error:
        record_error(error, "process-group-cleanup")
        fallback_killed, cleanup_complete = _force_kill_process_group(
            process,
            grace=grace,
        )
        killed = killed or fallback_killed

    for pipe in (process.stdin, process.stdout):
        if pipe is None:
            continue
        try:
            pipe.close()
        except BaseException as error:
            record_error(error, "pipe-cleanup")
    if process.poll() is None:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            cleanup_complete = False

    if deferred_error is not None:
        raise deferred_error
    if failure is None:
        if not cleanup_complete:
            failure = f"cleanup-error:{stage}"
        elif not sigterm_sent:
            failure = f"process-error:{stage}"
        elif killed:
            if process.returncode != -signal.SIGKILL:
                failure = f"process-error:{stage}"
        elif process.returncode not in (0, -signal.SIGTERM):
            failure = f"process-error:{stage}"

    return _ProcessCleanupResult(
        sigterm_sent=sigterm_sent,
        killed=killed,
        cleanup_complete=cleanup_complete,
        failure=failure,
    )


def _run_terminal_action_while_draining(
    protocol: _SerialProtocol,
    terminal_action: Callable[[], None],
) -> tuple[BaseException | None, threading.Thread | None]:
    """Run one terminal action while draining serial output and join its worker."""

    action_errors: list[BaseException] = []

    def run_terminal_action() -> None:
        try:
            terminal_action()
        except BaseException as error:
            action_errors.append(error)

    action_thread = threading.Thread(target=run_terminal_action, daemon=True)
    action_thread.start()
    deadline = time.monotonic() + TERMINAL_ACTION_TIMEOUT_SECONDS
    action_timed_out = False
    try:
        while action_thread.is_alive():
            if time.monotonic() >= deadline:
                action_timed_out = True
                break
            try:
                protocol._read_serial_chunk(
                    deadline=min(
                        deadline,
                        time.monotonic() + TERMINAL_ACTION_DRAIN_TIMEOUT_SECONDS,
                    ),
                    needle=b"terminal action drain",
                )
            except TimeoutError:
                continue
            except SerialOutputLimitExceeded:
                protocol.failure = f"serial-output-limit:{protocol.stage}"
            except ValueError:
                protocol.failure = f"command-validation:{protocol.stage}"
                break
            except (OSError, RuntimeError):
                protocol.failure = f"process-error:{protocol.stage}"
                break
    except BaseException:
        # Preserve join-before-cleanup when the bounded action completes, but
        # never let a hostile callback block deferred process termination.
        action_thread.join(max(0.0, deadline - time.monotonic()))
        raise
    finally:
        if action_thread.is_alive():
            action_timed_out = True
        if not action_timed_out:
            action_thread.join()
        else:
            action_thread.join(TERMINAL_ACTION_DRAIN_TIMEOUT_SECONDS)
    if action_timed_out:
        return TimeoutError("terminal action exceeded its bounded timeout"), action_thread
    terminal_action_error = action_errors[0] if action_errors else None
    try:
        protocol.drain_after_terminal_action()
    except SerialOutputLimitExceeded:
        protocol.failure = f"serial-output-limit:{protocol.stage}"
    except ValueError:
        protocol.failure = f"command-validation:{protocol.stage}"
    except (OSError, RuntimeError):
        if terminal_action_error is None:
            protocol.failure = f"process-error:{protocol.stage}"
    return terminal_action_error, None


def run_serial_session(
    argv: Sequence[str],
    *,
    commands: Sequence[BootCommand],
    raw_log_path: Path,
    startup_timeout: float,
    command_timeout: float,
    boot_timeout: float,
    termination_grace: float,
    command_observer: Callable[[BootCommand, str], None] | None = None,
    reboot_expectation: RebootExpectation | None = None,
    serial_interaction: SerialInteraction | None = None,
    completion_line: bytes = USERSPACE_MARKER,
    allow_completion_token: bool = False,
    post_terminal_timeout: float = 0.0,
    milestone_expectations: Sequence[MilestoneExpectation] = (),
    raw_log_file: BinaryIO | None = None,
    terminal_action: Callable[[], None] | None = None,
) -> SessionResult:
    """Run one guarded serial session and its optional live terminal action.

    Runs the action once after a successful marker and post-terminal observation
    while QEMU remains live; suppresses it for timeout, protocol/process failure,
    exit, and reboot recovery. Serial bytes are drained after the action, and an
    action exception is re-raised only after process-group cleanup.
    """

    _validate_session_timeouts(
        startup_timeout=startup_timeout,
        command_timeout=command_timeout,
        boot_timeout=boot_timeout,
        termination_grace=termination_grace,
        post_terminal_timeout=post_terminal_timeout,
        reboot_expectation=reboot_expectation,
    )

    booti_count = sum(command.text.startswith("booti ") for command in commands)
    if booti_count > 1:
        raise ValueError("refusing to launch a command plan with a second booti")
    if serial_interaction is not None and booti_count != 1:
        raise ValueError("serial interaction requires exactly one booti command")
    if serial_interaction is not None and reboot_expectation is not None:
        raise ValueError("serial interaction cannot be combined with reboot recovery")
    if (
        serial_interaction is not None
        and completion_line
        not in (USERSPACE_MARKER, serial_interaction.completion_line)
    ):
        raise ValueError("serial interaction cannot use a competing completion line")

    def run_with_log(log_path: Path, log_file: BinaryIO) -> SessionResult:
        with (
            _defer_termination_until_cleanup() as deferred_termination,
            _adopt_orphaned_descendants(),
        ):
            return _run_serial_session(
                argv,
                commands=commands,
                raw_log_path=log_path,
                startup_timeout=startup_timeout,
                command_timeout=command_timeout,
                boot_timeout=boot_timeout,
                termination_grace=termination_grace,
                command_observer=command_observer,
                reboot_expectation=reboot_expectation,
                serial_interaction=serial_interaction,
                completion_line=completion_line,
                allow_completion_token=allow_completion_token,
                post_terminal_timeout=post_terminal_timeout,
                milestone_expectations=milestone_expectations,
                raw_log_file=log_file,
                terminal_action=terminal_action,
                deferred_termination=deferred_termination,
            )

    if raw_log_file is not None:
        return run_with_log(raw_log_path, raw_log_file)

    with PinnedOutputDirectory.open(raw_log_path.parent) as directory:
        existing = directory.entry_metadata(raw_log_path.name)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise ValueError("serial log must not be a symbolic link")
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError("serial log must be a regular file")
        directory.remove_entry(raw_log_path.name)
        with directory.create_exclusive(raw_log_path.name) as log_file:
            result = run_with_log(directory.path / raw_log_path.name, log_file)
            log_file.flush()
            os.fsync(log_file.fileno())
            directory.verify_open_file(raw_log_path.name, log_file)
            directory.verify_current()
            return result


def _run_serial_session(
    argv: Sequence[str],
    *,
    commands: Sequence[BootCommand],
    raw_log_path: Path,
    startup_timeout: float,
    command_timeout: float,
    boot_timeout: float,
    termination_grace: float,
    command_observer: Callable[[BootCommand, str], None] | None = None,
    reboot_expectation: RebootExpectation | None = None,
    serial_interaction: SerialInteraction | None = None,
    completion_line: bytes = USERSPACE_MARKER,
    allow_completion_token: bool = False,
    post_terminal_timeout: float = 0.0,
    milestone_expectations: Sequence[MilestoneExpectation] = (),
    raw_log_file: BinaryIO | None = None,
    deferred_termination: _DeferredTermination,
    terminal_action: Callable[[], None] | None = None,
) -> SessionResult:
    """Run one session while orphaned descendants are adopted by this process."""

    deferred_termination.raise_if_pending()
    raw_log_path.parent.mkdir(parents=True, exist_ok=True)
    owned_raw_log: BinaryIO | None = None
    if raw_log_file is None:
        owned_raw_log = raw_log_path.open("wb")
        raw_log_file = owned_raw_log
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        if owned_raw_log is not None:
            owned_raw_log.close()
        raise
    selector: selectors.BaseSelector | None = None
    protocol: _SerialProtocol | None = None
    cleanup: _ProcessCleanupResult | None = None
    terminal_action_error: BaseException | None = None
    terminal_action_worker: threading.Thread | None = None
    try:
        deferred_termination.raise_if_pending()
        assert process.stdin is not None
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert raw_log_file is not None
        protocol = _SerialProtocol(
            process=process,
            selector=selector,
            raw_log=raw_log_file,
            commands=commands,
            startup_timeout=startup_timeout,
            command_timeout=command_timeout,
            boot_timeout=boot_timeout,
            command_observer=command_observer,
            reboot_expectation=reboot_expectation,
            serial_interaction=serial_interaction,
            completion_line=completion_line,
            allow_completion_token=allow_completion_token,
            termination_checkpoint=deferred_termination.raise_if_pending,
            post_terminal_timeout=post_terminal_timeout,
            milestone_expectations=milestone_expectations,
        )
        protocol.run()
        if (
            terminal_action is not None
            and protocol.marker_seen
            and not protocol.timed_out
            and protocol.failure is None
            and reboot_expectation is None
            and process.poll() is None
        ):
            terminal_action_error, terminal_action_worker = _run_terminal_action_while_draining(
                protocol,
                terminal_action,
            )
    finally:
        try:
            cleanup = _cleanup_serial_process(
                process,
                selector=selector,
                owned_raw_log=owned_raw_log,
                grace=termination_grace,
                stage="startup" if protocol is None else protocol.stage,
                failure=None if protocol is None else protocol.failure,
            )
        except BaseException as cleanup_error:
            if terminal_action_error is not None:
                raise cleanup_error from terminal_action_error
            raise
        finally:
            if terminal_action_worker is not None:
                # A hostile Python callback cannot be killed safely.  Keep it
                # daemonized and make only a bounded post-cleanup join attempt.
                terminal_action_worker.join(TERMINAL_ACTION_DRAIN_TIMEOUT_SECONDS)

    if terminal_action_error is not None:
        raise terminal_action_error

    assert protocol is not None
    assert cleanup is not None
    return SessionResult(
        marker_seen=protocol.marker_seen,
        booti_sent_count=protocol.booti_sent_count,
        timed_out=protocol.timed_out,
        killed=cleanup.killed,
        cleanup_complete=cleanup.cleanup_complete,
        returncode=process.returncode,
        failure=cleanup.failure,
        termination_action=(
            "SIGKILL"
            if cleanup.killed
            else "SIGTERM"
            if cleanup.sigterm_sent
            else "already-exited"
        ),
        recovery_complete=protocol.recovery_complete,
        recovery_elapsed_seconds=protocol.recovery_elapsed_seconds,
        boot_to_recovery_elapsed_seconds=protocol.boot_to_recovery_elapsed_seconds,
        milestones=protocol._milestone_tracker.events,
    )
