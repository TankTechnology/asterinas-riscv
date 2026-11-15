#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Status: manual -- QEMU safety-net simulations; run with `rt recovery-sim`.
"""Boot-failure simulations for the board recovery guarantees.

Each case boots the current release kernel under QEMU with a chosen initramfs
and command line, then counts boots and the kernel's recovery markers. The
cases mirror the ways a real board has been lost or could be lost:

- stage1 holding after a root-init mode that cannot match the rootfs;
- stage1 holding because the initramfs has no busybox;
- a userspace that never becomes healthy;
- a kernel panic with and without the restart policy;
- an unset deadline on a virtual platform (the one case that deliberately
  stays off).

Two further cases assert that the boot gate refuses the exact command lines
behind the 2026-11-11 incidents.

This is not a proof; it is the executable demonstration that the paths the
safety case relies on actually fire.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TOOLS = REPO / "tools/riscv"
DEFAULT_IMAGE = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
MAX_RELEASE_IMAGE_BYTES = 10 * 1024 * 1024


def build_stage1(workdir: Path, name: str = "stage1.cpio", cflags: str = "") -> Path:
    archive = workdir / name
    environment = dict(os.environ)
    if cflags:
        environment["RISC_V_CFLAGS"] = cflags
    subprocess.run(
        [str(TOOLS / "debian/rootfs/build_stage1.sh"), str(archive)],
        check=True,
        stdout=subprocess.DEVNULL,
        env=environment,
    )
    return archive


def build_pause_initramfs(workdir: Path) -> Path:
    source = workdir / "pause_init.c"
    source.write_text(
        "#include <unistd.h>\nint main(void) { for (;;) pause(); }\n",
        encoding="utf-8",
    )
    binary = workdir / "pause_init"
    subprocess.run(
        ["riscv64-linux-gnu-gcc", "-O2", "-static", "-o", str(binary), str(source)],
        check=True,
    )
    root = workdir / "pauseroot"
    root.mkdir()
    shutil.copyfile(binary, root / "init")
    archive = workdir / "pause.cpio"
    with open(archive, "wb") as out:
        subprocess.run(
            ["sh", "-c", "find . | cpio --quiet --reproducible -o -H newc"],
            cwd=root,
            stdout=out,
            check=True,
        )
    return archive


def build_empty_initramfs(workdir: Path) -> Path:
    root = workdir / "emptyroot"
    root.mkdir()
    archive = workdir / "empty.cpio"
    with open(archive, "wb") as out:
        subprocess.run(
            ["sh", "-c", "find . | cpio --quiet --reproducible -o -H newc"],
            cwd=root,
            stdout=out,
            check=True,
        )
    return archive


def run_case(
    name: str,
    image: Path,
    initrd: Path,
    append: str,
    seconds: int,
    checks: dict,
) -> tuple[bool, str]:
    command = [
        "qemu-system-riscv64",
        "-machine",
        "virt",
        "-m",
        "2G",
        "-smp",
        "4",
        "-nographic",
        "-kernel",
        str(image),
        "-initrd",
        str(initrd),
        "-append",
        append,
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=seconds)
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired as expired:
        text = ((expired.stdout or b"") + (expired.stderr or b"")).decode(
            "utf-8", "replace"
        )

    boots = len(re.findall(r"OpenSBI v", text))
    armed = len(re.findall(r"ASTERINAS_SOFTWARE_REBOOT_ARMED", text))
    problems = []
    if boots < checks.get("min_boots", 1):
        problems.append(f"boots={boots} < {checks['min_boots']}")
    if "max_boots" in checks and boots > checks["max_boots"]:
        problems.append(f"boots={boots} > {checks['max_boots']}")
    for needle in checks.get("contains", []):
        if needle not in text:
            problems.append(f"missing {needle!r}")
    detail = f"boots={boots} armed={armed}" + (
        "" if not problems else " " + "; ".join(problems)
    )
    return not problems, detail


def gate_cases() -> list[tuple[str, bool, str]]:
    sys.path.insert(0, str(REPO))
    from tools.riscv.board.megrez_console import BootSafetyError, validate_boot_append

    incidents = [
        (
            "gate-refuses-interactive-incident",
            "console=ttyS0 loglevel=warn init=/init asterinas.mmc_write_partition2 "
            "asterinas.hardware_watchdog_handoff=1 -- --root-init=interactive",
        ),
        (
            "gate-refuses-basic-without-initramfs",
            "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=5400 "
            "-- --root-init=basic",
        ),
        (
            "gate-refuses-unknown-init-option",
            "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=300 "
            "-- --root-init=systemd --debug-console=bogus",
        ),
        (
            "gate-refuses-root-init-before-separator",
            "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=300 "
            "--root-init=systemd",
        ),
    ]
    results = []
    for name, append in incidents:
        try:
            validate_boot_append(append)
        except BootSafetyError as error:
            results.append((name, True, f"refused: {str(error)[:60]}"))
        else:
            results.append((name, False, "accepted a line it must refuse"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        type=Path,
        default=DEFAULT_IMAGE,
        help="release kernel Image to boot (refuses dev/ktest builds)",
    )
    parser.add_argument(
        "--only", action="append", default=[], help="run only these case names"
    )
    parser.add_argument("--keep", action="store_true", help="keep the fixtures")
    args = parser.parse_args(argv)

    image = args.image
    if not image.is_file():
        print(f"missing kernel image: {image}", file=sys.stderr)
        return 2
    if image.stat().st_size > MAX_RELEASE_IMAGE_BYTES:
        print(
            f"{image} is {image.stat().st_size} bytes; a dev/ktest build "
            "overwrote the release image (rebuild with the release profile)",
            file=sys.stderr,
        )
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="recovery-sim-"))
    try:
        stage1 = build_stage1(workdir)
        # A build with a short hold bound, so the bounded-hold reboot can be
        # observed inside a simulation run instead of after five minutes.
        fast_hold = build_stage1(
            workdir, "stage1-fast-hold.cpio", "-DHOLD_REBOOT_SECONDS=8"
        )
        pause = build_pause_initramfs(workdir)
        empty = build_empty_initramfs(workdir)

        cases = [
            (
                "stuck-stage1-interactive",
                stage1,
                "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=8 "
                "-- --root-init=interactive",
                30,
                {"min_boots": 2, "contains": ["step=start mode=interactive"]},
            ),
            (
                "stuck-stage1-basic-no-busybox",
                stage1,
                "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=8 "
                "-- --root-init=basic",
                30,
                {"min_boots": 2, "contains": ["basic-busybox-missing"]},
            ),
            (
                "stage1-root-init-argument",
                stage1,
                "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=8 "
                "-- --root-init=systemd --debug-console=bogus",
                30,
                {"min_boots": 2, "contains": ["root-init-argument"]},
            ),
            (
                # A held boot must stay observable: one failure line and then
                # silence is indistinguishable from a dead machine.
                "hold-heartbeat",
                stage1,
                "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=25 "
                "-- --root-init=basic",
                32,
                {
                    "min_boots": 1,
                    "contains": [
                        "basic-busybox-missing",
                        "DEBIAN_STAGE1_HOLD reason=basic-busybox-missing",
                    ],
                },
            ),
            (
                # No kernel deadline at all: only stage1's own hold bound can
                # bring the board back. The fixture is built with an 8 s bound.
                "hold-reboots-without-kernel-deadline",
                fast_hold,
                "console=ttyS0 loglevel=warn init=/init -- --root-init=basic",
                28,
                {
                    "min_boots": 2,
                    "contains": [
                        "DEBIAN_STAGE1_HOLD_REBOOT reason=basic-busybox-missing"
                    ],
                },
            ),
            (
                "root-init-before-separator",
                stage1,
                "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=8 "
                "--root-init=systemd",
                30,
                {"min_boots": 2, "contains": ["step=start mode=interactive"]},
            ),
            (
                "hung-init",
                pause,
                "console=ttyS0 loglevel=warn init=/init asterinas.reboot_after=6",
                28,
                {"min_boots": 2},
            ),
            (
                "panic-restarts",
                empty,
                "console=ttyS0 loglevel=warn init=/init asterinas.panic_reboot=1 "
                "asterinas.reboot_after=0",
                24,
                {"min_boots": 2},
            ),
            (
                # The 2026-11-10 regression: the panic policy install used to
                # take the one injection slot, so the deadline was never armed.
                # Both nets must coexist now, and the deadline must fire.
                "panic-policy-and-deadline-coexist",
                pause,
                "console=ttyS0 loglevel=warn init=/init asterinas.panic_reboot=1 "
                "asterinas.reboot_after=6",
                25,
                {
                    "min_boots": 2,
                    "contains": [
                        "ASTERINAS_PANIC_REBOOT_ARMED",
                        "ASTERINAS_SOFTWARE_REBOOT_SYNC_START",
                    ],
                },
            ),
            (
                "panic-halts-without-policy",
                empty,
                "console=ttyS0 loglevel=warn init=/init asterinas.panic_reboot=0 "
                "asterinas.reboot_after=0",
                18,
                {"min_boots": 1, "max_boots": 1},
            ),
            (
                "qemu-default-is-off",
                pause,
                "console=ttyS0 loglevel=warn init=/init",
                18,
                {
                    "min_boots": 1,
                    "max_boots": 1,
                    "contains": ["ASTERINAS_SOFTWARE_REBOOT_DISABLED"],
                },
            ),
        ]

        selected = set(args.only)
        failures = 0
        print(f"{'case':36} result")
        for name, initrd, append, seconds, checks in cases:
            if selected and name not in selected:
                continue
            ok, detail = run_case(name, image, initrd, append, seconds, checks)
            failures += 0 if ok else 1
            print(f"{name:36} {'PASS' if ok else 'FAIL'}  {detail}", flush=True)
        for name, ok, detail in gate_cases():
            if selected and name not in selected:
                continue
            failures += 0 if ok else 1
            print(f"{name:36} {'PASS' if ok else 'FAIL'}  {detail}", flush=True)
        print(
            f"\n{'all simulations passed' if failures == 0 else f'{failures} FAILED'}"
        )
        return 0 if failures == 0 else 1
    finally:
        if args.keep:
            print(f"fixtures kept in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
