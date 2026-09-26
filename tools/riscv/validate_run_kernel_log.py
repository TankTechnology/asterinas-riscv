#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

"""Fail-closed validation for the complete ``make run_kernel`` transcript."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


SUCCESS_MARKERS = {
    "boot": "Successfully booted.",
    "conformance": "All conformance tests passed.",
    "ext2-firefox-recovery": "ASTERINAS_EXT2_FIREFOX_RECOVERY_OK cycles=16",
    "fs-syscall-compat": "ASTERINAS_FS_SYSCALL_COMPAT_OK readahead=5 quotactl=1",
    "ifconf": "SIOCGIFCONF regression passed.",
    "ifreq": "interface ioctl regression passed.",
    "ip-socket-netns": "IP socket namespace regression passed.",
    "ifconf-gvisor": "gVisor SIOCGIFCONF cases passed.",
    "proc-net-dev": "/proc/net/dev regression passed.",
    "ipv6-udp": "ipv6_udp: PASS",
    "regression": "All regression tests passed.",
    "udp-user-buffer-prefault": "UDP user buffer prefault regression passed.",
    "udp-msg-dontwait": "UDP MSG_DONTWAIT regression passed.",
    "tcp-msg-dontwait-send": "TCP MSG_DONTWAIT send regression passed.",
    "vsock": "Vsock test passed.",
}
MULTI_FACT_MARKERS = {
    "netlink-route-netns": (
        "netlink route socket namespace regression passed.",
        "IPv4 route lookup regression passed.",
        "IPv4 local route dump regression passed.",
        "IPv4 all route tables regression passed.",
        "netlink uevent port namespace regression passed.",
    ),
    "ipv6-dual-stack": (
        "ASTERINAS_IPV6_DUAL_STACK_TCP_OK",
        "ipv6_udp: PASS",
        "ASTERINAS_IPV6_DUAL_STACK_UDP_OK",
    ),
    "ipv6-dual-stack-udp": ("ASTERINAS_IPV6_DUAL_STACK_UDP_OK",),
}
FATAL_PATTERNS = (
    re.compile(r"uncaught panic", re.IGNORECASE),
    re.compile(r"kernel panic", re.IGNORECASE),
    re.compile(r"unexpected exception", re.IGNORECASE),
    re.compile(r"sbi remote fence\.i(?: to hart [0-9]+)? failed", re.IGNORECASE),
)
ICACHE_SMP4_MARKER = re.compile(
    r"^riscv_flush_icache cross-hart passed: "
    r"cpus=4 local=([0-9]+) remotes=([0-9]+),([0-9]+),([0-9]+) "
    r"generations=1024$"
)


class ValidationError(ValueError):
    """The transcript does not prove the requested acceptance result."""


def validate_ext2_image(image: Path) -> None:
    """Require a clean read-only host fsck after the guest has unmounted ext2."""

    if image.is_symlink() or not image.is_file():
        raise ValidationError(f"ext2 result image is not a regular file: {image}")
    try:
        result = subprocess.run(
            ["e2fsck", "-fn", str(image)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValidationError(f"e2fsck failed to run: {error}") from error
    if result.returncode != 0:
        raise ValidationError(
            f"e2fsck rejected ext2 result image (exit={result.returncode}): "
            f"{result.stdout[-500:]}"
        )


def _is_logical_marker(line: str, marker: str) -> bool:
    return line == marker or line.startswith(f"{marker} ")


def validate_transcript(
    transcript: str, *, mode: str, require_riscv_icache_smp4: bool = False
) -> None:
    lines = transcript.splitlines()
    if mode in MULTI_FACT_MARKERS:
        for marker in MULTI_FACT_MARKERS[mode]:
            if sum(_is_logical_marker(line, marker) for line in lines) != 1:
                raise ValidationError(f"expected exactly one success fact: {marker}")
    else:
        marker = SUCCESS_MARKERS[mode]
        if sum(line == marker for line in lines) != 1:
            raise ValidationError(f"expected exactly one terminal marker: {marker}")

    for line in lines:
        for pattern in FATAL_PATTERNS:
            if pattern.search(line):
                raise ValidationError(f"fatal transcript marker: {line.strip()}")

    if not require_riscv_icache_smp4:
        return
    if mode != "regression":
        raise ValidationError("the SMP4 icache contract requires regression mode")
    if any("riscv_flush_icache cross-hart skipped" in line for line in lines):
        raise ValidationError("the SMP4 icache regression was skipped")

    matches = [ICACHE_SMP4_MARKER.fullmatch(line) for line in lines]
    matches = [match for match in matches if match is not None]
    if len(matches) != 1:
        raise ValidationError("expected exactly one SMP4 cross-hart icache marker")
    cpu_ids = tuple(int(value) for value in matches[0].groups())
    if len(set(cpu_ids)) != 4:
        raise ValidationError("SMP4 cross-hart icache marker has duplicate CPU IDs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=tuple((*SUCCESS_MARKERS, *MULTI_FACT_MARKERS)),
        required=True,
    )
    parser.add_argument("--require-riscv-icache-smp4", action="store_true")
    parser.add_argument("--ext2-image", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        transcript = args.log.read_text(encoding="utf-8", errors="replace")
        validate_transcript(
            transcript,
            mode=args.mode,
            require_riscv_icache_smp4=args.require_riscv_icache_smp4,
        )
        if args.mode == "ext2-firefox-recovery":
            if args.ext2_image is None:
                raise ValidationError("ext2 recovery mode requires --ext2-image")
            validate_ext2_image(args.ext2_image)
    except (OSError, ValidationError) as error:
        print(f"run_kernel validation failed: {error}")
        return 1
    print(f"run_kernel validation passed: mode={args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
