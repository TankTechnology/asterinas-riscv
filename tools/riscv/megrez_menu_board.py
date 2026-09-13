# SPDX-License-Identifier: MPL-2.0

"""Run one logged Megrez menu maintenance or qualification operation."""

import argparse
import contextlib
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import re
import secrets
import threading
import time

from tools.riscv import megrez_boot_menu as menu
from tools.riscv.debian.rootfs.gate_runtime import SerialConsole
from tools.riscv.megrez_board_session import BoardSession, open_serial
from tools.riscv.megrez_debug_board import _lock_serial
from tools.riscv.megrez_rockos_attestation import (
    RealRockOsAttestationOperations,
    ROCKOS_PROMPT,
    _read_password,
)


class LiveLog(io.StringIO):
    """Keep progress readable during a long board operation, including failure."""

    def __init__(self, path):
        super().__init__()
        self.stream = path.open("x", buffering=1)

    def write(self, text):
        self.stream.write(text)
        return super().write(text)

    def close(self):
        self.stream.close()
        super().close()


def attach(device: str) -> RealRockOsAttestationOperations:
    operations = RealRockOsAttestationOperations(device)
    descriptor = open_serial(device)
    try:
        _lock_serial(descriptor)
        operations._fd = descriptor
        operations._session = BoardSession.from_fd(
            descriptor, None, confirm=False, log_stream=operations._log
        )
        return operations
    except BaseException:
        os.close(descriptor)
        raise


def menu_path(document: dict) -> str:
    digest = hashlib.sha256(document["extlinux"].encode()).hexdigest()
    return f"/extlinux/asterinas-menu-{digest[:12]}.conf"


def stage(
    operations,
    document,
    directory,
    address,
    port,
    password,
    evidence=None,
    username="debian",
):
    for item in document["artifacts"].values():
        data = (directory / item["path"][1:]).read_bytes()
        menu.require(
            menu.identity(data, item["path"]) == item, "staged artifact mismatch"
        )
    menu.require(
        (directory / "asterinas-menu.conf").read_text() == document["extlinux"],
        "staged selector mismatch",
    )
    script = (
        menu.publication_script(document, f"http://{address}:{port}")
        if evidence is None
        else menu.promotion_script(document, evidence)
    )
    with (directory / "publish.sh").open("w") as stream:
        stream.write(script)
    digest = hashlib.sha256(script.encode()).hexdigest()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer((address, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        commands = (
            f"curl -fSs http://{address}:{port}/publish.sh -o /tmp/asterinas-menu-publish.sh "
            f"&& printf '%s  %s\\n' {digest} /tmp/asterinas-menu-publish.sh | sha256sum -c - "
            "&& sh /tmp/asterinas-menu-publish.sh",
        )
        operations.publish(commands, password, 180)
        digest = hashlib.sha256(document["extlinux"].encode()).hexdigest()
        marker = (
            "ASTERINAS_MENU_CANARY_READY"
            if evidence is None
            else "ASTERINAS_MENU_PUBLISHED"
        )
        menu.require(
            f"{marker} sha256={digest}" in operations._log.getvalue(),
            "menu publication did not complete",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if evidence is None:
        operations.reboot_and_recover(password, 120)
    else:
        # Observe the actual persistent bootcmd after reboot, without stopping
        # autoboot or sending a sysboot command. Leave RockOS remotely usable.
        session = operations._require_session()
        session.send("reboot")
        firmware = session.wait_for("Megrez operating system", 120)
        menu.require(
            "OpenSBI" in firmware and "U-Boot " in firmware,
            "publication did not produce a fresh firmware epoch",
        )
        session.wait_for("login:", 180)
        operations._publication_root_shell = False
        operations.login(username, password, 30)


def query_guest(session, command):
    """Run a read-only shell query with the existing full-duplex transport."""
    serial = SerialConsole(session.fd, max_bytes=1024 * 1024, tx_delay=0.005)
    for attempt in range(2):
        start = serial.checkpoint()
        try:
            deadline = time.monotonic() + 20
            serial.send((command + "\n").encode(), deadline)
            serial.wait_for(b"root@asterinas-debug:/#", deadline, start=start)
            return serial.transcript[start:].decode(errors="replace")
        except TimeoutError:
            if attempt == 1:
                raise
            # Cancel an incomplete input line (or the read-only query), then
            # acknowledge a new prompt before the sole permitted retry.
            session._log("HOST_DESKTOP_QUERY_RETRY attempt=1\n")
            recovery = serial.checkpoint()
            deadline = time.monotonic() + 5
            serial.send(b"\x03\n", deadline)
            serial.wait_for(b"root@asterinas-debug:/#", deadline, start=recovery)
        finally:
            session._log(serial.transcript[start:].decode(errors="replace"))
    raise AssertionError("guest query attempts exhausted")


def desktop_ready(session):
    """Acknowledge each short command; detect serial truncation separately."""
    ready = True
    for command, marker in (
        (
            "test -S /tmp/.X11-unix/X0 && test -c /dev/fb0; echo DISPLAY_RC=$?",
            "DISPLAY_RC",
        ),
        ("pgrep -x firefox-esr || pgrep -x firefox; echo FIREFOX_RC=$?", "FIREFOX_RC"),
        (
            "DISPLAY=:0 timeout 8 xdotool search --onlyvisible --class firefox; echo WINDOW_RC=$?",
            "WINDOW_RC",
        ),
    ):
        # BoardSession.command enforces U-Boot echo/error rules, which do not
        # apply to Bash's prompt and wrapped readline display.
        response = query_guest(session, command)
        status = re.search(rf"\n{marker}=([0-9]+)\r?\n", response)
        menu.require(
            status is not None, "desktop check command was truncated or not executed"
        )
        ready = ready and status[1] == "0"
    return ready


def boot_cycle(operations, document, mode, username, password, nonce, check_root=False):
    session = operations._require_session()
    os.write(session.fd, b"\x03")
    session.wait_for_uboot_prompt(10)
    path = menu_path(document)
    started = time.monotonic()
    if mode == "fallback":
        # Test the same || recovery mechanism with an absent candidate path;
        # never delete or rename a working selector to inject a failure.
        command = (
            f"sysboot mmc 1:1 any 0x88200000 /extlinux/absent-{nonce}.conf "
            "|| run bootcmd_rockos"
        )
        session.send(command)
        session.wait_for("Enter choice:", 30)
    else:
        session.command(
            f"sysboot mmc 1:1 any 0x88200000 {path}", expect="Enter choice:", timeout=30
        )
    menu_seen = time.monotonic()
    if mode in ("rockos", "fallback"):
        if mode == "fallback":
            default, _, _ = menu.vendor_default(document["vendor"])
            labels = [
                line.split()[1]
                for line in document["vendor"].splitlines()
                if line.split()[:1] == ["label"]
            ]
            session.send(str(labels.index(default) + 1))
        session.wait_for("login:", 180)
        reached = time.monotonic()
        operations.login(username, password, 30)
        session.send("uname -r; cat /proc/sys/kernel/random/boot_id")
        session.wait_for(ROCKOS_PROMPT, 15)
        if check_root:
            operations.publish(
                (
                    "if findmnt -rn -S /dev/mmcblk1p2; then echo ROOTFS_IS_MOUNTED; "
                    "else e2fsck -fn /dev/mmcblk1p2; rc=$?; "
                    "printf 'ROOTFS_CHECK_RC=%s\\n' \"$rc\"; fi",
                ),
                password,
                180,
            )
            menu.require(
                "ROOTFS_CHECK_RC=0" in operations._log.getvalue(),
                "read-only Debian filesystem check did not pass",
            )
        operations.reboot_and_recover(password, 120)
    elif mode == "probe":
        session.send("3")
        session.wait_for("count=1 status=pass", 100)
        reached = time.monotonic()
        recovery = session.wait_for_uboot_prompt(120)
        menu.require(
            "OpenSBI" in recovery and "U-Boot " in recovery,
            "probe did not produce a fresh firmware epoch",
        )
    elif mode == "basic":
        session.send("2")
        session.wait_for("asterinas-basic# ", 60)
        reached = time.monotonic()
        session.send(
            f'cat /proc/mounts; uname -m; echo BASIC_OK:{nonce[:16]}""{nonce[16:]}'
        )
        response = session.wait_for(f"BASIC_OK:{nonce}", 20)
        menu.require(
            "/proc proc" in response and "/sys sysfs" in response,
            "Basic API mounts are missing",
        )
        session.send("sync; reboot -f")
        recovery = session.wait_for_uboot_prompt(90)
        menu.require(
            "OpenSBI" in recovery and "U-Boot " in recovery,
            "Basic reboot did not produce a fresh firmware epoch",
        )
    elif mode == "desktop":
        session.send("4")
        response = session.wait_for("ASTERINAS_DEBUG_CONSOLE_READY uid=0", 180)
        if "root@asterinas-debug:/#" not in response:
            session.wait_for("root@asterinas-debug:/#", 20)
        # The installed cold-profile ESR image produced its first visible
        # window after the original 180-second post-console limit. Keep this
        # larger observation bound distinct from measured startup latency;
        # extending a test deadline is not a browser performance fix.
        deadline = time.monotonic() + 300
        while not desktop_ready(session):
            menu.require(
                time.monotonic() < deadline, "desktop readiness deadline expired"
            )
            time.sleep(2)
        response = query_guest(
            session, f'echo DESKTOP_READY:{nonce[:16]}""{nonce[16:]}'
        )
        menu.require(
            f"DESKTOP_READY:{nonce}" in response, "missing desktop terminal marker"
        )
        reached = time.monotonic()
        # The experimental Debian image has no usable logind. A single
        # --force asks PID 1 to stop processes and unmount filesystems without
        # the logind authorization path; never use the double-force syscall.
        serial = SerialConsole(session.fd, max_bytes=1024 * 1024, tx_delay=0.005)
        try:
            # Unlike read-only queries, a reboot is sent once, never retried.
            serial.send(b"sync; systemctl --force reboot\n", time.monotonic() + 15)
        finally:
            session._log(serial.transcript.decode(errors="replace"))
        recovery = session.wait_for_uboot_prompt(120)
        menu.require(
            "OpenSBI" in recovery and "U-Boot " in recovery,
            "Desktop reboot did not produce a fresh firmware epoch",
        )
    else:
        raise ValueError("unknown qualification mode")
    return {
        "boot_to_ready_seconds": reached - started,
        "menu_to_ready_seconds": reached - menu_seen,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "cycle", "promote"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--device",
        default="/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0",
    )
    parser.add_argument("--username", default="debian")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--password-fd", type=int)
    credentials.add_argument(
        "--factory-login",
        action="store_true",
        help="use the documented public RockOS factory password",
    )
    parser.add_argument(
        "--mode",
        choices=("rockos", "fallback", "basic", "probe", "desktop"),
        default="probe",
    )
    parser.add_argument("--address", default="10.100.19.216")
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--check-root", action="store_true")
    parser.add_argument("--from-uboot", action="store_true")
    args = parser.parse_args()
    document = json.loads(args.manifest.read_text())
    menu.validate(document)
    if args.action == "promote":
        if args.evidence is None:
            parser.error("promote requires --evidence")
        # Reject incomplete evidence before acquiring or altering board state.
        menu.promotion_script(document, args.evidence)
    password = (
        ("debian" if args.factory_login else _read_password(args.password_fd))
        if args.action in ("stage", "promote") or args.mode in ("rockos", "fallback")
        else ""
    )
    args.output.mkdir(parents=True, exist_ok=False)
    nonce = secrets.token_hex(16)
    result = {
        "run_id": nonce,
        "action": args.action,
        "mode": args.mode if args.action == "cycle" else None,
        "menu_sha256": hashlib.sha256(document["extlinux"].encode()).hexdigest(),
        "passed": False,
        "recovered": False,
    }
    operations = attach(args.device)
    operations._log.close()
    operations._log = LiveLog(args.output / "serial.log")
    operations._require_session().log = operations._log
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            if args.action in ("stage", "promote"):
                if args.from_uboot:
                    os.write(operations._fd, b"\x03")
                    operations._require_session().wait_for_uboot_prompt(10)
                    operations.boot_rockos(180)
                    operations.login(args.username, password, 30)
                stage(
                    operations,
                    document,
                    args.manifest.parent,
                    args.address,
                    args.port,
                    password,
                    args.evidence if args.action == "promote" else None,
                    args.username,
                )
            else:
                result.update(
                    boot_cycle(
                        operations,
                        document,
                        args.mode,
                        args.username,
                        password,
                        nonce,
                        args.check_root,
                    )
                )
        result.update(passed=True, recovered=True)
    except (Exception, KeyboardInterrupt) as error:
        result["error"] = str(error) or type(error).__name__
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        transcript = operations._log.getvalue().encode()
        result["serial_sha256"] = hashlib.sha256(transcript).hexdigest()
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        operations.close()
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
