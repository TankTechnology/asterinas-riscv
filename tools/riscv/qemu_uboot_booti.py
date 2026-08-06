#!/usr/bin/env python3
"""Drive a guarded U-Boot ``booti`` validation under QEMU."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from megrez_contract import load_contract
from qemu_uboot_artifacts import (
    ArtifactExpectations as ArtifactExpectations,
    artifact_expectations_from_paths,
    load_artifact_manifest,
)
from qemu_uboot_artifacts import (
    validate_fixed_payload_layout as validate_fixed_payload_layout,
)
from qemu_uboot_audit import BootAudit as BootAudit
from qemu_uboot_audit import (
    audit_serial_log,
    memory_layout_observer as memory_layout_observer,
)
from qemu_uboot_commands import (
    BootCommand as BootCommand,
    BootScenario as BootScenario,
    boot_commands,
    qemu_argv,
    qemu_version,
)
from qemu_uboot_dtb import verify_prepared_dtb
from qemu_uboot_execution import (
    ExecutionDependencies,
    PreparedRunResult,
    execute_prepared,
    ktap_summary,
)
from qemu_uboot_profiles import (
    GENERIC_SV39,
    Fidelity,
    QemuUbootProfile,
    profile_by_name,
    validate_registered_profile,
    validate_profile_policy,
)
from qemu_uboot_session import (
    SerialInteraction as SerialInteraction,
    SessionResult as SessionResult,
    run_serial_session,
)
from qemu_uboot_shell import interaction_by_name
from qemu_uboot_variants import QemuUbootVariant, variant_by_name


def run_prepared(
    *,
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
    profile: QemuUbootProfile = GENERIC_SV39,
    scenario: BootScenario = BootScenario.POSITIVE,
    dtb_audit: Path | None = None,
    variant: QemuUbootVariant | None = None,
    source_dtb: Path | None = None,
    variant_audit: Path | None = None,
    bootargs_override: str | None = None,
    serial_interaction: SerialInteraction | None = None,
) -> PreparedRunResult:
    """Run and audit one prepared U-Boot boot using immutable evidence."""
    run_arguments = dict(locals())
    _validate_runnable_profile(profile)
    dependencies = ExecutionDependencies(
        load_artifact_manifest=load_artifact_manifest,
        verify_prepared_dtb=verify_prepared_dtb,
        qemu_argv=qemu_argv,
        qemu_version=qemu_version,
        run_serial_session=run_serial_session,
        audit_serial_log=audit_serial_log,
    )
    return execute_prepared(dependencies=dependencies, **run_arguments)


def _validate_runnable_profile(profile: QemuUbootProfile) -> None:
    validate_registered_profile(profile)
    if profile.fidelity is Fidelity.CONTRACT_APPROXIMATION:
        validate_profile_policy(load_contract(), profile)


def _profile_argument(value: str) -> QemuUbootProfile:
    try:
        return profile_by_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _scenario_argument(value: str) -> BootScenario:
    try:
        return BootScenario(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"unknown boot scenario: {value}") from error


def _serial_interaction_argument(value: str) -> SerialInteraction:
    try:
        return interaction_by_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _positive_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    field_parser = subparsers.add_parser("profile-field")
    field_parser.add_argument("--profile", type=_profile_argument, required=True)
    field_parser.add_argument(
        "--field",
        choices=(
            "qemu-machine", "storage-transport", "dtb-filename", "uboot-defconfig",
            "uboot-binary", "uboot-build-mode",
        ),
        required=True,
    )
    print_parser = subparsers.add_parser("print-commands")
    print_parser.add_argument("--profile", type=_profile_argument, default=GENERIC_SV39)
    print_parser.add_argument(
        "--scenario", type=_scenario_argument, default=BootScenario.POSITIVE
    )
    print_parser.add_argument("--variant", type=variant_by_name, default=None)
    print_parser.add_argument("--bootargs-override")
    manifest_parser = subparsers.add_parser("write-manifest")
    for name in ("kernel", "dtb", "initrd", "output"):
        manifest_parser.add_argument(f"--{name}", type=Path, required=True)
    run_parser = subparsers.add_parser("run")
    for name in (
        "uboot",
        "boot-disk",
        "manifest",
        "serial-log",
        "marker-event",
        "result",
    ):
        run_parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("dtb-audit", "source-dtb", "variant-audit"):
        run_parser.add_argument(f"--{name}", type=Path)
    run_parser.add_argument("--variant", type=variant_by_name)
    run_parser.add_argument("--profile", type=_profile_argument, default=GENERIC_SV39)
    run_parser.add_argument(
        "--scenario", type=_scenario_argument, default=BootScenario.POSITIVE
    )
    run_parser.add_argument("--serial-interaction", type=_serial_interaction_argument)
    run_parser.add_argument("--bootargs-override")
    for name, default in (
        ("startup", None),
        ("command", None),
        ("boot", None),
        ("termination-grace", 2.0),
    ):
        option = name if name == "termination-grace" else f"{name}-timeout"
        run_parser.add_argument(
            f"--{option}", type=_positive_finite_float, default=default
        )
    args = parser.parse_args(argv)
    if args.command == "run":
        for name in ("startup", "command", "boot"):
            attribute = f"{name}_timeout"
            if getattr(args, attribute) is None:
                setattr(args, attribute, getattr(args.profile.validation, attribute))
        materials = (args.variant, args.source_dtb, args.variant_audit)
        is_console_loss = args.scenario is BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        if (is_console_loss and not all(materials)) or (
            not is_console_loss and any(materials)
        ):
            parser.error(
                "variant, source DTB, and variant audit are required together only for console-loss"
            )
        if (
            args.bootargs_override is not None
            and args.scenario is not BootScenario.POSITIVE
        ):
            parser.error("bootargs override requires the positive scenario")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "profile-field":
        _validate_runnable_profile(args.profile)
        values = {
            "qemu-machine": args.profile.machine.qemu_machine.value,
            "storage-transport": args.profile.machine.storage_transport.value,
            "dtb-filename": args.profile.machine.dtb_filename,
            "uboot-defconfig": args.profile.machine.uboot_defconfig,
            "uboot-binary": args.profile.machine.uboot_binary,
            "uboot-build-mode": args.profile.machine.uboot_build_mode.value,
        }
        print(values[args.field])
        return 0
    if args.command == "print-commands":
        _validate_runnable_profile(args.profile)
        for command in boot_commands(
            profile=args.profile,
            scenario=args.scenario,
            variant=args.variant,
            bootargs_override=args.bootargs_override,
        ):
            print(command.text)
        return 0
    if args.command == "write-manifest":
        artifacts = artifact_expectations_from_paths(
            kernel=args.kernel, dtb=args.dtb, initrd=args.initrd
        )
        args.output.write_text(
            json.dumps(asdict(artifacts), indent=2, sort_keys=True) + "\n"
        )
        return 0
    if args.command == "run":
        run_args = vars(args).copy()
        del run_args["command"]
        run_args["result_path"] = run_args.pop("result")
        result = run_prepared(**run_args)
        print(ktap_summary(result), end="")
        print(f"result={args.result}")
        return 0 if result.passed else 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
