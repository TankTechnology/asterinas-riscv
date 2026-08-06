"""Bind prepared U-Boot runs to fixed scenarios and immutable evidence."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path

from megrez_contract import artifact_identity
from qemu_uboot_artifacts import ArtifactExpectations, verify_boot_disk_artifacts
from qemu_uboot_audit import BootAudit, memory_layout_observer
from qemu_uboot_commands import (
    USERSPACE_MARKER,
    BootScenario,
    boot_commands,
)
from qemu_uboot_execution_io import open_execution_workspace
from qemu_uboot_profiles import (
    GENERIC_SV39,
    AuditPolicy,
    BootActionKind,
    Fidelity,
    QemuUbootProfile,
    ResultScope,
)
from qemu_uboot_secure_io import PinnedPublication
from qemu_uboot_session import SerialInteraction, SessionResult
from qemu_uboot_variants import (
    FIRST_PROCESS_CONSOLE_LOSS,
    QemuUbootVariant,
    effective_bootargs as variant_effective_bootargs,
)


class RunStatus(str, Enum):
    """External test status, aligned with Linux test-harness semantics."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIP = "SKIP"


class TerminalClassification(str, Enum):
    """Whether a registered success terminal was complete or probe-scoped."""

    BOOT_COMPLETED = "BOOT_COMPLETED"
    PROBE_COMPLETED = "PROBE_COMPLETED"
    EXPECTED_NEGATIVE = "EXPECTED_NEGATIVE"
    INCOMPLETE = "INCOMPLETE"


def _run_status(passed: bool, session: SessionResult) -> RunStatus:
    """Separate a guest/contract failure from a harness failure."""

    if passed:
        return RunStatus.PASS
    if not session.cleanup_complete or (session.failure or "").startswith(
        (
            "process-error:",
            "cleanup-error:",
            "serial-log-cleanup:",
            "selector-cleanup:",
            "process-group-cleanup:",
            "pipe-cleanup:",
        )
    ):
        return RunStatus.ERROR
    return RunStatus.FAIL


def ktap_summary(result: PreparedRunResult) -> str:
    """Render one durable run result as a compact KTAP document."""

    passed = result.status == RunStatus.PASS.value
    outcome = "ok" if passed else "not ok"
    directive = " # TIMEOUT" if result.session.timed_out else ""
    lines = (
        "KTAP version 1",
        "1..1",
        f"{outcome} 1 - {result.profile}{directive}",
        f"# scope: {result.scope}",
        f"# fidelity: {result.fidelity}",
        f"# expected-terminal: {result.expected_terminal}",
        f"# terminal-classification: {result.terminal_classification}",
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ExecutionDependencies:
    """Patchable host operations used by one prepared execution."""

    load_artifact_manifest: Callable[..., ArtifactExpectations]
    verify_prepared_dtb: Callable[..., Any]
    qemu_argv: Callable[..., list[str]]
    qemu_version: Callable[..., str]
    run_serial_session: Callable[..., SessionResult]
    audit_serial_log: Callable[..., BootAudit]


@dataclass(frozen=True)
class PreparedRunResult:
    """Evidence summary for one prepared QEMU U-Boot run."""

    profile: str
    machine: str
    boot_flow: str
    validation_scenario: str
    fidelity: str
    scope: str
    expected_terminal: str
    handoff_observability: str
    machine_provenance: str
    scenario: str
    variant: str | None
    effective_bootargs: str
    artifacts: ArtifactExpectations
    session: SessionResult
    audit: BootAudit
    qemu_argv: tuple[str, ...]
    qemu_version: str
    opensbi_version: str | None
    uboot_version: str | None
    dtb_sha256: str | None
    source_dtb_sha256_before: str | None
    source_dtb_sha256_after: str | None
    payload_dtb_sha256_before: str | None
    payload_dtb_sha256_after: str | None
    boot_disk_sha256_before: str
    boot_disk_sha256_after: str
    uboot_sha256_before: str
    uboot_sha256_after: str
    manifest_sha256_before: str
    manifest_sha256_after: str
    status: str
    terminal_classification: str
    passed: bool


def _execution_contract(
    profile: QemuUbootProfile,
    scenario: BootScenario,
    variant: QemuUbootVariant | None,
    source_dtb: Path | None,
    variant_audit: Path | None,
    bootargs_override: str | None,
) -> tuple[str, bytes, bool]:
    """Validate one fixed scenario and return bootargs, completion, snapshot."""

    materials = (variant, source_dtb, variant_audit)
    if scenario is BootScenario.FIRST_PROCESS_CONSOLE_LOSS:
        if any(item is None for item in materials):
            raise ValueError(
                "console-loss requires variant, source DTB, and variant audit"
            )
        assert variant is not None
        effective = variant_effective_bootargs(profile, variant)
        return effective, variant.completion_line, True
    if any(item is not None for item in materials):
        raise ValueError("variant materials require first-process-console-loss")
    if bootargs_override is not None and scenario is not BootScenario.POSITIVE:
        raise ValueError("bootargs override requires the positive scenario")
    if scenario is BootScenario.REGISTERED_CONSOLE_SUPPRESSION:
        effective = variant_effective_bootargs(profile, FIRST_PROCESS_CONSOLE_LOSS)
    else:
        if scenario is BootScenario.STALE_BOOTARGS and profile == GENERIC_SV39:
            raise ValueError("stale-bootargs requires a Megrez profile")
        effective = (
            profile.bootargs if bootargs_override is None else bootargs_override
        )
    return effective, profile.validation.completion_line, False


def _prepared_identities(
    *,
    dependencies: ExecutionDependencies,
    boot_disk: Path,
    uboot: Path,
    manifest: Path,
    dtb_audit: Path | None,
    profile: QemuUbootProfile,
    artifacts: ArtifactExpectations,
    variant: QemuUbootVariant | None,
    source_dtb: Path | None,
    variant_audit: Path | None,
) -> tuple[str | None, str | None, str, str, str]:
    payload = (
        dependencies.verify_prepared_dtb(
            boot_disk=boot_disk,
            audit_path=dtb_audit,
            profile=profile,
            expected_size=artifacts.dtb_size,
            expected_crc32=artifacts.dtb_crc32,
            variant=variant,
            variant_audit_path=variant_audit,
            source_dtb=source_dtb,
        )
        if dtb_audit is not None
        else None
    )
    try:
        source_sha = artifact_identity(source_dtb).sha256 if source_dtb else None
        disk_sha = artifact_identity(boot_disk).sha256
        uboot_sha = artifact_identity(uboot).sha256
        manifest_sha = artifact_identity(manifest).sha256
    except OSError as error:
        raise ValueError("prepared identity input is not an existing file") from error
    return (
        source_sha,
        payload.sha256 if payload is not None else None,
        disk_sha,
        uboot_sha,
        manifest_sha,
    )


def _firmware_versions(serial_text: str) -> tuple[str | None, str | None]:
    """Extract the first observed OpenSBI and U-Boot version lines."""

    lines = serial_text.replace("\r", "").splitlines()
    opensbi = next((line.strip() for line in lines if "OpenSBI v" in line), None)
    uboot = next((line.strip() for line in lines if line.startswith("U-Boot ")), None)
    return opensbi, uboot


def _marker_event_text(session: SessionResult) -> str:
    lines = [
        f"marker_seen={'yes' if session.marker_seen else 'no'}",
        f"action={session.termination_action}",
        f"cleanup_complete={'yes' if session.cleanup_complete else 'no'}",
    ]
    if session.failure is not None:
        lines.append(f"failure={session.failure}")
    return "\n".join(lines) + "\n"


def _best_effort_close(publication: PinnedPublication | None) -> None:
    """Release a redundant FD after its evidence decision is already durable."""

    if publication is None:
        return
    try:
        publication.close()
    except OSError:
        pass


def execute_prepared(
    *,
    dependencies: ExecutionDependencies,
    uboot: Path,
    boot_disk: Path,
    manifest: Path,
    serial_log: Path,
    marker_event: Path,
    result_path: Path,
    startup_timeout: float,
    command_timeout: float,
    boot_timeout: float,
    termination_grace: float,
    profile: QemuUbootProfile,
    scenario: BootScenario,
    dtb_audit: Path | None,
    variant: QemuUbootVariant | None,
    source_dtb: Path | None,
    variant_audit: Path | None,
    bootargs_override: str | None = None,
    serial_interaction: SerialInteraction | None = None,
) -> PreparedRunResult:
    """Execute and serialize one profile-validated prepared run."""

    if profile.validation.audit_policy is AuditPolicy.REGISTERED_MILESTONES and (
        scenario is not BootScenario.POSITIVE
        or variant is not None
        or source_dtb is not None
        or variant_audit is not None
        or bootargs_override is not None
        or serial_interaction is not None
    ):
        raise ValueError("registered-milestone scenario rejects runtime overrides")
    effective, completion_line, snapshot_disk = _execution_contract(
        profile,
        scenario,
        variant,
        source_dtb,
        variant_audit,
        bootargs_override,
    )
    if profile.fidelity is not Fidelity.VIRTUAL_PLATFORM and dtb_audit is None:
        raise ValueError("a generated DTB audit is required for this machine contract")
    result_publication: PinnedPublication | None = None
    revocation_publication: PinnedPublication | None = None
    revoked_payload: bytes | None = None
    try:
        with open_execution_workspace(
            uboot=uboot,
            boot_disk=boot_disk,
            manifest=manifest,
            dtb_audit=dtb_audit,
            source_dtb=source_dtb,
            variant_audit=variant_audit,
            serial_log=serial_log,
            marker_event=marker_event,
            result_path=result_path,
        ) as workspace:
            staged = workspace.staged
            artifacts = dependencies.load_artifact_manifest(staged.manifest)
            if all(
                getattr(artifacts, name) is not None
                for name in ("kernel_sha256", "dtb_sha256", "initrd_sha256")
            ):
                artifacts = verify_boot_disk_artifacts(
                    boot_disk=staged.boot_disk,
                    dtb_filename=profile.machine.dtb_filename,
                    expected=artifacts,
                )
            identity_arguments = {
                "dependencies": dependencies,
                "boot_disk": staged.boot_disk,
                "uboot": staged.uboot,
                "manifest": staged.manifest,
                "dtb_audit": staged.dtb_audit,
                "profile": profile,
                "artifacts": artifacts,
                "variant": variant,
                "source_dtb": staged.source_dtb,
                "variant_audit": staged.variant_audit,
            }
            before = _prepared_identities(**identity_arguments)
            qemu_arguments = dependencies.qemu_argv(
                uboot=staged.uboot,
                boot_disk=staged.boot_disk,
                profile=profile,
                snapshot_disk=snapshot_disk,
            )
            version = dependencies.qemu_version(qemu_arguments[0])
            commands = boot_commands(
                artifacts,
                profile=profile,
                scenario=scenario,
                variant=variant,
                bootargs_override=bootargs_override,
            )
            with workspace.capture_serial() as (serial_capture_path, serial_capture):
                session = dependencies.run_serial_session(
                    qemu_arguments,
                    commands=commands,
                    raw_log_path=serial_capture_path,
                    raw_log_file=serial_capture,
                    startup_timeout=startup_timeout,
                    command_timeout=command_timeout,
                    boot_timeout=boot_timeout,
                    termination_grace=termination_grace,
                    command_observer=memory_layout_observer(artifacts),
                    completion_line=completion_line,
                    post_terminal_timeout=profile.validation.post_terminal_timeout,
                    milestone_expectations=(
                        profile.validation.milestones
                        if scenario is BootScenario.POSITIVE
                        and serial_interaction is None
                        and BootActionKind.EXPECT_MILESTONE
                        in profile.boot_flow.actions
                        else ()
                    ),
                    serial_interaction=serial_interaction,
                )
                serial_capture.flush()
                os.fsync(serial_capture.fileno())
                serial_capture.seek(0)
                serial_payload = serial_capture.read()

            serial_identity = workspace.publish_evidence("serial_log", serial_payload)
            serial_text = serial_payload.decode(errors="replace")
            marker_text = _marker_event_text(session)
            marker_identity = workspace.publish_evidence(
                "marker_event",
                marker_text.encode(),
            )
            userspace_marker = (
                completion_line
                if serial_interaction is None
                else serial_interaction.completion_line
            ).decode(errors="replace")
            readiness_marker = (
                None
                if serial_interaction is None
                else serial_interaction.ready_line.decode(errors="replace")
            )
            audit = dependencies.audit_serial_log(
                serial_text,
                marker_event=marker_text,
                artifacts=artifacts,
                profile=profile,
                scenario=scenario,
                variant=variant,
                userspace_marker=userspace_marker,
                readiness_marker=readiness_marker,
                bootargs_override=bootargs_override,
            )
            after = _prepared_identities(**identity_arguments)
            if before != after:
                raise ValueError("a prepared input changed during the run")
            workspace.verify_and_cleanup_staging(
                serial_identity=serial_identity,
                marker_identity=marker_identity,
            )

            passed = (
                session.booti_sent_count == 1
                and session.cleanup_complete
                and audit.passed
            )
            if scenario is BootScenario.STALE_BOOTARGS:
                passed = passed and not session.marker_seen and session.timed_out
                passed = passed and session.failure == "boot-timeout"
            else:
                passed = passed and session.marker_seen and not session.timed_out
                passed = passed and session.failure is None
            passed = passed and audit.effective_bootargs == effective
            if passed and scenario is not BootScenario.POSITIVE:
                terminal_classification = TerminalClassification.EXPECTED_NEGATIVE
            elif passed and profile.validation.scope is ResultScope.COMPLETE_BOOT:
                terminal_classification = TerminalClassification.BOOT_COMPLETED
            elif passed:
                terminal_classification = TerminalClassification.PROBE_COMPLETED
            else:
                terminal_classification = TerminalClassification.INCOMPLETE
            opensbi_version, uboot_version = _firmware_versions(serial_text)
            result = PreparedRunResult(
                profile=profile.name,
                machine=profile.machine.name,
                boot_flow=profile.boot_flow.name,
                validation_scenario=profile.validation.name,
                fidelity=profile.fidelity.value,
                scope=profile.validation.scope.value,
                expected_terminal=profile.validation.terminal.value,
                handoff_observability=(
                    "entry registers and satp rely on reviewed U-Boot booti; "
                    "the early-kernel marker is indirect handoff evidence"
                ),
                machine_provenance=profile.machine.provenance,
                scenario=scenario.value,
                variant=variant.name if variant else None,
                effective_bootargs=effective,
                artifacts=artifacts,
                session=session,
                audit=audit,
                qemu_argv=tuple(qemu_arguments),
                qemu_version=version,
                opensbi_version=opensbi_version,
                uboot_version=uboot_version,
                dtb_sha256=before[1],
                source_dtb_sha256_before=before[0],
                source_dtb_sha256_after=after[0],
                payload_dtb_sha256_before=before[1],
                payload_dtb_sha256_after=after[1],
                boot_disk_sha256_before=before[2],
                boot_disk_sha256_after=after[2],
                uboot_sha256_before=before[3],
                uboot_sha256_after=after[3],
                manifest_sha256_before=before[4],
                manifest_sha256_after=after[4],
                status=_run_status(passed, session).value,
                terminal_classification=terminal_classification.value,
                passed=passed,
            )
            result_payload = (
                json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
            ).encode()
            revoked_payload = (
                json.dumps(
                    asdict(
                        replace(
                            result,
                            passed=False,
                            status=RunStatus.ERROR.value,
                            terminal_classification=(
                                TerminalClassification.INCOMPLETE.value
                            ),
                        )
                    ),
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            with workspace.prepare_result(result_payload) as prepared_result:
                # Retain both inode handles before rename or parent fsync can fail
                # after exposing passed=true; never reopen an attacker-swapped path.
                result_publication = prepared_result.retain()
                revocation_publication = result_publication.duplicate()
                workspace.publish_result(prepared_result)
                workspace.sync_result()
                workspace.verify_after_result(
                    serial_identity=serial_identity,
                    marker_identity=marker_identity,
                    result_identity=result_publication.identity,
                )
    except BaseException:
        if result_publication is not None:
            assert revoked_payload is not None
            try:
                (revocation_publication or result_publication).overwrite(
                    revoked_payload
                )
            finally:
                _best_effort_close(result_publication)
                _best_effort_close(revocation_publication)
        raise
    assert result_publication is not None
    assert revocation_publication is not None
    assert revoked_payload is not None
    try:
        result_publication.close()
    except BaseException:
        try:
            revocation_publication.overwrite(revoked_payload)
        finally:
            _best_effort_close(revocation_publication)
        raise
    _best_effort_close(revocation_publication)
    return result
