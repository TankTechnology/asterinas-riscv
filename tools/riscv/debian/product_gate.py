#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the active QEMU product gates against supplied Debian artifacts.

The direct gate is mandatory. An ext4 image enables the JBD2 write/crash gate,
and a DSH image enables the optional agent probe inside the direct gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DIRECT_GATE = REPO_ROOT / "tools/riscv/debian/direct_boot_gate.py"
EXT4_GATE = REPO_ROOT / "tools/riscv/debian/ext4_gate.py"


class ProductGateError(RuntimeError):
    """A product component did not produce a passing receipt."""


def build_direct_command(
    kernel: Path,
    rootfs: Path,
    *,
    dsh_image: Path | None,
    rootfstype: str | None,
    log: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(DIRECT_GATE),
        "--kernel",
        str(kernel),
        "--rootfs",
        str(rootfs),
        "--log",
        str(log),
    ]
    if dsh_image is not None:
        command.extend(["--dsh-image", str(dsh_image)])
    if rootfstype is not None:
        command.extend(["--rootfstype", rootfstype])
    return command


def build_ext4_command(
    kernel: Path,
    rootfs: Path,
    *,
    log_dir: Path,
    fault_stages: str,
) -> list[str]:
    return [
        sys.executable,
        str(EXT4_GATE),
        "--kernel",
        str(kernel),
        "--rootfs",
        str(rootfs),
        "--log-dir",
        str(log_dir),
        "--fault-stages",
        fault_stages,
    ]


def _kill_matching_guests(markers: tuple[str, ...]) -> None:
    """Best-effort cleanup after a gate was force-killed with its QEMU child."""

    needles = [marker.encode() for marker in markers if marker]
    if not needles:
        return
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"qemu-system-riscv64" not in cmdline:
            continue
        if not any(needle in cmdline for needle in needles):
            continue
        try:
            os.kill(int(entry.name), signal.SIGKILL)
        except OSError:
            pass


def _run_component_process(
    command: list[str], *, timeout: float, markers: tuple[str, ...]
) -> tuple[int, str, str, bool]:
    """Run one gate, cleaning up its QEMU child if the aggregate times out."""

    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # SIGINT lets the gate's finally block terminate its own QEMU process.
        try:
            os.killpg(process.pid, signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            stdout, stderr = process.communicate()
            _kill_matching_guests(markers)
    return process.returncode, stdout, stderr, timed_out


def run_component(
    name: str,
    command: list[str],
    *,
    receipt: Path,
    log: Path,
    timeout: float,
    markers: tuple[str, ...] = (),
) -> dict[str, object]:
    """Run one gate and require its JSON receipt to report a pass."""

    returncode, stdout, stderr, timed_out = _run_component_process(
        command, timeout=timeout, markers=markers
    )
    status = f"timed out after {timeout:.0f}s" if timed_out else "completed"
    log.write_text(
        f"$ {' '.join(command)}\n"
        f"status: {status}\n\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}\n",
        encoding="utf-8",
    )
    if timed_out:
        raise ProductGateError(f"{name} timed out after {timeout:.0f}s; see {log}")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ProductGateError(
            f"{name} did not emit a JSON receipt; see {log}"
        ) from error
    if not isinstance(result, dict):
        raise ProductGateError(f"{name} emitted a non-object JSON receipt; see {log}")
    receipt.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if returncode != 0 or result.get("status") != "pass":
        raise ProductGateError(
            f"{name} failed: {result.get('error', 'see receipt')}; see {log}"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--ext4-rootfs", type=Path)
    parser.add_argument("--dsh-image", type=Path)
    parser.add_argument("--direct-rootfstype", choices=("ext2", "ext4"))
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("target/riscv-debian-product"),
    )
    parser.add_argument("--fault-stages", default="1,2,3,4")
    parser.add_argument("--timeout", type=float, default=1800.0)
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def validate_inputs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    for name in ("kernel", "rootfs", "ext4_rootfs", "dsh_image"):
        path = getattr(args, name)
        if path is not None and not path.is_file():
            parser.error(f"{name.replace('_', '-')} not found: {path}")
    if shutil.which("qemu-system-riscv64") is None:
        parser.error("qemu-system-riscv64 not found")
    if args.ext4_rootfs is not None and shutil.which("e2fsck") is None:
        parser.error("e2fsck not found")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")


def run_product(args: argparse.Namespace) -> dict[str, object]:
    started = time.monotonic()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    components: dict[str, object] = {}

    direct = run_component(
        "direct",
        build_direct_command(
            args.kernel,
            args.rootfs,
            dsh_image=args.dsh_image,
            rootfstype=args.direct_rootfstype,
            log=args.log_dir / "direct-serial.log",
        ),
        receipt=args.log_dir / "direct-gate.json",
        log=args.log_dir / "direct-gate.log",
        timeout=args.timeout,
        markers=(str(args.kernel), str(args.rootfs)),
    )
    components["direct"] = direct

    if args.ext4_rootfs is not None:
        ext4 = run_component(
            "ext4",
            build_ext4_command(
                args.kernel,
                args.ext4_rootfs,
                log_dir=args.log_dir / "ext4",
                fault_stages=args.fault_stages,
            ),
            receipt=args.log_dir / "ext4-gate.json",
            log=args.log_dir / "ext4-gate.log",
            timeout=args.timeout,
            markers=(str(args.kernel),),
        )
        components["ext4"] = ext4

    return {
        "status": "pass",
        "kernel": str(args.kernel),
        "rootfs": str(args.rootfs),
        "ext4_rootfs": str(args.ext4_rootfs) if args.ext4_rootfs else None,
        "dsh_image": str(args.dsh_image) if args.dsh_image else None,
        "direct_rootfstype": args.direct_rootfstype,
        "components": components,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "log_dir": str(args.log_dir),
    }


def write_result(log_dir: Path, result: dict[str, object]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "result.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_inputs(args, parser)
    try:
        result = run_product(args)
    except (ProductGateError, OSError) as error:
        result = {
            "status": "fail",
            "error": str(error),
            "kernel": str(args.kernel),
            "rootfs": str(args.rootfs),
            "ext4_rootfs": str(args.ext4_rootfs) if args.ext4_rootfs else None,
            "dsh_image": str(args.dsh_image) if args.dsh_image else None,
            "direct_rootfstype": args.direct_rootfstype,
            "log_dir": str(args.log_dir),
        }
        write_result(args.log_dir, result)
        print(json.dumps(result, indent=2))
        return 1
    write_result(args.log_dir, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
