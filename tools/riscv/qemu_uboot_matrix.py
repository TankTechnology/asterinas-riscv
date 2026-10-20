#!/usr/bin/env python3
"""Run the guarded QEMU recovery matrix against prepared payload bundles.

This command deliberately does not prepare artifacts and never retries a failed
leg.  A matrix run is only an orchestration layer around ``qemu_uboot_booti``;
each leg keeps the runner's serial log, marker event, and result JSON in its
own directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence


SCRIPT = Path(__file__).with_name("qemu_uboot_booti.py")


@dataclass(frozen=True)
class ArtifactBundle:
    name: str
    uboot: Path
    boot_disk: Path
    manifest: Path
    dtb_audit: Path


@dataclass(frozen=True)
class MatrixLeg:
    name: str
    profile: str
    device_set: str
    bundle: str


@dataclass(frozen=True)
class LegResult:
    leg: str
    profile: str
    device_set: str
    bundle: str
    attempt: int
    returncode: int
    result_path: str
    runner_stdout_path: str
    runner_stderr_path: str
    status: str
    passed: bool
    failure: str | None
    diagnosis: str | None
    observations: tuple[str, ...]
    receipt: dict[str, object]


MATRIX_LEGS = (
    MatrixLeg(
        "qemu-virt-headless",
        "qemu-virt-asterinas-timer-recovery",
        "headless",
        "qemu-virt",
    ),
    MatrixLeg(
        "qemu-virt-net",
        "qemu-virt-asterinas-timer-recovery",
        "virtio-net-slirp",
        "qemu-virt",
    ),
    MatrixLeg(
        "qemu-virt-xhci",
        "qemu-virt-asterinas-timer-recovery",
        "qemu-xhci",
        "qemu-virt",
    ),
    MatrixLeg(
        "sifive-u-headless",
        "sifive-u-asterinas-timer-recovery",
        "headless",
        "sifive-u",
    ),
    MatrixLeg(
        "sifive-u-gem",
        "sifive-u-gem-timer-recovery",
        "sifive-u-gem-slirp",
        "sifive-u",
    ),
)
MATRIX_LEG_BY_NAME = {leg.name: leg for leg in MATRIX_LEGS}
_QEMU_DEVICE_NAME = re.compile(r'^\s*name "([^"]+)"')
_GMAC_MODEL_TOKENS = ("dwmac", "stmmac", "gmac", "eic7700")
_BOOT_ID_PATTERN = re.compile(
    r"\bboot[_-]?id\s*[=:]\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)
_ROCKOS_LOGIN_PATTERN = re.compile(r"(?mi)^[ \t]*rockos-eswin login:[ \t]*$")
_RECOVERY_MARKER = "ASTERINAS_REBOOT_TIMER_READY"
_SERIAL_RECEIPT_TAIL_BYTES = 4096


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _bundle_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    for suffix in ("uboot", "boot-disk", "manifest", "dtb-audit"):
        parser.add_argument(
            f"--{prefix}-{suffix}",
            type=Path,
            required=True,
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--repeat", type=_positive_int, default=1)
    run.add_argument(
        "--legs",
        default=",".join(leg.name for leg in MATRIX_LEGS),
        help="comma-separated registered leg names (default: all)",
    )
    run.add_argument(
        "--require-fallback",
        choices=("uboot-prompt", "rockos-login"),
        default=None,
        help="fail a leg unless its serial receipt observes this fallback state",
    )
    _bundle_arguments(run, "qemu-virt")
    _bundle_arguments(run, "sifive-u")
    return parser.parse_args(argv)


def _selected_legs(value: str) -> tuple[MatrixLeg, ...]:
    names = tuple(name.strip() for name in value.split(",") if name.strip())
    if not names:
        raise ValueError("at least one matrix leg is required")
    unknown = sorted(set(names) - MATRIX_LEG_BY_NAME.keys())
    if unknown:
        raise ValueError(f"unknown matrix leg: {', '.join(unknown)}")
    if len(names) != len(set(names)):
        raise ValueError("matrix leg names must not repeat")
    return tuple(MATRIX_LEG_BY_NAME[name] for name in names)


def _bundles(args: argparse.Namespace) -> dict[str, ArtifactBundle]:
    return {
        "qemu-virt": ArtifactBundle(
            "qemu-virt",
            args.qemu_virt_uboot,
            args.qemu_virt_boot_disk,
            args.qemu_virt_manifest,
            args.qemu_virt_dtb_audit,
        ),
        "sifive-u": ArtifactBundle(
            "sifive-u",
            args.sifive_u_uboot,
            args.sifive_u_boot_disk,
            args.sifive_u_manifest,
            args.sifive_u_dtb_audit,
        ),
    }


def classify_qemu_device_help(text: str) -> dict[str, object]:
    """Extract network and GMAC model names from QEMU's device inventory."""

    network_models: list[str] = []
    in_network_section = False
    for line in text.splitlines():
        if line.strip() == "Network devices:":
            in_network_section = True
            continue
        if in_network_section and line and not line.startswith("name "):
            in_network_section = False
        match = _QEMU_DEVICE_NAME.match(line)
        if match and in_network_section:
            network_models.append(match.group(1))
    gmac_models = sorted(
        {
            model
            for model in network_models
            if any(token in model.lower() for token in _GMAC_MODEL_TOKENS)
        }
    )
    return {
        "network_models": sorted(set(network_models)),
        "gmac_models": gmac_models,
        "gmac_model_present": bool(gmac_models),
    }


def probe_qemu_capabilities(
    *,
    binary: str = "qemu-system-riscv64",
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, object], str]:
    """Capture the QEMU version and device inventory before a matrix run."""

    version = run_command(
        [binary, "--version"], capture_output=True, text=True, check=False
    )
    devices = run_command(
        [binary, "-device", "help"], capture_output=True, text=True, check=False
    )
    help_text = (devices.stdout or "") + (devices.stderr or "")
    report = classify_qemu_device_help(help_text)
    report.update(
        {
            "binary": binary,
            "version": (version.stdout or version.stderr or "").splitlines()[0]
            if (version.stdout or version.stderr)
            else "unknown",
            "device_help_returncode": devices.returncode,
        }
    )
    return report, help_text


def _command(leg: MatrixLeg, bundle: ArtifactBundle, directory: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "run",
        "--profile",
        leg.profile,
        "--device-set",
        leg.device_set,
        "--uboot",
        str(bundle.uboot),
        "--boot-disk",
        str(bundle.boot_disk),
        "--manifest",
        str(bundle.manifest),
        "--dtb-audit",
        str(bundle.dtb_audit),
        "--serial-log",
        str(directory / "serial.log"),
        "--marker-event",
        str(directory / "marker-event.txt"),
        "--result",
        str(directory / "result.json"),
    ]


def _read_result(path: Path) -> tuple[str, bool, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "MISSING_OR_INVALID", False, "result-unreadable"
    status = str(payload.get("status", "UNKNOWN"))
    passed = payload.get("passed") is True
    session = payload.get("session")
    failure = session.get("failure") if isinstance(session, dict) else None
    return status, passed, failure if isinstance(failure, str) else None


def _load_result(path: Path) -> dict[str, object] | None:
    """Load one runner result for the durable leg receipt."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _classify_fallback_state(serial_text: str) -> str:
    """Distinguish a real RockOS login from a firmware-only fallback.

    QEMU virt bundles do not contain the board's RockOS image.  A U-Boot
    prompt is therefore useful recovery evidence, but must never be reported
    as a RockOS boot.  The login marker is deliberately exact to avoid
    treating arbitrary ``RockOS`` diagnostic text as control access.
    """

    recovery_marker = serial_text.find(_RECOVERY_MARKER)
    if recovery_marker < 0:
        # The initial U-Boot prompt is not evidence of a post-recovery
        # fallback.  Without the guest's recovery marker, keep the state
        # unknown so a quiet or partial boot cannot be promoted to recovery.
        return "unknown"
    post_recovery = serial_text[recovery_marker + len(_RECOVERY_MARKER) :]
    login = _ROCKOS_LOGIN_PATTERN.search(post_recovery)
    if login is not None:
        return "rockos-login"
    if "=> " in post_recovery:
        return "uboot-prompt"
    return "unknown"


def build_leg_receipt(
    *,
    result_payload: dict[str, object] | None,
    serial_text: str,
    returncode: int,
    status: str,
    passed: bool,
    failure: str | None,
    diagnosis: str | None,
) -> dict[str, object]:
    """Summarize serial identity and recovery without hiding raw evidence.

    A missing or malformed result remains ``unknown``. A quiet serial tail is
    retained as evidence, never interpreted as a hardware hang by itself.
    """

    session = result_payload.get("session") if result_payload else None
    session = session if isinstance(session, dict) else {}
    if result_payload is None:
        recovery_state = "unknown"
    elif session.get("recovery_complete") is True:
        recovery_state = "recovered"
    elif (
        (
            isinstance(result_payload.get("validation_scenario"), str)
            and "recovery" in result_payload["validation_scenario"]
        )
        or (failure is not None and failure.startswith("recovery-timeout:"))
    ):
        recovery_state = (
            "not-recovered"
            if failure is not None and failure.startswith("recovery-timeout:")
            else "not-observed"
        )
    else:
        recovery_state = "not-requested"

    boot_ids: list[str] = []
    for match in _BOOT_ID_PATTERN.finditer(serial_text):
        boot_id = match.group(1).lower()
        if boot_id not in boot_ids:
            boot_ids.append(boot_id)
    guest_boot_id = boot_ids[0] if boot_ids else None
    serial_bytes = serial_text.encode("utf-8", errors="replace")
    tail = serial_bytes[-_SERIAL_RECEIPT_TAIL_BYTES:]
    recovery_timer_ready = _RECOVERY_MARKER in serial_text
    return {
        "schema": "asterinas.qemu-uboot-leg-receipt.v1",
        "source": "qemu-serial",
        "execution_scope": "qemu-guest",
        "hardware_state": "not-accessed",
        "status": status,
        "passed": passed,
        "returncode": returncode,
        "failure": failure,
        "diagnosis": diagnosis,
        "recovery_state": recovery_state,
        "fallback_state": _classify_fallback_state(serial_text),
        "watchdog_state": (
            "armed"
            if recovery_timer_ready
            else "missing"
        ),
        "watchdog_kind": (
            "software-recovery-timer" if recovery_timer_ready else "not-observed"
        ),
        "boot_ids": boot_ids,
        "boot_id_state": "observed" if guest_boot_id is not None else "missing",
        "guest_boot_id": guest_boot_id,
        "opensbi_epochs": serial_text.count("OpenSBI v"),
        "uboot_epochs": serial_text.count("U-Boot "),
        "serial_bytes": len(serial_bytes),
        "serial_tail_sha256": hashlib.sha256(tail).hexdigest(),
        "serial_tail": tail.decode("utf-8", errors="replace"),
    }


def diagnose_leg(
    *, failure: str | None, serial_text: str, runner_stderr: str
) -> str | None:
    """Classify common bounded failures without replacing the raw failure."""

    if failure is None:
        return None
    if failure.startswith("recovery-timeout:"):
        stage = failure.split(":", 1)[1]
        return f"recovery-epoch-missing:{stage}"
    if failure == "boot-timeout":
        if "Starting kernel" not in serial_text:
            return "boot-timeout-before-kernel"
        if "ASTERINAS_REBOOT_TIMER_READY" not in serial_text:
            return "recovery-marker-missing-after-kernel"
        return "boot-timeout-after-recovery-marker"
    if failure.startswith("runner-exit:") and runner_stderr:
        return "runner-launch-or-harness-error"
    return None


def classify_serial_observations(leg: MatrixLeg, serial_text: str) -> tuple[str, ...]:
    """Extract bounded device-path observations from a leg's serial log.

    These labels are evidence about the selected QEMU path only.  In
    particular, the SiFive GEM label never claims that an EIC7700 GMAC was
    exercised.
    """

    observations: list[str] = []
    if leg.device_set == "qemu-xhci":
        if "usb: Starting PCI xHCI host:" in serial_text:
            observations.append("xhci-host-started")
        if "usb: PCI xHCI selected:" in serial_text:
            observations.append("xhci-kernel-selected")
    elif leg.device_set == "sifive-u-gem-slirp":
        if (
            "DHCP client bound to address" in serial_text
            and "TFTP from server" in serial_text
        ):
            observations.append("gem-uboot-network-active")
    elif leg.name == "sifive-u-headless":
        if (
            "DHCP client bound to address" not in serial_text
            and "TFTP from server" not in serial_text
        ):
            observations.append("minimal-network-silent")
    if leg.device_set == "headless" and not any(
        marker in serial_text
        for marker in ("usb: PCI xHCI selected:", "usb: Starting PCI xHCI host:")
    ):
        observations.append("usb-disabled")
    if "ASTERINAS_REBOOT_TIMER_READY" in serial_text:
        observations.append("timer-recovery-marker")
    return tuple(observations)


def run_matrix(
    *,
    output: Path,
    bundles: dict[str, ArtifactBundle],
    legs: Sequence[MatrixLeg],
    repeat: int = 1,
    required_fallback: str | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    capabilities: dict[str, object] | None = None,
    capability_text: str | None = None,
) -> tuple[dict[str, object], int]:
    """Run all selected legs and return the durable summary plus exit code."""

    if repeat < 1:
        raise ValueError("repeat must be positive")
    if required_fallback not in (None, "uboot-prompt", "rockos-login"):
        raise ValueError(f"unsupported fallback requirement: {required_fallback}")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("matrix output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    if capability_text is not None:
        (output / "qemu-device-help.txt").write_text(capability_text, encoding="utf-8")
    results: list[LegResult] = []
    for attempt in range(1, repeat + 1):
        for leg in legs:
            bundle = bundles[leg.bundle]
            directory = output / leg.name / f"attempt-{attempt:02d}"
            directory.mkdir(parents=True, exist_ok=False)
            command = _command(leg, bundle, directory)
            try:
                completed = run_command(
                    command, check=False, capture_output=True, text=True
                )
                returncode = completed.returncode
                runner_stdout = getattr(completed, "stdout", "") or ""
                runner_stderr = getattr(completed, "stderr", "") or ""
                if not isinstance(runner_stdout, str):
                    runner_stdout = str(runner_stdout)
                if not isinstance(runner_stderr, str):
                    runner_stderr = str(runner_stderr)
            except OSError as error:
                returncode = 127
                runner_stdout = ""
                runner_stderr = f"{type(error).__name__}: {error}\n"
                (directory / "runner-error.txt").write_text(
                    runner_stderr, encoding="utf-8"
                )
            stdout_path = directory / "runner.stdout.log"
            stderr_path = directory / "runner.stderr.log"
            stdout_path.write_text(runner_stdout, encoding="utf-8")
            stderr_path.write_text(runner_stderr, encoding="utf-8")
            result_path = directory / "result.json"
            status, passed, failure = _read_result(result_path)
            result_payload = _load_result(result_path)
            if returncode != 0 and failure in (None, "result-unreadable"):
                failure = f"runner-exit:{returncode}"
            elif returncode == 0 and status == "MISSING_OR_INVALID":
                failure = "result-unreadable"
            serial_path = directory / "serial.log"
            serial_text = (
                serial_path.read_text(encoding="utf-8", errors="replace")
                if serial_path.is_file()
                else ""
            )
            diagnosis = diagnose_leg(
                failure=failure,
                serial_text=serial_text,
                runner_stderr=runner_stderr,
            )
            observations = classify_serial_observations(leg, serial_text)
            receipt = build_leg_receipt(
                result_payload=result_payload,
                serial_text=serial_text,
                returncode=returncode,
                status=status,
                passed=passed and returncode == 0,
                failure=failure,
                diagnosis=diagnosis,
            )
            if (
                required_fallback is not None
                and receipt["fallback_state"] != required_fallback
            ):
                passed = False
                failure = "fallback-state-mismatch"
                diagnosis = f"fallback-state-required:{required_fallback}"
                receipt["passed"] = False
                receipt["failure"] = failure
                receipt["diagnosis"] = diagnosis
            results.append(
                LegResult(
                    leg=leg.name,
                    profile=leg.profile,
                    device_set=leg.device_set,
                    bundle=leg.bundle,
                    attempt=attempt,
                    returncode=returncode,
                    result_path=str(result_path.relative_to(output)),
                    runner_stdout_path=str(stdout_path.relative_to(output)),
                    runner_stderr_path=str(stderr_path.relative_to(output)),
                    status=status,
                    passed=passed and returncode == 0,
                    failure=failure,
                    diagnosis=diagnosis,
                    observations=observations,
                    receipt=receipt,
                )
            )
    summary = {
        "schema": "asterinas.qemu-uboot-matrix.v6",
        "repeat": repeat,
        "required_fallback": required_fallback,
        "qemu_capabilities": capabilities
        or {"available": False, "reason": "not-probed"},
        "legs": [asdict(result) for result in results],
        "passed": all(result.passed for result in results),
    }
    summary_path = output / "matrix.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary, 0 if summary["passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command != "run":
        raise AssertionError(f"unhandled command: {args.command}")
    try:
        legs = _selected_legs(args.legs)
        bundles = _bundles(args)
        if shutil.which("qemu-system-riscv64") is None:
            raise ValueError(
                "qemu-system-riscv64 is unavailable; run this target in the "
                "persistent Asterinas development container"
            )
        capabilities, capability_text = probe_qemu_capabilities()
        if capabilities["device_help_returncode"] != 0:
            raise ValueError("qemu-system-riscv64 could not enumerate devices")
        summary, exit_code = run_matrix(
            output=args.output,
            bundles=bundles,
            legs=legs,
            repeat=args.repeat,
            required_fallback=args.require_fallback,
            capabilities=capabilities,
            capability_text=capability_text,
        )
    except ValueError as error:
        print(f"qemu U-Boot matrix: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
