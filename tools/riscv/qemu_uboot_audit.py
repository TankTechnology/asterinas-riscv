"""Offline evidence auditing for QEMU U-Boot serial transcripts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from qemu_uboot_artifacts import (
    DEFAULT_ARTIFACTS,
    DTB_LOAD_ADDRESS,
    INITRD_LOAD_ADDRESS,
    KERNEL_LOAD_ADDRESS,
    ArtifactExpectations,
    validate_bdinfo_memory_layout,
)
from qemu_uboot_commands import (
    BOOTI_COMMAND,
    RNG_SEED_REMOVE_COMMAND,
    STALE_BOOTARGS,
    USERSPACE_MARKER_TEXT,
    BootCommand,
    BootScenario,
)
from qemu_uboot_profiles import (
    GENERIC_SV39,
    AuditPolicy,
    MilestoneExpectation,
    QemuUbootProfile,
    ResultScope,
    validate_registered_profile,
)
from qemu_uboot_variants import (
    FIRST_PROCESS_CONSOLE_LOSS,
    QemuUbootVariant,
    effective_bootargs as variant_effective_bootargs,
    validate_registered_variant,
)


DIAGNOSTIC_PREFIX = "ASTERINAS_FIRST_PROCESS_DIAG"
NS16550_REGISTRATION_MESSAGE = "uart: Registered NS16550A as a console"
REQUIRED_DIAGNOSTIC_STAGES = (
    "diagnostic_active",
    "process_components_ready",
    "device_init_ready",
    "stdio_init_ready",
    "user_enter",
    "user_first_return",
    "user_first_syscall",
    "user_first_write_returned",
)
ALL_DIAGNOSTIC_STAGES = (
    *REQUIRED_DIAGNOSTIC_STAGES[:6],
    "user_first_page_fault",
    "user_first_page_fault_handler",
    "user_page_fault_repeated",
    *REQUIRED_DIAGNOSTIC_STAGES[6:],
)

_PREFIX_PATTERN = re.escape(DIAGNOSTIC_PREFIX)
_USER_ENTER_PATTERN = re.compile(
    rf"{_PREFIX_PATTERN} stage=user_enter "
    r"cpu=([0-9]+) sepc=0x([0-9a-f]+) sp=0x([0-9a-f]+)"
)
_SIMPLE_RETURN_PATTERN = re.compile(
    rf"{_PREFIX_PATTERN} stage=user_first_return "
    r"reason=(user_syscall|kernel_event) sepc=0x([0-9a-f]+)"
)
_EXCEPTION_RETURN_PATTERN = re.compile(
    rf"{_PREFIX_PATTERN} stage=user_first_return "
    r"reason=user_exception sepc=0x([0-9a-f]+) cause=([a-z_]+) "
    r"detail_kind=([a-z_]+)(?: detail=0x([0-9a-f]+))?"
)
_SYSCALL_PATTERN = re.compile(
    rf"{_PREFIX_PATTERN} stage=user_first_syscall "
    r"id=([0-9]+) sepc=0x([0-9a-f]+)"
)
_WRITE_PATTERN = re.compile(
    rf"{_PREFIX_PATTERN} stage=user_first_write_returned "
    r"fd=([0-9]+) requested=([0-9]+) result=(-?[0-9]+)"
)
_PAGE_FAULT_PATTERN = re.compile(
    rf"{_PREFIX_PATTERN} "
    r"stage=(user_first_page_fault|user_page_fault_repeated) "
    r"cause=(instruction_page_fault|load_page_fault|store_page_fault) "
    r"stval=0x([0-9a-f]+) sepc=0x([0-9a-f]+)"
)
_PAGE_FAULT_HANDLER_PATTERN = re.compile(
    rf"{_PREFIX_PATTERN} stage=user_first_page_fault_handler "
    r"outcome=(resolved|fault_signal_queued)"
)
_EXCEPTION_DETAIL_KINDS = {
    "instruction_misaligned": "unavailable",
    "instruction_fault": "unavailable",
    "illegal_instruction": "instruction",
    "breakpoint": "unavailable",
    "load_misaligned": "stval",
    "load_fault": "stval",
    "store_misaligned": "stval",
    "store_fault": "stval",
    "user_env_call": "unavailable",
    "supervisor_env_call": "unavailable",
    "instruction_page_fault": "stval",
    "load_page_fault": "stval",
    "store_page_fault": "stval",
    "unknown": "unavailable",
}


@dataclass(frozen=True)
class DiagnosticStageEvidence:
    """Schema-valid occurrences of one stable diagnostic stage."""

    stage: str
    count: int
    indices: tuple[int, ...]


@dataclass(frozen=True)
class DiagnosticAudit:
    """Ordered first-process diagnostic evidence from one serial transcript."""

    total_count: int
    activation_count: int
    stage_evidence: tuple[DiagnosticStageEvidence, ...]
    ordered_stages: tuple[str, ...]
    last_stage: str | None
    first_return_reason: str | None
    first_syscall: int | None
    first_syscall_sepc: int | None
    write_fd: int | None
    write_requested: int | None
    write_result: int | None
    page_fault_outcome: str | None
    repeated_page_fault: bool
    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class _ParsedDiagnosticMarker:
    stage: str
    first_return_reason: str | None = None
    first_syscall: int | None = None
    first_syscall_sepc: int | None = None
    write_fd: int | None = None
    write_requested: int | None = None
    write_result: int | None = None
    page_fault_outcome: str | None = None


@dataclass(frozen=True)
class BootAudit:
    """Counts and final disposition of one serial boot transcript."""

    booti_command_count: int
    userspace_marker_count: int
    effective_bootargs: str
    diagnostic: DiagnosticAudit
    application_processor_ids: tuple[int, ...]
    random_source: str | None
    classification: str
    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class _ScenarioPolicy:
    """Fixed bootargs and diagnostic parser selection for one scenario."""

    scenario: BootScenario
    effective_bootargs: str
    environment_bootargs: str
    diagnostic_variant: QemuUbootVariant | None


def memory_layout_observer(
    artifacts: ArtifactExpectations,
) -> Callable[[BootCommand, str], None]:
    """Create a command observer that gates loads on live ``bdinfo`` ranges."""

    def observe(command: BootCommand, transcript: str) -> None:
        if command.name == "memory-layout":
            validate_bdinfo_memory_layout(transcript, artifacts)

    return observe


def _normalized_lines(serial_log: str) -> list[str]:
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", serial_log)
    return without_ansi.replace("\r", "").splitlines()


def _normalized_diagnostic_lines(serial_log: str) -> list[str]:
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", serial_log)
    normalized_newlines = without_ansi.replace("\r\n", "\n").replace("\r", "\n")
    return normalized_newlines.splitlines(keepends=True)


def _is_diagnostic_line(line: str) -> bool:
    return line.startswith(DIAGNOSTIC_PREFIX)


def _parse_diagnostic_marker(line: str) -> _ParsedDiagnosticMarker | None:
    exact_lines = {
        f"{DIAGNOSTIC_PREFIX} stage=diagnostic_active console_registry=empty": (
            "diagnostic_active"
        ),
        f"{DIAGNOSTIC_PREFIX} stage=process_components_ready": (
            "process_components_ready"
        ),
        f"{DIAGNOSTIC_PREFIX} stage=device_init_ready": "device_init_ready",
        f"{DIAGNOSTIC_PREFIX} stage=stdio_init_ready": "stdio_init_ready",
    }
    if stage := exact_lines.get(line):
        return _ParsedDiagnosticMarker(stage=stage)

    if match := _USER_ENTER_PATTERN.fullmatch(line):
        return _ParsedDiagnosticMarker(stage="user_enter")

    if match := _SIMPLE_RETURN_PATTERN.fullmatch(line):
        return _ParsedDiagnosticMarker(
            stage="user_first_return",
            first_return_reason=match.group(1),
        )

    if match := _EXCEPTION_RETURN_PATTERN.fullmatch(line):
        cause = match.group(2)
        detail_kind = match.group(3)
        detail = match.group(4)
        expected_detail_kind = _EXCEPTION_DETAIL_KINDS.get(cause)
        if expected_detail_kind != detail_kind:
            return None
        if (detail_kind == "unavailable") != (detail is None):
            return None
        return _ParsedDiagnosticMarker(
            stage="user_first_return",
            first_return_reason="user_exception",
        )

    if match := _SYSCALL_PATTERN.fullmatch(line):
        try:
            return _ParsedDiagnosticMarker(
                stage="user_first_syscall",
                first_syscall=int(match.group(1)),
                first_syscall_sepc=int(match.group(2), 16),
            )
        except ValueError:
            return None

    if match := _WRITE_PATTERN.fullmatch(line):
        try:
            return _ParsedDiagnosticMarker(
                stage="user_first_write_returned",
                write_fd=int(match.group(1)),
                write_requested=int(match.group(2)),
                write_result=int(match.group(3)),
            )
        except ValueError:
            return None

    if match := _PAGE_FAULT_PATTERN.fullmatch(line):
        return _ParsedDiagnosticMarker(stage=match.group(1))

    if match := _PAGE_FAULT_HANDLER_PATTERN.fullmatch(line):
        return _ParsedDiagnosticMarker(
            stage="user_first_page_fault_handler",
            page_fault_outcome=match.group(1),
        )

    return None


def _unique_marker(
    markers: dict[str, list[_ParsedDiagnosticMarker]], stage: str
) -> _ParsedDiagnosticMarker | None:
    stage_markers = markers[stage]
    return stage_markers[0] if len(stage_markers) == 1 else None


def audit_diagnostic_markers(
    serial_log: str,
    *,
    variant: QemuUbootVariant | None = None,
) -> DiagnosticAudit:
    """Parse exact first-process markers without performing any I/O."""

    if variant is not None:
        validate_registered_variant(variant)

    indices_by_stage: dict[str, list[int]] = {
        stage: [] for stage in ALL_DIAGNOSTIC_STAGES
    }
    markers_by_stage: dict[str, list[_ParsedDiagnosticMarker]] = {
        stage: [] for stage in ALL_DIAGNOSTIC_STAGES
    }
    failures: list[str] = []
    ordered_stages: list[str] = []
    seen_stages: set[str] = set()
    required_index = 0
    optional_state = 0
    frozen = False
    total_count = 0

    for line_index, retained_line in enumerate(
        _normalized_diagnostic_lines(serial_log)
    ):
        terminated = retained_line.endswith("\n")
        line = retained_line[:-1] if terminated else retained_line
        if not _is_diagnostic_line(line):
            continue

        total_count += 1
        if not terminated:
            failures.append(
                f"diagnostic marker at line {line_index} is not newline-terminated"
            )
            frozen = True
            continue

        marker = _parse_diagnostic_marker(line)
        if marker is None:
            failures.append(f"malformed diagnostic marker at line {line_index}")
            frozen = True
            continue

        indices_by_stage[marker.stage].append(line_index)
        markers_by_stage[marker.stage].append(marker)
        if marker.stage in seen_stages:
            failures.append(
                f"duplicate diagnostic stage {marker.stage} at line {line_index}"
            )
            frozen = True
            continue
        seen_stages.add(marker.stage)

        if frozen:
            continue

        if marker.stage in REQUIRED_DIAGNOSTIC_STAGES:
            expected_stage = (
                REQUIRED_DIAGNOSTIC_STAGES[required_index]
                if required_index < len(REQUIRED_DIAGNOSTIC_STAGES)
                else None
            )
            if marker.stage != expected_stage:
                failures.append(
                    f"out-of-order diagnostic stage {marker.stage} at line {line_index}; "
                    f"expected {expected_stage}"
                )
                frozen = True
                continue
            if marker.stage == "user_first_syscall" and optional_state == 1:
                failures.append("first page fault has no handler outcome")
                frozen = True
                continue

            ordered_stages.append(marker.stage)
            required_index += 1
            continue

        if required_index != REQUIRED_DIAGNOSTIC_STAGES.index("user_first_syscall"):
            failures.append(
                f"misordered optional diagnostic stage {marker.stage} "
                f"at line {line_index}"
            )
            frozen = True
            continue

        expected_optional_state = {
            "user_first_page_fault": (0, 1),
            "user_first_page_fault_handler": (1, 2),
            "user_page_fault_repeated": (2, 3),
        }
        expected_state, following_state = expected_optional_state[marker.stage]
        if optional_state != expected_state:
            failures.append(
                f"misordered optional diagnostic stage {marker.stage} "
                f"at line {line_index}"
            )
            frozen = True
            continue
        optional_state = following_state

    if variant is None:
        if total_count:
            failures.append("diagnostic markers are not allowed without a variant")
    else:
        if optional_state == 1:
            failures.append("first page fault has no handler outcome")
        for missing_stage in REQUIRED_DIAGNOSTIC_STAGES[len(ordered_stages) :]:
            failures.append(f"missing diagnostic stage: {missing_stage}")

    stage_evidence = tuple(
        DiagnosticStageEvidence(
            stage=stage,
            count=len(indices_by_stage[stage]),
            indices=tuple(indices_by_stage[stage]),
        )
        for stage in ALL_DIAGNOSTIC_STAGES
    )
    first_return = _unique_marker(markers_by_stage, "user_first_return")
    first_syscall = _unique_marker(markers_by_stage, "user_first_syscall")
    first_write = _unique_marker(markers_by_stage, "user_first_write_returned")
    page_fault_handler = _unique_marker(
        markers_by_stage, "user_first_page_fault_handler"
    )
    last_stage = ordered_stages[-1] if variant is not None and ordered_stages else None

    return DiagnosticAudit(
        total_count=total_count,
        activation_count=len(indices_by_stage["diagnostic_active"]),
        stage_evidence=stage_evidence,
        ordered_stages=tuple(ordered_stages),
        last_stage=last_stage,
        first_return_reason=(
            first_return.first_return_reason if first_return is not None else None
        ),
        first_syscall=(
            first_syscall.first_syscall if first_syscall is not None else None
        ),
        first_syscall_sepc=(
            first_syscall.first_syscall_sepc if first_syscall is not None else None
        ),
        write_fd=first_write.write_fd if first_write is not None else None,
        write_requested=(
            first_write.write_requested if first_write is not None else None
        ),
        write_result=first_write.write_result if first_write is not None else None,
        page_fault_outcome=(
            page_fault_handler.page_fault_outcome
            if page_fault_handler is not None
            else None
        ),
        repeated_page_fault=bool(markers_by_stage["user_page_fault_repeated"]),
        passed=not failures,
        failures=tuple(failures),
    )


def _scenario_policy(
    profile: QemuUbootProfile,
    scenario: BootScenario,
    variant: QemuUbootVariant | None,
    bootargs_override: str | None = None,
) -> _ScenarioPolicy:
    """Resolve the immutable bootargs and diagnostic contract once."""

    scenario = BootScenario(scenario)
    if bootargs_override is not None and scenario is not BootScenario.POSITIVE:
        raise ValueError("bootargs override requires the positive scenario")
    if scenario is BootScenario.FIRST_PROCESS_CONSOLE_LOSS:
        if variant is None:
            raise ValueError("first-process-console-loss requires its fixed variant")
        validate_registered_variant(variant)
        if variant is not FIRST_PROCESS_CONSOLE_LOSS:
            raise ValueError("first-process-console-loss requires its fixed variant")
        effective_bootargs = variant_effective_bootargs(profile, variant)
        diagnostic_variant = variant
    elif variant is not None:
        raise ValueError("variants require the first-process-console-loss scenario")
    elif scenario is BootScenario.REGISTERED_CONSOLE_SUPPRESSION:
        effective_bootargs = variant_effective_bootargs(
            profile,
            FIRST_PROCESS_CONSOLE_LOSS,
        )
        diagnostic_variant = None
    else:
        effective_bootargs = (
            profile.bootargs if bootargs_override is None else bootargs_override
        )
        diagnostic_variant = None

    environment_bootargs = (
        STALE_BOOTARGS
        if scenario is BootScenario.STALE_BOOTARGS
        else effective_bootargs
    )
    return _ScenarioPolicy(
        scenario=scenario,
        effective_bootargs=effective_bootargs,
        environment_bootargs=environment_bootargs,
        diagnostic_variant=diagnostic_variant,
    )


def _kernel_message_indices(lines: list[str], message: str) -> tuple[int, ...]:
    """Return whole-line matches with only the standard logger prefix allowed."""

    pattern = re.compile(
        r"(?:\[\s*[0-9]+\.[0-9]+\]\s+INFO\s*:\s*)?" + re.escape(message)
    )
    return tuple(index for index, line in enumerate(lines) if pattern.fullmatch(line))


def _marker_event_failures(
    marker_event: str,
    scenario: BootScenario,
) -> tuple[str, ...]:
    """Validate the exact watcher and cleanup provenance for a scenario."""

    fields: dict[str, str] = {}
    failures: list[str] = []
    allowed_fields = {"marker_seen", "action", "cleanup_complete", "failure"}
    for line_index, line in enumerate(marker_event.splitlines()):
        match = re.fullmatch(r"([a-z_]+)=([A-Za-z0-9_:-]+)", line)
        if match is None:
            failures.append(f"malformed marker event line {line_index}")
            continue
        name, value = match.groups()
        if name not in allowed_fields:
            failures.append(f"unknown marker event field: {name}")
            continue
        if name in fields:
            failures.append(f"duplicate marker event field: {name}")
            continue
        fields[name] = value

    if scenario is BootScenario.STALE_BOOTARGS:
        expected_fields = {
            "marker_seen": "no",
            "cleanup_complete": "yes",
            "failure": "boot-timeout",
        }
        expected_names = {*expected_fields, "action"}
    else:
        expected_fields = {
            "marker_seen": "yes",
            "cleanup_complete": "yes",
        }
        expected_names = {*expected_fields, "action"}

    if set(fields) != expected_names:
        failures.append("marker event fields do not match the fixed scenario contract")
    for name, expected in expected_fields.items():
        if fields.get(name) != expected:
            failures.append(f"marker event {name} did not equal {expected}")
    if fields.get("action") not in {"SIGTERM", "SIGKILL"}:
        failures.append("marker event lacks SIGTERM/SIGKILL provenance")
    return tuple(failures)


def _audit_registered_milestones(
    serial_log: str,
    *,
    marker_event: str,
    artifacts: ArtifactExpectations,
    profile: QemuUbootProfile,
    scenario: BootScenario,
    variant: QemuUbootVariant | None,
    terminal_marker: str,
    bootargs_override: str | None,
) -> BootAudit:
    """Audit a named-board run only against its registered observable contract."""

    validate_registered_profile(profile)
    if scenario is not BootScenario.POSITIVE or variant is not None:
        raise ValueError("registered-milestone audit requires the positive scenario")
    if bootargs_override is not None:
        raise ValueError("registered-milestone audit rejects a bootargs override")
    if terminal_marker != profile.validation.completion_line.decode():
        raise ValueError("terminal marker differs from the registered scenario")

    lines = _normalized_lines(serial_log)
    failures: list[str] = []
    terminal_indices = [
        index for index, line in enumerate(lines) if line == terminal_marker
    ]
    terminal_count = len(terminal_indices)
    recovery = profile.validation.recovery
    milestone_lines = (
        lines[: terminal_indices[0] + 1]
        if recovery is not None and terminal_count == 1
        else lines
    )
    milestone_indices: list[tuple[MilestoneExpectation, list[int]]] = []
    last_index = -1
    for expectation in profile.validation.milestones:
        marker = expectation.line.decode()
        indices = [
            line_index
            for line_index, line in enumerate(milestone_lines)
            if marker in line
        ]
        milestone_indices.append((expectation, indices))
        if not indices:
            failures.append(f"missing milestone: {expectation.stage.value}")
            continue
        if len(indices) != expectation.expected_occurrences:
            failures.append(
                "unexpected milestone occurrence count: "
                f"{expectation.stage.value} expected "
                f"{expectation.expected_occurrences}, got {len(indices)}"
            )
            continue

        if indices[0] <= last_index:
            failures.append(f"out-of-order milestone: {expectation.stage.value}")
            continue
        last_index = indices[-1]

    booti_lines = [
        index
        for index, line in enumerate(lines)
        if line.removeprefix("=> ") == BOOTI_COMMAND
    ]
    if len(booti_lines) != 1:
        failures.append(f"expected exactly one booti command, got {len(booti_lines)}")
    if terminal_count != 1:
        failures.append(f"expected exactly one terminal marker, got {terminal_count}")
    else:
        terminal_index = terminal_indices[0]
        if recovery is None:
            for expectation, indices in milestone_indices:
                if expectation.stage is profile.validation.terminal:
                    continue
                if any(index > terminal_index for index in indices):
                    failures.append(
                        "milestone repeated after terminal: "
                        f"{expectation.stage.value}"
                    )
        else:
            armed_indices = [
                index
                for index, line in enumerate(lines[:terminal_index])
                if line == recovery.armed_marker.decode()
            ]
            if len(armed_indices) != 1:
                failures.append(
                    "expected exactly one recovery armed marker before terminal"
                )
            recovery_indices: list[int] = []
            for name, marker_bytes in recovery.milestones:
                marker = marker_bytes.decode().strip()
                matches = [
                    index
                    for index, line in enumerate(
                        lines[terminal_index + 1 :],
                        start=terminal_index + 1,
                    )
                    if (line.strip() == marker if name == "prompt" else marker in line)
                ]
                if len(matches) != 1:
                    failures.append(
                        f"expected exactly one post-terminal recovery {name} marker"
                    )
                else:
                    recovery_indices.append(matches[0])
            if len(recovery_indices) == len(recovery.milestones) and any(
                current >= following
                for current, following in zip(
                    recovery_indices,
                    recovery_indices[1:],
                )
            ):
                failures.append("post-terminal recovery markers are out of order")

    required_fragments = (
        "OpenSBI v",
        "U-Boot 2026.07",
        f"{artifacts.kernel_size} bytes read",
        f"{artifacts.dtb_size} bytes read",
        f"{artifacts.initrd_size} bytes read",
        f'bootargs={profile.bootargs}',
        f'bootargs = "{profile.bootargs}";',
        "ASTERINAS_PRE_BOOTI",
        "Starting kernel ...",
    )
    failures.extend(
        f"missing required output: {fragment}"
        for fragment in required_fragments
        if fragment not in serial_log
    )
    for start, size, crc32 in (
        (KERNEL_LOAD_ADDRESS, artifacts.kernel_size, artifacts.kernel_crc32),
        (DTB_LOAD_ADDRESS, artifacts.dtb_size, artifacts.dtb_crc32),
        (INITRD_LOAD_ADDRESS, artifacts.initrd_size, artifacts.initrd_crc32),
    ):
        expected = f"crc32 for {start:08x} ... {start + size - 1:08x} ==> {crc32}"
        if sum(line == expected for line in lines) != 1:
            failures.append(f"expected exactly one CRC line: {expected}")
    if any(line.strip() == "=> saveenv" for line in lines):
        failures.append("persistent U-Boot environment write detected")
    for marker in profile.validation.forbidden_markers:
        if marker.decode() in serial_log:
            failures.append(f"forbidden output matched: {marker.decode()}")
    failures.extend(_marker_event_failures(marker_event, scenario))
    try:
        validate_bdinfo_memory_layout(serial_log, artifacts)
    except ValueError as error:
        failures.append(str(error))

    passed = not failures
    if passed and profile.validation.scope is ResultScope.COMPLETE_BOOT:
        classification = "BOOT_COMPLETED"
    elif passed:
        classification = "PROBE_COMPLETED"
    else:
        classification = "FAIL"
    return BootAudit(
        booti_command_count=len(booti_lines),
        userspace_marker_count=terminal_count,
        effective_bootargs=profile.bootargs,
        diagnostic=audit_diagnostic_markers("", variant=None),
        application_processor_ids=(),
        random_source=None,
        classification=classification,
        passed=passed,
        failures=tuple(failures),
    )


def audit_serial_log(
    serial_log: str,
    *,
    marker_event: str,
    artifacts: ArtifactExpectations = DEFAULT_ARTIFACTS,
    profile: QemuUbootProfile = GENERIC_SV39,
    scenario: BootScenario = BootScenario.POSITIVE,
    variant: QemuUbootVariant | None = None,
    userspace_marker: str = USERSPACE_MARKER_TEXT,
    readiness_marker: str | None = None,
    bootargs_override: str | None = None,
) -> BootAudit:
    """Audit the one-shot markers in a U-Boot serial transcript."""

    if not userspace_marker or "\n" in userspace_marker or "\r" in userspace_marker:
        raise ValueError("userspace marker must be one non-empty unterminated line")
    if readiness_marker is not None and (
        not readiness_marker or "\n" in readiness_marker or "\r" in readiness_marker
    ):
        raise ValueError("readiness marker must be one non-empty unterminated line")

    if profile.validation.audit_policy is AuditPolicy.REGISTERED_MILESTONES:
        if readiness_marker is not None:
            raise ValueError("registered-milestone audit rejects serial interaction")
        return _audit_registered_milestones(
            serial_log,
            marker_event=marker_event,
            artifacts=artifacts,
            profile=profile,
            scenario=scenario,
            variant=variant,
            terminal_marker=userspace_marker,
            bootargs_override=bootargs_override,
        )

    policy = _scenario_policy(
        profile,
        scenario,
        variant,
        bootargs_override=bootargs_override,
    )
    scenario = policy.scenario
    lines = _normalized_lines(serial_log)
    booti_indices = [
        index
        for index, line in enumerate(lines)
        if line.removeprefix("=> ") == BOOTI_COMMAND
    ]
    booti_count = len(booti_indices)
    marker_count = sum(line == userspace_marker for line in lines)
    readiness_count = (
        None
        if readiness_marker is None
        else sum(line == readiness_marker for line in lines)
    )
    diagnostic = audit_diagnostic_markers(
        serial_log,
        variant=policy.diagnostic_variant,
    )
    required_fragments = (
        "OpenSBI v1.7",
        "U-Boot 2026.07",
        f"{artifacts.kernel_size} bytes read",
        f"{artifacts.dtb_size} bytes read",
        f"{artifacts.initrd_size} bytes read",
    )
    failures = [
        f"missing required output: {fragment}"
        for fragment in required_fragments
        if fragment not in serial_log
    ]
    if readiness_count is not None and readiness_count != 1:
        failures.append(
            f"expected exactly one readiness marker, got {readiness_count}"
        )
    failures.extend(f"diagnostic audit: {failure}" for failure in diagnostic.failures)
    expected_crc_lines = (
        (
            KERNEL_LOAD_ADDRESS,
            artifacts.kernel_size,
            artifacts.kernel_crc32,
        ),
        (DTB_LOAD_ADDRESS, artifacts.dtb_size, artifacts.dtb_crc32),
        (INITRD_LOAD_ADDRESS, artifacts.initrd_size, artifacts.initrd_crc32),
    )
    for start, size, crc32 in expected_crc_lines:
        expected = f"crc32 for {start:08x} ... {start + size - 1:08x} ==> {crc32}"
        count = sum(line == expected for line in lines)
        if count != 1:
            failures.append(f"expected exactly one CRC line: {expected}")
    required_bootarg_lines = (
        f'=> setenv bootargs "{policy.environment_bootargs}"',
        "=> printenv bootargs",
        f"bootargs={policy.environment_bootargs}",
        f'=> fdt set /chosen bootargs "{policy.effective_bootargs}"',
        "=> fdt print /chosen",
        f'bootargs = "{policy.effective_bootargs}";',
    )
    bootarg_indices = []
    for expected in required_bootarg_lines:
        matching_indices = [
            index for index, line in enumerate(lines) if line.strip() == expected
        ]
        if len(matching_indices) != 1:
            failures.append(f"expected exactly one bootargs line: {expected}")
        else:
            bootarg_indices.append(matching_indices[0])
    if len(bootarg_indices) == len(required_bootarg_lines):
        if any(
            current >= following
            for current, following in zip(bootarg_indices, bootarg_indices[1:])
        ):
            failures.append("bootargs proofs are out of order")
        if booti_count == 1 and bootarg_indices[-1] >= booti_indices[0]:
            failures.append("bootargs proofs did not complete before booti")
    if any(line.strip() == "=> saveenv" for line in lines):
        failures.append("persistent U-Boot environment write detected")
    if profile.remove_rng_seed:
        rng_commands = [
            index
            for index, line in enumerate(lines)
            if line.strip() == f"=> {RNG_SEED_REMOVE_COMMAND}"
        ]
        fdt_prints = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "=> fdt print /chosen"
        ]
        if len(rng_commands) != 1:
            failures.append("expected exactly one idempotent rng-seed removal")
        elif len(fdt_prints) == 1:
            print_index = fdt_prints[0]
            if rng_commands[0] >= print_index:
                failures.append("rng-seed removal did not precede its proof")
            next_command = next(
                (
                    index
                    for index in range(print_index + 1, len(lines))
                    if lines[index].strip().startswith("=> ")
                ),
                len(lines),
            )
            if any(
                re.match(r"\s*rng-seed\s*=", line)
                for line in lines[print_index + 1 : next_command]
            ):
                failures.append("final /chosen proof still contains rng-seed")
            if booti_count == 1 and print_index >= booti_indices[0]:
                failures.append("rng-seed proof did not complete before booti")
    extension_lines = [
        line for line in lines if line.startswith("Boot HART ISA Extensions")
    ]
    if len(extension_lines) != 1:
        failures.append("expected exactly one OpenSBI extension line")
    else:
        extensions = {
            extension.strip()
            for extension in extension_lines[0].partition(":")[2].split(",")
            if extension.strip()
        }
        other_ad_extension = "svadu" if profile.ad_extension == "svade" else "svade"
        if profile.ad_extension not in extensions or other_ad_extension in extensions:
            failures.append(f"OpenSBI did not prove forced {profile.ad_extension} mode")
        if (
            profile.required_random_source is not None
            and profile.required_random_source not in extensions
        ):
            failures.append(
                "OpenSBI did not prove required random source "
                f"{profile.required_random_source}"
            )
        if profile != GENERIC_SV39:
            for unconfirmed_extension in ("zkr", "svpbmt"):
                if unconfirmed_extension in extensions:
                    failures.append(
                        "OpenSBI reported unconfirmed Megrez extension "
                        f"{unconfirmed_extension}"
                    )
    if sum(line == "=> echo ASTERINAS_PRE_BOOTI" for line in lines) != 1:
        failures.append("expected exactly one pre-booti command")
    if sum(line == "ASTERINAS_PRE_BOOTI" for line in lines) != 1:
        failures.append("expected exactly one pre-booti output marker")
    if booti_count != 1:
        failures.append(f"expected exactly one booti command, got {booti_count}")
    if sum(line == "Starting kernel ..." for line in lines) != 1:
        failures.append("expected exactly one Starting kernel marker")
    if sum(line == "Enter riscv_boot" for line in lines) != 1:
        failures.append("expected exactly one riscv_boot entry marker")
    ap_matches = [
        re.search(r"Processor ([0-9]+) started\. Spinning for tasks\.$", line)
        for line in lines
    ]
    raw_ap_ids = [int(match.group(1)) for match in ap_matches if match]
    application_processor_ids = tuple(sorted(set(raw_ap_ids)))
    random_markers = {
        "timestamp": "use randomness based on the timestamp, which is insecure",
        "hardware": "use randomness generated by hardware",
        "device-tree": "use randomness provided by the device tree",
    }
    detected_random_sources = [
        source
        for source, marker in random_markers.items()
        if any(marker in line for line in lines)
    ]
    random_source = (
        detected_random_sources[0] if len(detected_random_sources) == 1 else None
    )
    if profile != GENERIC_SV39:
        expected_ap_ids = tuple(range(1, profile.hart_count))
        if application_processor_ids != expected_ap_ids or len(raw_ap_ids) != len(
            expected_ap_ids
        ):
            failures.append(
                f"application processor starts did not prove exactly {expected_ap_ids}"
            )
        megrez_markers = (
            f"Booting {profile.hart_count - 1} processors",
            "All application processors started. The BSP continues to run.",
            "OSTD initialized. Preparing components.",
            "[kernel] rootfs is ready",
        )
        for marker in megrez_markers:
            if sum(marker in line for line in lines) != 1:
                failures.append(f"expected exactly one Megrez marker: {marker}")
        if detected_random_sources != ["timestamp"]:
            failures.append("Megrez run did not prove timestamp RNG fallback only")

    if scenario is BootScenario.POSITIVE:
        if marker_count != 1:
            failures.append(
                f"expected exactly one userspace marker, got {marker_count}"
            )
    elif scenario is BootScenario.REGISTERED_CONSOLE_SUPPRESSION:
        if marker_count != 1:
            failures.append(
                f"expected exactly one userspace marker, got {marker_count}"
            )
        ns16550_indices = _kernel_message_indices(
            lines,
            NS16550_REGISTRATION_MESSAGE,
        )
        if len(ns16550_indices) != 1:
            failures.append(
                "registered-console-suppression requires exactly one NS16550 console"
            )
        if diagnostic.total_count != 0:
            failures.append(
                "registered-console-suppression emitted first-process diagnostics"
            )
    elif scenario is BootScenario.FIRST_PROCESS_CONSOLE_LOSS:
        if marker_count != FIRST_PROCESS_CONSOLE_LOSS.expected_userspace_marker_count:
            failures.append(
                "console-loss userspace marker count did not match its fixed variant"
            )
        ns16550_indices = _kernel_message_indices(
            lines,
            NS16550_REGISTRATION_MESSAGE,
        )
        if ns16550_indices:
            failures.append("console-loss unexpectedly registered an NS16550 console")

        process_complete_indices = _kernel_message_indices(
            lines,
            "All components initialization in Process stage completed",
        )
        activation_evidence = next(
            evidence
            for evidence in diagnostic.stage_evidence
            if evidence.stage == "diagnostic_active"
        )
        if len(process_complete_indices) != 1:
            failures.append(
                "console-loss requires exactly one Process-stage completion line"
            )
        elif (
            len(activation_evidence.indices) != 1
            or process_complete_indices[0] >= activation_evidence.indices[0]
        ):
            failures.append(
                "Process-stage completion was not proven before diagnostic activation"
            )

        if diagnostic.first_syscall != 56:
            failures.append("console-loss first syscall was not openat (56)")
        expected_write = (1, 50, 50)
        observed_write = (
            diagnostic.write_fd,
            diagnostic.write_requested,
            diagnostic.write_result,
        )
        if observed_write != expected_write:
            failures.append(
                "console-loss terminal write did not prove fd=1 requested=50 result=50"
            )

        evidence_by_stage = {
            evidence.stage: evidence for evidence in diagnostic.stage_evidence
        }
        page_fault_count = evidence_by_stage["user_first_page_fault"].count
        handler_count = evidence_by_stage["user_first_page_fault_handler"].count
        if (page_fault_count, handler_count) not in {(0, 0), (1, 1)}:
            failures.append(
                "console-loss page-fault evidence is not a complete optional pair"
            )
        if diagnostic.page_fault_outcome not in {None, "resolved"}:
            failures.append("console-loss page fault did not resolve")
        if diagnostic.repeated_page_fault:
            failures.append("console-loss reported a repeated page fault")
    elif scenario is BootScenario.STALE_BOOTARGS:
        if profile == GENERIC_SV39:
            failures.append("stale-bootargs requires a Megrez profile")
        if marker_count != 0:
            failures.append("stale-bootargs unexpectedly reached userspace")
        panic_line = (
            "Failed to run the init process: Error { errno: ENOENT, "
            'msg: Some("found a negative dentry") }'
        )
        panic_lines = [line for line in lines if re.search(r"(?i)panic", line)]
        if sum(line.rstrip().endswith("Uncaught panic:") for line in panic_lines) != 1:
            failures.append("missing exact stale-bootargs panic marker")
        if len(panic_lines) != 1:
            failures.append("unexpected additional panic output in stale-bootargs run")
        if sum(line.strip() == panic_line for line in lines) != 1:
            failures.append("missing exact init ENOENT negative-dentry failure")

    forbidden_patterns = (
        r"(?i)Bad Linux RISCV Image magic",
        r"(?i)unexpected exception",
        r"(?i)(instruction|load|store) page fault",
    )
    if scenario is not BootScenario.STALE_BOOTARGS:
        forbidden_patterns = (
            r"(?i)panic",
            r"(?i)\bfatal\b",
            *forbidden_patterns,
        )
    for pattern in forbidden_patterns:
        if re.search(pattern, serial_log):
            failures.append(f"forbidden output matched: {pattern}")
    failures.extend(_marker_event_failures(marker_event, scenario))
    try:
        validate_bdinfo_memory_layout(serial_log, artifacts)
    except ValueError as error:
        failures.append(str(error))
    passed = not failures
    classification = "FAIL"
    if passed:
        if scenario is BootScenario.STALE_BOOTARGS:
            classification = "EXPECTED_INIT_ENOENT"
        elif scenario is BootScenario.FIRST_PROCESS_CONSOLE_LOSS:
            classification = FIRST_PROCESS_CONSOLE_LOSS.classification
        else:
            classification = "PASS"
    return BootAudit(
        booti_command_count=booti_count,
        userspace_marker_count=marker_count,
        effective_bootargs=policy.effective_bootargs,
        diagnostic=diagnostic,
        application_processor_ids=application_processor_ids,
        random_source=random_source,
        classification=classification,
        passed=passed,
        failures=tuple(failures),
    )
