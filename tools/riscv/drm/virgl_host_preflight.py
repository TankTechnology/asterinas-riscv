#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed host checks for the QEMU virgl launch contract.

Listing a GL device in a Python configuration is not evidence that the
selected QEMU can provide one.  This small preflight checks the executable that
will actually be launched and records the two capabilities required by the
3D gate: an EGL headless display backend and the virtio-gpu GL device.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class VirglHostPreflightResult:
    passed: bool
    reason: str
    qemu: str
    version: str = ""
    has_egl_headless: bool = False
    has_virtio_gpu_gl: bool = False


@dataclass(frozen=True)
class VirglRuntimeSmokeResult:
    passed: bool
    reason: str
    qemu: str
    timed_out: bool = False
    returncode: int | None = None
    stderr: str = ""


def _run_help(qemu: str, *arguments: str) -> tuple[str, str, int]:
    try:
        completed = subprocess.run(
            [qemu, *arguments],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return "", f"{type(error).__name__}: {error}", 127
    return completed.stdout, completed.stderr, completed.returncode


def inspect_qemu(qemu: str | Path) -> VirglHostPreflightResult:
    executable = str(qemu)
    version_out, version_err, version_rc = _run_help(executable, "--version")
    if version_rc != 0:
        detail = (version_err or version_out).strip() or f"exit {version_rc}"
        return VirglHostPreflightResult(False, f"qemu --version failed: {detail}", executable)

    display_out, display_err, display_rc = _run_help(executable, "-display", "help")
    device_out, device_err, device_rc = _run_help(executable, "-device", "help")
    display = f"{display_out}\n{display_err}"
    devices = f"{device_out}\n{device_err}"
    has_egl = "egl-headless" in display
    has_gl_device = "virtio-gpu-gl-device" in devices
    version = version_out.splitlines()[0].strip() if version_out.splitlines() else ""
    if display_rc != 0 or device_rc != 0:
        return VirglHostPreflightResult(
            False,
            "QEMU help query failed",
            executable,
            version,
            has_egl,
            has_gl_device,
        )
    if not has_egl:
        return VirglHostPreflightResult(
            False,
            "QEMU lacks egl-headless display backend",
            executable,
            version,
            False,
            has_gl_device,
        )
    if not has_gl_device:
        return VirglHostPreflightResult(
            False,
            "QEMU lacks virtio-gpu-gl-device",
            executable,
            version,
            True,
            False,
        )
    return VirglHostPreflightResult(
        True,
        "passed",
        executable,
        version,
        True,
        True,
    )


def run_runtime_smoke(
    qemu: str | Path, *, timeout: float = 5.0,
    environment: dict[str, str] | None = None,
) -> VirglRuntimeSmokeResult:
    """Start a GL-capable QEMU without a guest and require it to stay alive.

    The help-text preflight is deliberately separate from this check: a build
    can advertise `egl-headless` and `virtio-gpu-gl-device` while its runtime
    EGL module or virgl renderer is missing. `-S` prevents a guest from
    running; the timeout is the successful outcome because QEMU should remain
    alive waiting for a monitor connection.
    """

    executable = str(qemu)
    if timeout <= 0:
        raise ValueError("runtime smoke timeout must be positive")
    argv = [
        executable,
        "-machine", "virt",
        "-bios", "none",
        "-m", "256M",
        "-nodefaults",
        # Do not pass -nographic here: it replaces the selected EGL backend
        # with `none` late in QEMU option processing. Null serial/monitor keep
        # the smoke headless without disabling the GL display.
        "-display",
        "egl-headless,rendernode="
        + os.environ.get("ASTERINAS_QEMU_RENDER_NODE", "/dev/dri/renderD129"),
        "-device", "virtio-gpu-gl-device",
        "-S",
        "-serial", "null",
        "-monitor", "none",
    ]

    def cleanup_orphaned_container_qemu() -> None:
        # When the selected launcher uses `docker exec`, killing the exec
        # client does not necessarily reap the QEMU child. Reap only this
        # no-guest smoke shape; a real guest launch has neither `-bios none`
        # nor this exact argument prefix and is left untouched.
        container = env.get("ASTERINAS_QEMU_GL_CONTAINER")
        if not container:
            return
        pattern = (
            "qemu-system-riscv64 -machine virt -bios none -m 256M "
            "-nodefaults -display egl-headless,rendernode="
        )
        try:
            subprocess.run(
                [
                    "docker", "exec", container, "sh", "-lc",
                    f"pkill -TERM -f {pattern!r} 2>/dev/null || true; sleep 0.1; "
                    f"pkill -KILL -f {pattern!r} 2>/dev/null || true",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    env = os.environ.copy()
    if environment:
        env.update(environment)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        cleanup_orphaned_container_qemu()
        stderr = error.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return VirglRuntimeSmokeResult(
            True,
            f"QEMU GL runtime stayed alive for {timeout:.1f}s",
            executable,
            timed_out=True,
            stderr=stderr[-4096:],
        )
    except (OSError, subprocess.SubprocessError) as error:
        cleanup_orphaned_container_qemu()
        return VirglRuntimeSmokeResult(
            False,
            f"QEMU GL runtime could not start: {type(error).__name__}: {error}",
            executable,
        )

    stderr = (completed.stderr or "")[-4096:]
    detail = stderr.strip() or f"exit {completed.returncode}"
    return VirglRuntimeSmokeResult(
        False,
        f"QEMU GL runtime exited before the smoke deadline: {detail}",
        executable,
        returncode=completed.returncode,
        stderr=stderr,
    )


def _parse_args(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", default="qemu-system-riscv64", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--runtime-smoke", action="store_true",
        help="also start a no-guest GL runtime smoke (timeout means pass)",
    )
    parser.add_argument("--runtime-timeout", type=float, default=5.0)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parse_args(arguments)
    result = inspect_qemu(args.qemu)
    payload: dict[str, object] = {"capability": asdict(result)}
    if args.runtime_smoke and result.passed:
        smoke = run_runtime_smoke(args.qemu, timeout=args.runtime_timeout)
        payload["runtime_smoke"] = asdict(smoke)
        result_passed = result.passed and smoke.passed
    else:
        result_passed = result.passed
    print(json.dumps(payload, sort_keys=True))
    return 0 if result_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
