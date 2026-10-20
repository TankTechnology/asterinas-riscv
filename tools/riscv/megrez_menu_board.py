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
from types import SimpleNamespace

from tools.riscv import megrez_boot_menu as menu
from tools.riscv.megrez_hardware_probe import (
    PROBE_SPECS,
    ProbeContractError,
    classify_probe_timeout,
    validate_prerequisites,
    validate_probe_evidence,
    validate_probe_timeout_evidence,
)
from tools.riscv.debian.rootfs.gate_runtime import SerialConsole
from tools.riscv.megrez_board_session import (
    BoardSession,
    boot_loaded_artifacts,
    open_serial,
    validate_recovery_epoch,
)
from tools.riscv.megrez_debug_board import RealBoardOperations, _lock_serial
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


def _record_probe_recovery(operations, device: str, status: str) -> None:
    """Append one host-observed recovery marker to the device transcript."""

    operations._log.write(
        f"ASTERINAS_PROBE_RECOVERY device={device} status={status}\n"
    )


# These are registration/activation markers from the Asterinas guest, rather
# than firmware or RockOS messages.  A Basic qualification is allowed to
# initialize the component framework, but it must not acquire any of the
# optional EIC7700 datapaths that the minimal DT profile gates off.
BASIC_FORBIDDEN_DEVICE_MARKERS = (
    "framebuffer: Registered firmware framebuffer",
    "dwmac: prepared GMAC",
    "dwmac: ASTERINAS_GMAC_",
    "usb: Selected DWC3 USB host",
    "usb: Starting DWC3 xHCI host",
)
BASIC_REQUIRED_DEVICE_MARKERS = (
    "uart: Registered DW APB UART",
    "mmc: [mmc] controller",
)


def validate_basic_device_gate(transcript: str) -> dict[str, object]:
    """Validate the runtime device set of one Basic boot.

    U-Boot and RockOS precede the guest in one serial transcript and both may
    legitimately mention Ethernet/USB.  Scope the check to the last Asterinas
    kernel epoch, then fail closed if a forbidden device registered or a
    required management/storage device did not appear.  This prevents an
    intended DT edit from being reported as successful when the selected
    menu entry silently loaded an unmodified DTB.
    """

    if not isinstance(transcript, str):
        raise TypeError("Basic device-gate transcript must be text")
    kernel_start = transcript.rfind("Enter riscv_boot")
    if kernel_start < 0:
        raise RuntimeError("Basic device-gate is missing the Asterinas kernel epoch")
    guest = transcript[kernel_start:]
    forbidden = tuple(
        marker for marker in BASIC_FORBIDDEN_DEVICE_MARKERS if marker in guest
    )
    if forbidden:
        raise RuntimeError(
            "Basic device-gate observed forbidden registrations: "
            + ", ".join(forbidden)
        )
    missing = tuple(
        marker for marker in BASIC_REQUIRED_DEVICE_MARKERS if marker not in guest
    )
    if missing:
        raise RuntimeError(
            "Basic device-gate missing required registrations: "
            + ", ".join(missing)
        )
    return {
        "kernel_epoch": "Enter riscv_boot",
        "forbidden_registrations": [],
        "required_registrations": list(BASIC_REQUIRED_DEVICE_MARKERS),
    }


def _ordered_recovery_epoch(transcript: str) -> bool:
    """Return whether *transcript* contains one complete fresh firmware epoch.

    A serial log normally starts with the firmware epoch that was already
    running when the host attached.  That prefix must never be reused as
    evidence that a later watchdog or software reboot recovered the board.
    Keep this predicate small and ordered so a stale U-Boot prompt, or a
    partial banner followed by a prompt, cannot clear the recovery latch.
    """

    if not isinstance(transcript, str):
        return False
    try:
        validate_recovery_epoch(transcript)
    except RuntimeError:
        return False
    return True


def _ordered_recovery_boundary(transcript: str) -> str | None:
    """Classify a fresh firmware epoch that reached U-Boot or RockOS login."""

    if not isinstance(transcript, str):
        return None
    opensbi = transcript.find("OpenSBI")
    if opensbi < 0:
        return None
    uboot = re.search(r"U-Boot[ \t]+[^\r\n]*", transcript[opensbi + 1 :])
    if uboot is None:
        return None
    boundary_start = opensbi + 1 + uboot.end()
    if transcript.find("=> ", boundary_start) >= 0:
        return "uboot-prompt"
    if re.search(r"(?m)\blogin:\s*", transcript[boundary_start:]):
        return "rockos-login"
    return None


def _log_length(operations) -> int:
    """Read the current in-memory log length without consuming the serial fd."""

    try:
        return len(operations._log.getvalue())
    except (AttributeError, TypeError):
        return 0


def _basic_loader_args(document: dict) -> SimpleNamespace:
    """Build the guarded direct-loader arguments for the Basic menu leg."""

    artifacts = document["artifacts"]
    return SimpleNamespace(
        booti=artifacts["kernel"]["path"].removeprefix("/"),
        initrd=artifacts["stage1"]["path"].removeprefix("/"),
        dtb=artifacts["dtb"]["path"].removeprefix("/"),
        expected_crc32={
            "booti": artifacts["kernel"]["crc32"],
            "initrd": artifacts["stage1"]["crc32"],
            "dtb": artifacts["dtb"]["crc32"],
        },
        bootargs=menu.MODE_ARGS["basic"],
        load_transport="mmc",
        mmc_device_partition="1:1",
        firmware_framebuffer=False,
        device_profile="minimal",
    )


def _desktop_loader_args(document: dict) -> SimpleNamespace:
    """Build the guarded direct-loader arguments for the fbdev desktop leg.

    The desktop keeps the firmware simple-framebuffer handoff but disables
    the EIC7700 Ethernet and USB wrapper nodes.  Loading through ``sysboot``
    would silently reuse the full DTB and reintroduce the physical-board
    failure mode that this gate is meant to exclude.
    """

    artifacts = document["artifacts"]
    return SimpleNamespace(
        booti=artifacts["kernel"]["path"].removeprefix("/"),
        initrd=artifacts["stage1"]["path"].removeprefix("/"),
        dtb=artifacts["dtb"]["path"].removeprefix("/"),
        expected_crc32={
            "booti": artifacts["kernel"]["crc32"],
            "initrd": artifacts["stage1"]["crc32"],
            "dtb": artifacts["dtb"]["crc32"],
        },
        bootargs=document["desktop_args"],
        load_transport="mmc",
        mmc_device_partition="1:1",
        firmware_framebuffer=False,
        device_profile="display-safe",
    )


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


def desktop_ready(session, *, fbdev_only=False):
    """Acknowledge each short command; detect serial truncation separately."""
    ready = True
    checks = [
        (
            "test -S /tmp/.X11-unix/X0 && test -c /dev/fb0; echo DISPLAY_RC=$?",
            "DISPLAY_RC",
        )
    ] if fbdev_only else [
        (
            "test -S /tmp/.X11-unix/X0 && test -c /dev/fb0; echo DISPLAY_RC=$?",
            "DISPLAY_RC",
        ),
        ("pgrep -x firefox-esr || pgrep -x firefox; echo FIREFOX_RC=$?", "FIREFOX_RC"),
        (
            "DISPLAY=:0 timeout 8 xdotool search --onlyvisible --class firefox; echo WINDOW_RC=$?",
            "WINDOW_RC",
        ),
    ]
    for command, marker in checks:
        # BoardSession.command enforces U-Boot echo/error rules, which do not
        # apply to Bash's prompt and wrapped readline display.
        response = query_guest(session, command)
        status = re.search(rf"\n{marker}=([0-9]+)\r?\n", response)
        menu.require(
            status is not None, "desktop check command was truncated or not executed"
        )
        ready = ready and status[1] == "0"
    return ready


def boot_cycle(
    operations,
    document,
    mode,
    username,
    password,
    nonce,
    check_root=False,
    hardware_watchdog=False,
    firmware_timeout: float | None = None,
    from_uboot=False,
):
    menu.require(
        not hardware_watchdog
        or mode in ("basic", "probe", "desktop")
        or mode in PROBE_SPECS
        or mode == menu.PHY_DIAGNOSTIC_MODE,
        "hardware watchdog is restricted to the Basic, desktop, or read-only probe entry",
    )
    menu.require(
        (mode not in PROBE_SPECS and mode != menu.PHY_DIAGNOSTIC_MODE)
        or hardware_watchdog,
        "hardware watchdog is required for every device probe",
    )
    session = operations._require_session()
    initial_firmware_timeout = (
        firmware_timeout if firmware_timeout is not None else (45 if hardware_watchdog else 10)
    )
    if from_uboot:
        # `stage` intentionally leaves the same board at a U-Boot prompt. A
        # fresh reset gives this cycle its own OpenSBI/U-Boot epoch before any
        # watchdog or sysboot command, without handing the descriptor to an
        # unverified listener.
        session.send("reset")
    else:
        os.write(session.fd, b"\x03")
    session.wait_for_uboot_prompt(initial_firmware_timeout)
    # Everything before this point belongs to the firmware epoch that was
    # already present when the host attached.  Failure handling in ``main``
    # uses this checkpoint so that banner text from that prefix cannot be
    # mistaken for recovery after the current boot attempt.
    operations._recovery_checkpoint = _log_length(operations)
    operations._boot_attempt_started = False
    if hardware_watchdog:
        # Arm only after a fresh U-Boot prompt and before sysboot. The helper
        # performs clock/reset/type/readback checks and fails closed before
        # loading the guest when the board does not expose the expected WDT.
        RealBoardOperations._arm_hardware_watchdog(session, time.monotonic() + 10)
    # From here onward a watchdog is armed (when requested) and the selected
    # guest path may have started.  Exception handling must wait for recovery
    # even if the loader or guest marker fails before ``booti`` returns.
    operations._boot_attempt_started = True
    started = time.monotonic()
    # Basic deliberately bypasses the menu's immutable DTB stanza.  The
    # direct loader applies the volatile minimal profile and verifies each
    # readback before `booti`; selecting the menu entry would silently reload
    # the unmodified full DTB.
    direct_basic = mode == "basic"
    if mode == "fallback":
        # Test the same || recovery mechanism with an absent candidate path;
        # never delete or rename a working selector to inject a failure.
        command = (
            f"sysboot mmc 1:1 any 0x88200000 /extlinux/absent-{nonce}.conf "
            "|| run bootcmd_rockos"
        )
        session.send(command)
        session.wait_for("Enter choice:", 30)
    elif not direct_basic:
        path = menu_path(document)
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
        recovery = session.wait_for_uboot_prompt(
            firmware_timeout if firmware_timeout is not None else 120
        )
        menu.require(
            "OpenSBI" in recovery and "U-Boot " in recovery,
            "probe did not produce a fresh firmware epoch",
        )
    elif mode in PROBE_SPECS:
        # Device probes are separate menu entries.  They never send a guest
        # command: Stage1 prints the bounded result and reboots by itself.
        session.send(str(menu.MODES.index(mode) + 2))
        # Accept either a completed pass or an explicit fail marker.  The
        # validator below still requires the full whitelist for pass and a
        # bounded, recovered failure for fail; waiting only for pass would
        # turn a safe MMIO timeout into an unclassified host timeout.
        try:
            session.wait_for(f"ASTERINAS_PROBE_DONE device={mode} status=", 100)
        except TimeoutError:
            # A stalled MMIO transaction can prevent the guest from flushing
            # PROBE_FAIL before the hardware watchdog fires.  Give the
            # recovery path its own bounded observation window and retain the
            # outcome as a failed timeout, never as a qualification pass.
            recovery = session.wait_for_uboot_prompt(120)
            menu.require(
                "OpenSBI" in recovery and "U-Boot " in recovery,
                "hardware probe timeout did not produce a fresh firmware epoch",
            )
            reached = time.monotonic()
            _record_probe_recovery(operations, mode, "timeout")
            timeout_result = validate_probe_timeout_evidence(
                operations._log.getvalue(), mode, nonce
            )
            return {
                "boot_to_ready_seconds": reached - started,
                "menu_to_ready_seconds": reached - menu_seen,
                "probe_status": "timeout",
                "probe_failure": "completion-timeout",
                "probe_read_offsets": list(timeout_result.read_offsets),
                "probe_attempt_offsets": list(timeout_result.attempt_offsets),
                "probe_timeout_phase": classify_probe_timeout(
                    operations._log.getvalue(), mode, nonce
                ),
                "recovery_observed": True,
            }
        reached = time.monotonic()
        recovery = session.wait_for_uboot_prompt(120)
        menu.require(
            "OpenSBI" in recovery and "U-Boot " in recovery,
            "hardware probe did not produce a fresh firmware epoch",
        )
        try:
            probe_result = validate_probe_evidence(
                operations._log.getvalue(),
                mode,
                nonce,
                require_access_audit=mode.startswith("gmac-"),
            )
        except ProbeContractError:
            _record_probe_recovery(operations, mode, "invalid")
            raise
        _record_probe_recovery(operations, mode, probe_result.status)
        if probe_result.status == "fail":
            # A recovered probe failure is useful safety evidence, but it is
            # never a passed qualification or a prerequisite for GMAC.
            return {
                "boot_to_ready_seconds": reached - started,
                "menu_to_ready_seconds": reached - menu_seen,
                "probe_status": "fail",
                "probe_read_offsets": list(probe_result.read_offsets),
                "probe_attempt_offsets": list(probe_result.attempt_offsets),
                "probe_access_audit": (
                    None
                    if probe_result.access_audit is None
                    else probe_result.access_audit.__dict__
                ),
                "recovery_observed": True,
            }
        return {
            "boot_to_ready_seconds": reached - started,
            "menu_to_ready_seconds": reached - menu_seen,
            "probe_status": "pass",
            "probe_read_offsets": list(probe_result.read_offsets),
            "probe_attempt_offsets": list(probe_result.attempt_offsets),
            "probe_access_audit": (
                None
                if probe_result.access_audit is None
                else probe_result.access_audit.__dict__
            ),
        }
    elif mode == menu.PHY_DIAGNOSTIC_MODE:
        # PHY-only is a separate gate after a corrected GMAC-1 read-only
        # probe. It does not register the network device or send traffic.
        session.send(str(menu.MODES.index(mode) + 2))
        try:
            session.wait_for("ASTERINAS_GMAC_PHY_DONE status=", 100)
        except TimeoutError:
            recovery = session.wait_for_uboot_prompt(120)
            menu.require(
                "OpenSBI" in recovery and "U-Boot " in recovery,
                "PHY diagnostic timeout did not produce a fresh firmware epoch",
            )
            reached = time.monotonic()
            return {
                "boot_to_ready_seconds": reached - started,
                "menu_to_ready_seconds": reached - menu_seen,
                "phy_status": "timeout",
                "recovery_observed": True,
            }
        reached = time.monotonic()
        recovery = session.wait_for_uboot_prompt(120)
        menu.require(
            "OpenSBI" in recovery and "U-Boot " in recovery,
            "PHY diagnostic did not produce a fresh firmware epoch",
        )
        status = (
            "pass"
            if "ASTERINAS_GMAC_PHY_DONE status=pass" in operations._log.getvalue()
            else "fail"
        )
        return {
            "boot_to_ready_seconds": reached - started,
            "menu_to_ready_seconds": reached - menu_seen,
            "phy_status": status,
        }
    elif mode == "basic":
        boot_loaded_artifacts(session, _basic_loader_args(document))
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
        basic_device_gate = validate_basic_device_gate(operations._log.getvalue())
        session.send("sync; reboot -f")
        reboot_checkpoint = _log_length(operations)
        try:
            recovery = session.wait_for_uboot_prompt(90)
        except TimeoutError:
            recovery = operations._log.getvalue()[reboot_checkpoint:]
            if _ordered_recovery_boundary(recovery) is None:
                recovery += session.wait_for_recovery_boundary(90)
        boundary = _ordered_recovery_boundary(recovery)
        menu.require(
            boundary is not None,
            "Basic reboot did not produce a fresh firmware or RockOS boundary",
        )
        recovery_boundary = boundary
    elif mode == "desktop":
        # Keep the framebuffer handoff, but never let the desktop menu path
        # reload the unmodified DTB and probe the risky EIC7700 wrappers.
        boot_loaded_artifacts(session, _desktop_loader_args(document))
        response = session.wait_for("ASTERINAS_DEBUG_CONSOLE_READY uid=0", 180)
        if "root@asterinas-debug:/#" not in response:
            session.wait_for("root@asterinas-debug:/#", 20)
        # The installed cold-profile ESR image produced its first visible
        # window after the original 180-second post-console limit. Keep this
        # larger observation bound distinct from measured startup latency;
        # extending a test deadline is not a browser performance fix.
        deadline = time.monotonic() + 300
        while not desktop_ready(session, fbdev_only=True):
            menu.require(
                time.monotonic() < deadline, "desktop readiness deadline expired"
            )
            time.sleep(2)
        framebuffer = query_guest(
            session,
            "test -x /usr/lib/asterinas/desktop-m4-framebuffer-rw && "
            "/usr/lib/asterinas/desktop-m4-framebuffer-rw --device /dev/fb0; "
            "rc=$?; echo FB_RW_RC=$rc",
        )
        menu.require(
            "DEBIAN_DESKTOP_M4_FB_RW " in framebuffer
            and "status=pass" in framebuffer
            and re.search(r"\nFB_RW_RC=0\r?\n", framebuffer) is not None,
            "desktop framebuffer write/restore gate failed",
        )
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
        **(
            {"basic_device_gate": basic_device_gate}
            if mode == "basic"
            else {}
        ),
        **(
            {"recovery_boundary": recovery_boundary}
            if mode == "basic"
            else {}
        ),
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
        choices=(
            "rockos",
            "fallback",
            "basic",
            "probe",
            "desktop",
            *PROBE_SPECS,
            menu.PHY_DIAGNOSTIC_MODE,
        ),
        default="probe",
    )
    parser.add_argument(
        "--prior-result",
        action="append",
        type=Path,
        help="a passed result.json for each required earlier hardware probe",
    )
    parser.add_argument("--address", default="10.100.19.216")
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--check-root", action="store_true")
    parser.add_argument(
        "--hardware-watchdog",
        action="store_true",
        help="arm the verified EIC7700 WDT for a bounded Basic, desktop, or probe cycle",
    )
    origin = parser.add_mutually_exclusive_group()
    origin.add_argument(
        "--from-uboot",
        action="store_true",
        help="the attached serial session is already at a fresh U-Boot prompt",
    )
    origin.add_argument(
        "--from-rockos",
        action="store_true",
        help="authenticate the current RockOS login on this same serial owner before staging",
    )
    args = parser.parse_args()
    if args.from_rockos and args.action not in ("stage", "promote"):
        parser.error("--from-rockos is only valid for stage or promote")
    document = json.loads(args.manifest.read_text())
    menu.validate(document)
    if args.hardware_watchdog and (
        args.action != "cycle"
        or (
        args.mode != "basic"
        and args.mode != "probe"
        and args.mode != "desktop"
            and args.mode not in PROBE_SPECS
            and args.mode != menu.PHY_DIAGNOSTIC_MODE
        )
    ):
        parser.error("--hardware-watchdog requires a Basic, desktop, or read-only probe cycle")
    if (
        args.action == "cycle"
        and args.mode == "desktop"
        and not args.hardware_watchdog
    ):
        parser.error("desktop cycle requires --hardware-watchdog")
    if (
        args.action == "cycle"
        and (args.mode in PROBE_SPECS or args.mode == menu.PHY_DIAGNOSTIC_MODE)
        and not args.hardware_watchdog
    ):
        parser.error("device probes require --hardware-watchdog")
    if args.action == "cycle" and (
        args.mode in PROBE_SPECS or args.mode == menu.PHY_DIAGNOSTIC_MODE
    ):
        completed = set()
        for path in args.prior_result or ():
            data = json.loads(path.read_text())
            if data.get("passed") is True and data.get("recovered") is True:
                completed.add(data.get("mode"))
        if args.mode in PROBE_SPECS:
            validate_prerequisites(args.mode, completed)
        elif "gmac-1" not in completed:
            parser.error("gmac-phy requires a passed gmac-1 read-only probe")
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
                elif args.from_rockos:
                    # The previous serial owner may already have consumed the
                    # login banner.  A bare carriage return is harmless at a
                    # login prompt and makes the handoff boundary observable
                    # without rebooting or selecting a different entry.
                    operations._require_session().send("")
                    operations._require_session().wait_for("login:", 30)
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
                cycle_result = boot_cycle(
                    operations,
                    document,
                    args.mode,
                    args.username,
                    password,
                    nonce,
                    args.check_root,
                    args.hardware_watchdog,
                    from_uboot=args.from_uboot,
                )
                result.update(cycle_result)
        if result.get("probe_status") in {"fail", "timeout"}:
            result["error"] = (
                "probe reported fail"
                if result["probe_status"] == "fail"
                else "probe completion timeout"
            )
        elif result.get("phy_status") in {"fail", "timeout"}:
            result["error"] = f"PHY diagnostic {result['phy_status']}"
        else:
            result.update(passed=True, recovered=True)
    except (Exception, KeyboardInterrupt) as error:
        result["error"] = str(error) or type(error).__name__
        if args.action == "cycle" and args.hardware_watchdog:
            # A watchdog cycle may fail before its guest marker.  Keep the
            # same serial owner until a fresh firmware epoch is observed so a
            # recoverable timeout is never reported as an abandoned board.
            transcript = operations._log.getvalue()
            checkpoint = getattr(operations, "_recovery_checkpoint", len(transcript))
            recovery_suffix = transcript[checkpoint:]
            recovery_observed = _ordered_recovery_epoch(recovery_suffix)
            recovery_boundary = (
                "uboot-prompt" if recovery_observed else _ordered_recovery_boundary(recovery_suffix)
            )
            recovery_observed = recovery_boundary is not None
            if getattr(operations, "_boot_attempt_started", False) and not recovery_observed:
                try:
                    recovery = operations._require_session().wait_for_recovery_boundary(120)
                except (RuntimeError, TimeoutError) as recovery_error:
                    result["recovery_error"] = str(recovery_error)
                else:
                    recovery_suffix += recovery
                    recovery_observed = _ordered_recovery_epoch(recovery_suffix)
                    recovery_boundary = (
                        "uboot-prompt"
                        if recovery_observed
                        else _ordered_recovery_boundary(recovery_suffix)
                    )
                    recovery_observed = recovery_boundary is not None
            result["recovery_observed"] = recovery_observed
            result["recovered"] = recovery_observed
            if recovery_boundary is not None:
                result["recovery_boundary"] = recovery_boundary
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
