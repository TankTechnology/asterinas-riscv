#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the signed Debian ext4 profile through Stage1 and systemd twice.

The first boot exercises login, user management, the Debian shell, filesystem,
process and syscall workload, then updates apt, installs/removes/reinstalls
``hello``, controls a systemd service, and makes an HTTP request.  The service
requests a normal reboot.  The second boot proves that the account, package,
service enablement, network, and explicit state file survived ext4 journal
replay before emitting PASS.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import time
from typing import Any, Mapping

from tools.riscv.debian.rootfs.gate_protocol import (
    GateResult,
    _normalize_transcript,
    qemu_argv,
)
from tools.riscv.debian.rootfs.gate_runtime import (
    GateTermination,
    TerminationSignalState,
)
from tools.riscv.debian.rootfs.rootfs_gate import (
    GateConfig,
    GateFailure,
    parse_gate_args,
)
from tools.riscv.debian.rootfs.rootfs_gate_backend import (
    ConcreteOperations,
    _safe_output,
)
from tools.riscv.debian.rootfs.systemd_m2_gate import (
    orchestrate_systemd_m2_gate,
)
from tools.riscv.debian.rootfs.contract import load_manifest


SYSTEMD_EXT4_BOOTARGS = (
    "console=ttyS0 loglevel=4 init=/init "
    "-- --root-fs=ext4 --root-init=systemd"
)
NETWORK_RECOVERY_ONLY = False
_READY_RE = re.compile(
    r"\ADEBIAN_EXT4_READY boot=([12]) arch=([^ ]+) release=([^ ]+) "
    r"pid1=([^ ]+) rootfs=([^ ]+) shell=([01]) process=([01]) "
    r"filesystem=([01]) syscall=([01]) apt_update=([01]) package=([^ ]+) "
    r"dpkg=([01]) login=([01]) user=([01]) apt_install=([01]) "
    r"apt_remove=([01]) service=([01]) network=([01]) persist=([01])\Z"
)
_PASS = "DEBIAN_EXT4_PASS boot=2 persist=1"
_NETWORK_MANAGER_RECOVERY_RE = re.compile(
    r"^DEBIAN_EXT4_NETWORK_RECOVERY interface=[^ ]+ manager=1$"
)
_LOGIN_PASS = "ASTERINAS_LOGIN_PASS uid=1000 home=/home/debian shell=/bin/bash"
_M5_PASS = {
    1: "DEBIAN_EXT4_M5_PASS boot=1 apt=1 maintainer=1 triggers=1 locks=1 recovery=1 packages=1 upgrade=1",
    2: "DEBIAN_EXT4_M5_PASS boot=2 apt=1 maintainer=1 triggers=1 locks=1 recovery=1 packages=1 upgrade=1 persist=1",
}
_REQUIRED_PROGRESS = (
    ("DEBIAN_EXT4_PROGRESS step=loopback-sockets-done", 2),
    ("DEBIAN_EXT4_PROGRESS step=service-restart-journald-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=oneshot-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=timer-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=forking-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=service-restart-on-failure-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=service-dependency-order-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=socket-activation-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=dbus-logind-session-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=apt-interruption-recovery-done", 1),
    ("DEBIAN_EXT4_PROGRESS step=network-recovery-done", 2),
    ("DEBIAN_EXT4_PROGRESS step=ext4-consistency-done", 1),
)
_FATAL_MARKERS = (
    b"DEBIAN_EXT4_FAIL reason=",
    b"DEBIAN_ROOTFS_FAIL reason=",
    b"debian_rootfs_fail reason=",
    b"kernel panic",
    b"uncaught panic:",
    b"printing stack trace:",
    b"ext4-fs error",
    b"buffer i/o error",
)
_STAGE1_HANDOFF_MARKERS = (
    "DEBIAN_STAGE1_PROGRESS step=start mode=systemd",
    "DEBIAN_STAGE1_PROGRESS step=root-found device=/dev/vdb",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=root-mount",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=dev-bind",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=api-directories",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=run-mount",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=tmp-mount",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=chroot",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=chdir",
    "DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec",
)


def wait_for_guest_marker(serial: Any, marker: bytes, deadline: float, *, start: int = 0) -> bytes:
    """Stop immediately when the guest reports failure instead of timing out."""
    observed = serial.wait_for_any((marker, *_FATAL_MARKERS), deadline, start=start)
    if observed != marker:
        raise GateFailure(f"guest failure while waiting for {marker.decode()}: {observed.decode()}")
    return serial.transcript


def systemd_ext4_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Use the normal reboot contract with one user-mode VirtIO NIC."""

    base = qemu_argv(**{**arguments, "allow_reboot": True})
    nic_index = base.index("-nic")
    if base[nic_index : nic_index + 2] != ("-nic", "none"):
        raise ValueError("unexpected QEMU NIC contract")
    return (
        *base[:nic_index],
        "-netdev",
        "user,id=debian-ext4",
        "-device",
        "virtio-net-device,netdev=debian-ext4",
        *base[nic_index + 2 :],
    )


def _classify_failure(reason: str) -> GateResult:
    return GateResult(False, reason, None)


def classify_systemd_ext4(
    transcript: bytes | str, *, expected_debian_release: str
) -> GateResult:
    """Require two ordered ext4 root boots and the apt/persistence evidence."""

    normalized = _normalize_transcript(transcript)
    if isinstance(normalized, GateResult):
        return normalized
    text, lines = normalized
    lowered = text.lower()
    for marker in _FATAL_MARKERS:
        if marker in lowered.encode():
            return _classify_failure(f"fatal transcript marker: {marker.decode()}")

    handoff_positions = []
    for marker in _STAGE1_HANDOFF_MARKERS:
        positions = [index for index, line in enumerate(lines) if line == marker]
        if len(positions) != 2:
            qualifier = "duplicate" if len(positions) > 2 else "missing"
            return _classify_failure(f"{qualifier} Stage1 handoff marker: {marker}")
        if positions[0] >= positions[1]:
            return _classify_failure(f"Stage1 handoff marker is reordered: {marker}")
        handoff_positions.append(positions)
    if any(
        handoff_positions[index][boot] >= handoff_positions[index + 1][boot]
        for boot in (0, 1)
        for index in range(len(handoff_positions) - 1)
    ):
        return _classify_failure("Stage1 handoff markers are out of order")

    ready: dict[int, list[tuple[int, re.Match[str]]]] = {1: [], 2: []}
    for index, line in enumerate(lines):
        match = _READY_RE.fullmatch(line)
        if match is not None:
            ready[int(match.group(1), 10)].append((index, match))
    if len(ready[1]) != 1:
        return _classify_failure("missing or duplicate ext4 boot 1 READY marker")
    if len(ready[2]) != 1:
        return _classify_failure("missing or duplicate ext4 boot 2 READY marker")
    pass_positions = [index for index, line in enumerate(lines) if line == _PASS]
    if len(pass_positions) != 1:
        return _classify_failure("missing or duplicate ext4 PASS marker")
    login_positions = [index for index, line in enumerate(lines) if line.startswith(_LOGIN_PASS)]
    if len(login_positions) != 1:
        return _classify_failure("missing or duplicate interactive login marker")
    m5_positions = {
        boot: [index for index, line in enumerate(lines) if line == marker]
        for boot, marker in _M5_PASS.items()
    }
    if any(len(positions) != 1 for positions in m5_positions.values()):
        return _classify_failure("missing or duplicate M5 package lifecycle marker")
    for marker, expected_count in _REQUIRED_PROGRESS:
        positions = [index for index, line in enumerate(lines) if line == marker]
        if len(positions) != expected_count:
            return _classify_failure(f"missing or duplicate required progress marker: {marker}")
    network_manager_recoveries = [
        line for line in lines if _NETWORK_MANAGER_RECOVERY_RE.fullmatch(line)
    ]
    if len(network_manager_recoveries) != 2:
        return _classify_failure(
            "network manager restart recovery evidence is missing or incomplete"
        )
    starts = [index for index, line in enumerate(lines) if line == "Starting kernel ..."]
    if len(starts) != 2:
        return _classify_failure("normal reboot requires exactly two kernel starts")
    firmware = [
        index
        for index, line in enumerate(lines)
        if line.startswith("OpenSBI ") or line.startswith("U-Boot ")
    ]
    first_index, first = ready[1][0]
    second_index, second = ready[2][0]
    if not (starts[0] < first_index < starts[1] < second_index < pass_positions[0]):
        return _classify_failure("ext4 boot markers are reordered")
    if not (starts[0] < login_positions[0] < first_index):
        return _classify_failure("interactive login marker is reordered")
    if not (
        first_index > m5_positions[1][0]
        and second_index > m5_positions[2][0]
        and m5_positions[1][0] < starts[1] < m5_positions[2][0]
    ):
        return _classify_failure("M5 package lifecycle markers are reordered")
    if not any(first_index < index < starts[1] for index in firmware):
        return _classify_failure("firmware restart evidence is missing")

    for boot, match in ((1, first), (2, second)):
        fields = match.groupdict()
        if match.group(2) != "riscv64":
            return _classify_failure(f"boot {boot} architecture identity mismatch")
        if match.group(3) != expected_debian_release:
            return _classify_failure(f"boot {boot} Debian release identity mismatch")
        if match.group(4) != "systemd" or match.group(5) != "ext4":
            return _classify_failure(f"boot {boot} root PID/filesystem identity mismatch")
        required_fields = (6, 7, 8, 9, 12, 13, 14, 15, 16, 17, 18, 19)
        if any(match.group(index) != "1" for index in required_fields):
            return _classify_failure(f"boot {boot} shell workload evidence is incomplete")
        if match.group(11) != "hello":
            return _classify_failure(f"boot {boot} package identity mismatch")
        expected_apt = "1" if boot == 1 else "0"
        if match.group(10) != expected_apt:
            return _classify_failure(f"boot {boot} apt evidence mismatch")
        del fields
    return GateResult(True, "pass", None)


def classify_network_recovery(
    transcript: bytes | str, *, expected_debian_release: str
) -> GateResult:
    """Require two boots whose network manager restart completed successfully."""

    del expected_debian_release
    normalized = _normalize_transcript(transcript)
    if isinstance(normalized, GateResult):
        return normalized
    text, lines = normalized
    lowered = text.lower()
    for marker in _FATAL_MARKERS:
        if marker in lowered.encode():
            return _classify_failure(f"fatal transcript marker: {marker.decode()}")
    starts = [index for index, line in enumerate(lines) if line == "Starting kernel ..."]
    if len(starts) != 2:
        return _classify_failure("network recovery requires exactly two kernel starts")
    ordered_positions = []
    for boot, start in enumerate(starts, 1):
        end = starts[1] if boot == 1 else len(lines)
        required = (
            *_STAGE1_HANDOFF_MARKERS,
            "DEBIAN_EXT4_PROGRESS step=network-recovery-start",
            "DEBIAN_EXT4_PROGRESS step=network-recovery-done",
            f"DEBIAN_EXT4_NETWORK_ONLY boot={boot}",
        )
        positions = []
        for marker in required:
            matches = [index for index in range(start, end) if lines[index] == marker]
            if len(matches) != 1:
                return _classify_failure(f"boot {boot} missing or duplicate marker: {marker}")
            positions.append(matches[0])
        if positions != sorted(positions):
            return _classify_failure(f"boot {boot} network recovery markers are reordered")
        managers = [
            index for index in range(start, end)
            if _NETWORK_MANAGER_RECOVERY_RE.fullmatch(lines[index])
        ]
        if len(managers) != 1 or not positions[-2] < managers[0] < positions[-1]:
            return _classify_failure(f"boot {boot} network manager recovery evidence is incomplete")
        ordered_positions.append(positions[-1])
    boot_markers = [
        line for line in lines if line.startswith("DEBIAN_EXT4_NETWORK_ONLY boot=")
    ]
    if boot_markers != ["DEBIAN_EXT4_NETWORK_ONLY boot=1", "DEBIAN_EXT4_NETWORK_ONLY boot=2"]:
        return _classify_failure("network recovery boot markers are missing or reordered")
    managers = [line for line in lines if _NETWORK_MANAGER_RECOVERY_RE.fullmatch(line)]
    if len(managers) != 2:
        return _classify_failure("network manager restart recovery evidence is incomplete")
    if lines.count("DEBIAN_EXT4_NETWORK_ONLY_PASS boots=2 manager=1") != 1:
        return _classify_failure("network recovery completion marker is missing")
    if lines.index("DEBIAN_EXT4_NETWORK_ONLY_PASS boots=2 manager=1") <= ordered_positions[1]:
        return _classify_failure("network recovery completion marker is reordered")
    if any(line.startswith("DEBIAN_EXT4_NETWORK_RECOVERY ") and
           not _NETWORK_MANAGER_RECOVERY_RE.fullmatch(line) for line in lines):
        return _classify_failure("network recovery contains a fallback or malformed manager marker")
    if not any(
        ordered_positions[0] < index < starts[1]
        and line.startswith(("OpenSBI ", "U-Boot "))
        for index, line in enumerate(lines)
    ):
        return _classify_failure("network recovery firmware restart evidence is missing")
    return GateResult(True, "pass", None)


class SystemdExt4Operations(ConcreteOperations):
    """Concrete QEMU operations for the systemd-ext4-m3 profile."""

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return systemd_ext4_qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(
            "boot.ext4",
            "debian-root.run.ext2",
            "systemd-ext4.serial.log",
            "result.json",
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = dict(ConcreteOperations.validate_inputs(self, config, snapshots))
        manifest = load_manifest(self.input_paths["manifest"])
        if manifest.schema_version != 9 or manifest.profile != "systemd-ext4-m3":
            raise GateFailure("rootfs manifest is not the systemd-ext4-m3 profile")
        if manifest.filesystem.filesystem_type != "ext4":
            raise GateFailure("systemd-ext4-m3 manifest does not identify ext4")
        identity["profile"] = manifest.profile
        return identity

    def launch(self, config: GateConfig, prepared: Any) -> dict[str, Any]:
        return super().launch(config, prepared, 1)

    def _boot_once(
        self,
        session: Mapping[str, Any],
        config: GateConfig,
        *,
        wait_prompt: bool,
        start: int = 0,
    ) -> None:
        deadline = time.monotonic() + config.boot_timeout
        serial = session["serial"]
        if wait_prompt:
            serial.wait_for(b"=> ", deadline, start=start)
        bootargs = SYSTEMD_EXT4_BOOTARGS
        if NETWORK_RECOVERY_ONLY:
            bootargs = bootargs.replace(
                " -- --root-fs=ext4", " asterinas.network_recovery_only=1 -- --root-fs=ext4"
            )
        commands = (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x88000000 /qemu-virt.dtb",
            "fdt addr 0x88000000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            f'setenv bootargs "{bootargs}"',
        )
        for index, command in enumerate(commands, 1):
            self._send_uboot(session, command, index, deadline)
        marker = f"__ASTERINAS_EXT4_BOOT_{secrets.token_hex(8).upper()}__"
        split = len(marker) // 2
        serial.send(
            (
                f"setenv ast_ba {marker[:split]}; "
                f"setenv ast_bb {marker[split:]}; "
                "echo ${ast_ba}${ast_bb}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x88000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline, start=start)

    def run_protocol(self, session: Mapping[str, Any], config: GateConfig) -> None:
        if NETWORK_RECOVERY_ONLY:
            self._run_network_recovery_protocol(session, config)
            return
        self._boot_once(session, config, wait_prompt=True)
        serial = session["serial"]
        login_ready = b"DEBIAN_EXT4_LOGIN_READY boot=1"
        wait_for_guest_marker(serial, login_ready, time.monotonic() + config.boot_timeout)
        login_start = serial.checkpoint()
        serial.send(b"debian\n", time.monotonic() + config.boot_timeout)
        serial.wait_for(b"Password:", time.monotonic() + config.boot_timeout, start=login_start)
        serial.send(b"asterinas\n", time.monotonic() + config.boot_timeout)
        login_deadline = time.monotonic() + config.boot_timeout
        serial.wait_for(
            b"Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY",
            login_deadline,
            start=login_start,
        )
        # Asterinas' serial getty does not always redraw bash's prompt after
        # the login banner.  The banner is emitted only after PAM has opened
        # the user session; leave a short scheduling window before sending
        # the shell probe instead of depending on prompt rendering.
        # login(1) prints the Debian banner before it has finished handing the
        # controlling tty to the user's shell.  Give bash enough time to run
        # its startup files so the first probe line is not consumed during
        # that handoff.
        time.sleep(5.0)
        # A newline wakes shells whose prompt is not rendered on this serial
        # console, while still leaving an ordinary interactive session.
        serial.send(b"\n", time.monotonic() + config.boot_timeout)
        time.sleep(1.0)
        login_command = (
            "if [ \"$(id -u)\" = 1000 ] && [ \"$HOME\" = /home/debian ] "
            "&& [ \"$SHELL\" = /bin/bash ] && [ -n \"$TERM\" ] "
            "&& [ -n \"$PATH\" ] && [ -c \"$(tty)\" ]; then "
            "touch /run/asterinas-debian-login/complete; "
            "printf 'ASTERINAS_LOGIN_PASS uid=%s home=%s shell=%s tty=%s term=%s\\n' "
            "\"$(id -u)\" \"$HOME\" \"$SHELL\" \"$(tty)\" \"$TERM\"; "
            "else echo ASTERINAS_LOGIN_FAIL; fi\n"
        )
        serial.send(login_command.encode(), time.monotonic() + config.boot_timeout)
        login_result = serial.wait_for_any(
            (b"ASTERINAS_LOGIN_PASS", b"ASTERINAS_LOGIN_FAIL"),
            time.monotonic() + config.boot_timeout,
        )
        if login_result == b"ASTERINAS_LOGIN_FAIL":
            raise GateFailure("interactive login probe failed")
        serial.send(b"exit\n", time.monotonic() + config.boot_timeout)
        ready1 = b"DEBIAN_EXT4_READY boot=1"
        transcript = wait_for_guest_marker(serial, ready1, time.monotonic() + config.boot_timeout)
        restart_start = transcript.rfind(ready1) + len(ready1)
        reboot_deadline = time.monotonic() + config.boot_timeout
        autoboot = b"Hit any key to stop autoboot"
        transcript = serial.wait_for(autoboot, reboot_deadline, start=restart_start)
        prompt_start = transcript.rfind(autoboot) + len(autoboot)
        serial.send(b" \n", reboot_deadline)
        serial.wait_for(b"=> ", reboot_deadline, start=prompt_start)
        second_boot_start = serial.checkpoint()
        self._boot_once(
            session,
            config,
            wait_prompt=False,
            start=second_boot_start,
        )
        wait_for_guest_marker(serial,
            b"DEBIAN_EXT4_PASS boot=2",
            time.monotonic() + config.boot_timeout,
        )

    def _run_network_recovery_protocol(
        self, session: Mapping[str, Any], config: GateConfig
    ) -> None:
        self._boot_once(session, config, wait_prompt=True)
        serial = session["serial"]
        wait_for_guest_marker(
            serial,
            b"DEBIAN_EXT4_NETWORK_ONLY boot=1",
            time.monotonic() + config.boot_timeout,
        )
        restart_start = serial.checkpoint()
        reboot_deadline = time.monotonic() + config.boot_timeout
        autoboot = b"Hit any key to stop autoboot"
        transcript = serial.wait_for(autoboot, reboot_deadline, start=restart_start)
        prompt_start = transcript.rfind(autoboot) + len(autoboot)
        serial.send(b" \n", reboot_deadline)
        serial.wait_for(b"=> ", reboot_deadline, start=prompt_start)
        second_boot_start = serial.checkpoint()
        self._boot_once(session, config, wait_prompt=False, start=second_boot_start)
        wait_for_guest_marker(
            serial,
            b"DEBIAN_EXT4_NETWORK_ONLY_PASS boots=2 manager=1",
            time.monotonic() + config.boot_timeout,
        )

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        del prepared
        self._require_config(config)
        result["qemu_argv"] = self._attempted_argv
        output = self._require_output()
        output.atomic_write("systemd-ext4.serial.log", transcript)
        output.atomic_write("result.json", (json.dumps(result, indent=2, sort_keys=True) + "\n").encode())


def main(arguments: list[str] | None = None) -> int:
    try:
        global NETWORK_RECOVERY_ONLY
        raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
        NETWORK_RECOVERY_ONLY = "--network-recovery-only" in raw_arguments
        config = parse_gate_args(
            [argument for argument in raw_arguments if argument != "--network-recovery-only"]
        )
        _safe_output(config.output_directory)
        with TerminationSignalState(), SystemdExt4Operations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config,
                operations,
                classifier=(classify_network_recovery if NETWORK_RECOVERY_ONLY else classify_systemd_ext4),
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-systemd-ext4-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-systemd-ext4-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
