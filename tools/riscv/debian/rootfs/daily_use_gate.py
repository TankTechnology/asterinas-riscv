#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Two-boot QEMU gate for the Debian daily-use console flow.

One QEMU process boots the freshly built ``desktop-m4-firefox`` image twice.
The first boot reaches multi-user, logs in on ttyS0 through the serial-getty
autologin, checks identity, sets the clock from the host, writes a nonce,
runs ``sudo`` and ``apt-get update`` over the QEMU slirp network, then asks
systemd to reboot.  The second boot logs in again, reads the nonce back and
raises SIGPWR to PID 1, which starts ``sigpwr.target`` and our
``asterinas-sigpwr-poweroff.service``; the guest powers off through SBI and
QEMU exits with the SiFive finisher status.

RISC-V QEMU ``virt`` has no ACPI, so the host cannot inject a power-button
event.  Sending SIGPWR to PID 1 exercises the same systemd path the ACPI
button would use; the transcript records the marker before the signal.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Mapping

from tools.riscv.debian.rootfs.contract import load_manifest
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import desktop_m5_qemu_argv
from tools.riscv.debian.rootfs.gate_protocol import (
    MAX_COMMAND_PAYLOAD_BYTES,
    GateResult,
)
from tools.riscv.debian.rootfs.gate_runtime import TerminationSignalState
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


PROFILE_NAME = "desktop-m4-firefox"
DAILY_USE_BOOTARGS = (
    "console=ttyS0 loglevel=4 init=/init systemd.unit=multi-user.target "
    "-- --root-init=systemd"
)
NONCE_PATH = "/home/asterinas/daily-use-nonce"
APT_TIMEOUT_SECONDS = 600.0
POWEROFF_TIMEOUT_SECONDS = 300.0
CLOCK_TOLERANCE_SECONDS = 3600
_FORBIDDEN_MARKERS = (
    (b"kernel panic", "kernel panic"),
    (b"uncaught panic:", "kernel panic"),
    (b"printing stack trace:", "kernel stack trace"),
    (b"ext2-fs error", "ext2 error"),
    (b"debian_rootfs_fail reason=", "stage1 failure"),
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class DailyCommand:
    """One nonce-framed shell command."""

    name: str
    payload: str
    begin_marker: str
    status_prefix: str
    end_marker: str


def _frame(boot_number: int, name: str, body: str, nonce: str) -> DailyCommand:
    token = hashlib.sha256(
        f"{nonce}:{boot_number}:{name}".encode("utf-8")
    ).hexdigest()[:20].upper()
    base = f"__ASTERINAS_DAILY_B{boot_number}_{token}"
    begin = f"{base}_BEGIN__"
    status = f"{base}_STATUS__"
    end = f"{base}_END__"
    payload = (
        f"printf '%s\\n' '{begin}'; "
        f"( {body} ); __asterinas_daily_status=$?; "
        f"printf '%s%d\\n' '{status}' \"$__asterinas_daily_status\"; "
        f"printf '%s\\n' '{end}'"
    )
    if len(payload.encode("utf-8")) > MAX_COMMAND_PAYLOAD_BYTES:
        raise ValueError(f"command {name} payload exceeds 64 KiB")
    return DailyCommand(name, payload, begin, status, end)


def _apt_body() -> str:
    return (
        "sudo apt-get update >/tmp/daily-apt.log 2>&1; "
        "rc=$?; "
        "printf 'APT_RC=%s\\n' \"$rc\"; "
        "if [ $rc -ne 0 ]; then "
        "printf 'APT_LOG_BEGIN\\n'; sudo tail -20 /tmp/daily-apt.log; "
        "printf 'APT_LOG_END\\n'; fi; "
        # Keep the frame status zero so the classifier can report the apt
        # return code and log instead of a generic nonzero-frame error.
        "exit 0"
    )


def daily_use_commands(
    nonce: str, host_epoch: int
) -> tuple[tuple[DailyCommand, ...], tuple[DailyCommand, ...]]:
    """Return the boot-one and boot-two command sets."""

    if not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise ValueError("daily-use nonce must be 64 lowercase hex digits")
    if isinstance(host_epoch, bool) or not isinstance(host_epoch, int):
        raise ValueError("host epoch must be an integer")
    boot_one = (
        _frame(1, "identity", "id -u; id -un; tty; uname -m", nonce),
        _frame(1, "release", "cat /etc/debian_version", nonce),
        _frame(1, "clock", "date -u +%s", nonce),
        _frame(
            1,
            "persistence",
            f"printf '%s\\n' '{nonce}' > {NONCE_PATH}; sync; cat {NONCE_PATH}",
            nonce,
        ),
        _frame(1, "sudo", "sudo -n true && printf '%s\\n' SUDO_OK", nonce),
        _frame(
            1,
            "resolver",
            "cat /etc/resolv.conf; "
            "getent ahostsv4 mirrors.tuna.tsinghua.edu.cn | head -1 "
            "|| printf 'DNS_LOOKUP_FAILED\\n'; true",
            nonce,
        ),
        _frame(
            1,
            "systemd",
            "for _i in $(seq 1 30); do "
            '_s=$(systemctl is-system-running 2>/dev/null || true); '
            'case "$_s" in running|degraded) break;; esac; sleep 1; done; '
            'printf \'%s\\n\' "$_s"; '
            "systemctl --failed --no-legend --plain 2>/dev/null || true",
            nonce,
        ),
        _frame(1, "apt", _apt_body(), nonce),
    )
    boot_two = (
        _frame(2, "persistence", f"cat {NONCE_PATH}", nonce),
    )
    return boot_one, boot_two


def require_daily_use_manifest(manifest: Any) -> None:
    """Fail closed unless the manifest is the daily-use profile."""

    if manifest.schema_version != 4 or manifest.profile != PROFILE_NAME:
        raise GateFailure(
            "rootfs manifest is not the schema-4 desktop-m4-firefox profile"
        )


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _lines(transcript: bytes | str) -> list[str]:
    text = (
        transcript.decode("utf-8", "replace")
        if isinstance(transcript, bytes)
        else transcript
    )
    return [line.strip() for line in _strip_ansi(text).splitlines()]


def _extract_outputs(
    lines: list[str], commands: tuple[DailyCommand, ...]
) -> tuple[dict[str, str] | None, str | None]:
    outputs: dict[str, str] = {}
    positions: list[tuple[int, int, int]] = []
    for command in commands:
        begins = [
            index for index, line in enumerate(lines) if line == command.begin_marker
        ]
        ends = [
            index for index, line in enumerate(lines) if line == command.end_marker
        ]
        statuses = [
            index
            for index, line in enumerate(lines)
            if line.startswith(command.status_prefix)
        ]
        if any(len(found) != 1 for found in (begins, ends, statuses)):
            return None, f"missing or duplicate protocol marker for {command.name}"
        begin, status, end = begins[0], statuses[0], ends[0]
        if not begin < status < end:
            return None, f"reordered protocol markers for {command.name}"
        status_text = lines[status][len(command.status_prefix) :]
        if not status_text.isdigit() or int(status_text, 10) != 0:
            return None, f"command {command.name} exited with status {status_text}"
        outputs[command.name] = "\n".join(lines[begin + 1 : status]).strip()
        positions.append((begin, status, end))
    if any(
        previous[2] >= following[0]
        for previous, following in zip(positions, positions[1:])
    ):
        return None, "protocol commands are out of order"
    return outputs, None


def classify_daily_use(
    transcript: bytes | str,
    *,
    expected_debian_release: str,
    nonce: str,
    host_epoch: int,
    boot_one: tuple[DailyCommand, ...],
    boot_two: tuple[DailyCommand, ...],
) -> GateResult:
    """Classify a completely drained two-boot transcript."""

    if isinstance(transcript, str):
        raw = transcript.encode("utf-8", "replace")
    else:
        raw = transcript
    lowered = raw.lower()
    for marker, reason in _FORBIDDEN_MARKERS:
        if marker in lowered:
            return GateResult(False, reason, None)

    lines = _lines(raw)
    for line in lines:
        if re.fullmatch(r"\S+ login:\s*", line) or re.fullmatch(
            r"Password:\s*", line
        ):
            return GateResult(False, "serial autologin did not start", None)
    kernel_starts = [
        index for index, line in enumerate(lines) if line == "Starting kernel ..."
    ]
    if len(kernel_starts) != 2:
        return GateResult(
            False,
            f"normal reboot evidence requires exactly two kernel starts, saw {len(kernel_starts)}",
            None,
        )
    multi_user = sum(
        1 for line in lines if "Reached target multi-user.target" in line
    )
    if multi_user < 2:
        return GateResult(False, "multi-user target was not reached twice", None)

    ready_one = f"__ASTERINAS_DAILY_READY_B1_{nonce}_1000_asterinas__"
    ready_two = f"__ASTERINAS_DAILY_READY_B2_{nonce}_1000_asterinas__"
    reboot_marker = f"__ASTERINAS_DAILY_REBOOT_{nonce}__"
    sigpwr_marker = f"__ASTERINAS_DAILY_SIGPWR_{nonce}__"
    fallback_marker = f"__ASTERINAS_DAILY_SIGPWR_FALLBACK_{nonce}__"
    for marker, reason in (
        (ready_one, "boot 1 serial autologin identity marker is missing"),
        (ready_two, "boot 2 serial autologin identity marker is missing"),
        (reboot_marker, "guest reboot marker is missing"),
        (sigpwr_marker, "SIGPWR marker is missing"),
    ):
        if marker not in lines:
            return GateResult(False, reason, None)
    if fallback_marker in lines:
        return GateResult(
            False, "SIGPWR did not power off the guest; fallback was required", None
        )

    first_start = kernel_starts[0]
    second_start = kernel_starts[1]
    positions = {
        "boot1-ready": lines.index(ready_one),
        "reboot": lines.index(reboot_marker),
        "boot2-ready": lines.index(ready_two),
        "sigpwr": lines.index(sigpwr_marker),
    }
    order = [
        first_start,
        positions["boot1-ready"],
        positions["reboot"],
        second_start,
        positions["boot2-ready"],
        positions["sigpwr"],
    ]
    if order != sorted(order):
        return GateResult(False, "daily-use milestones are out of order", None)

    outputs_one, error = _extract_outputs(lines, boot_one)
    if error is not None:
        return GateResult(False, f"boot 1: {error}", None)
    outputs_two, error = _extract_outputs(lines, boot_two)
    if error is not None:
        return GateResult(False, f"boot 2: {error}", None)

    identity = [line for line in outputs_one["identity"].splitlines() if line]
    if identity != ["1000", "asterinas", "/dev/ttyS0", "riscv64"]:
        return GateResult(False, f"identity mismatch: {identity!r}", None)
    clock_text = outputs_one["clock"].strip()
    if not clock_text.isdigit():
        return GateResult(False, f"guest clock is not an epoch: {clock_text!r}", None)
    skew = int(clock_text, 10) - host_epoch
    if abs(skew) > CLOCK_TOLERANCE_SECONDS:
        return GateResult(
            False,
            f"guest clock skew is {skew}s, outside +/-{CLOCK_TOLERANCE_SECONDS}s",
            None,
        )
    release = outputs_one["release"].strip()
    if release != expected_debian_release:
        return GateResult(
            False,
            f"Debian release mismatch: {release!r} != {expected_debian_release!r}",
            None,
        )
    if outputs_one["persistence"] != nonce:
        return GateResult(False, "boot 1 nonce was not written", None)
    if "SUDO_OK" not in outputs_one["sudo"]:
        return GateResult(False, "passwordless sudo failed", None)
    resolver = outputs_one["resolver"]
    if "nameserver 10.0.2.3" not in resolver:
        return GateResult(
            False, "image resolver is not the QEMU slirp resolver", None
        )
    if "DNS_LOOKUP_FAILED" in resolver or not re.search(
        r"\b\d{1,3}(?:\.\d{1,3}){3}\b", resolver
    ):
        return GateResult(False, "DNS resolution failed over slirp", None)
    systemd_lines = [
        line for line in outputs_one["systemd"].splitlines() if line.strip()
    ]
    if not systemd_lines or systemd_lines[0].strip() not in (
        "running",
        "degraded",
    ):
        return GateResult(
            False,
            f"unexpected systemd state: {systemd_lines[:1]!r}",
            None,
        )
    if "APT_RC=0" not in outputs_one["apt"]:
        return GateResult(False, "apt-get update failed", None)
    if outputs_two["persistence"] != nonce:
        return GateResult(False, "boot 2 did not read back the nonce", None)
    return GateResult(True, "pass", None)


class DailyUseOperations(ConcreteOperations):
    """Daily-use console gate over the shared persistent-root backend."""

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        arguments["allow_reboot"] = True
        return desktop_m5_qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(
            "boot.ext4",
            "debian-root.run.ext2",
            "daily-use.serial.log",
            "fsck.txt",
            "result.json",
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = dict(super().validate_inputs(config, snapshots))
        manifest = load_manifest(self.input_paths["manifest"])
        require_daily_use_manifest(manifest)
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
        commands = (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x88000000 /qemu-virt.dtb",
            "fdt addr 0x88000000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            f'setenv bootargs "{DAILY_USE_BOOTARGS}"',
        )
        for index, command in enumerate(commands, 1):
            self._send_uboot(session, command, index, deadline)
        marker = f"__ASTERINAS_DAILY_BOOT_{secrets.token_hex(8).upper()}__"
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

    def _wait_for_multi_user(
        self, serial: Any, config: GateConfig, *, start: int
    ) -> None:
        # Wait for the stable part of the line.  systemd colourises the log on
        # a tty and inserts an escape sequence between "target" and
        # "multi-user.target"; the target name itself stays contiguous.
        serial.wait_for(
            b"multi-user.target",
            time.monotonic() + config.boot_timeout,
            start=start,
        )

    @staticmethod
    def _wait_for_autologin(
        serial: Any,
        boot_number: int,
        nonce: str,
        config: GateConfig,
        *,
        start: int,
    ) -> None:
        marker = (
            f"__ASTERINAS_DAILY_READY_B{boot_number}_{nonce}_1000_asterinas__"
        )
        payload = (
            f"printf '__ASTERINAS_DAILY_READY_B{boot_number}_%s_%s_%s__\\n' "
            f"'{nonce}' \"$(id -u)\" \"$(id -un)\"\n"
        ).encode()
        deadline = time.monotonic() + config.boot_timeout
        while True:
            serial.send(payload, deadline)
            try:
                serial.wait_for(
                    marker.encode(),
                    min(deadline, time.monotonic() + 3.0),
                    start=start,
                )
            except TimeoutError:
                for line in _lines(serial.transcript):
                    if re.fullmatch(r"\S+ login:\s*", line) or re.fullmatch(
                        r"Password:\s*", line
                    ):
                        raise GateFailure(
                            f"boot {boot_number} serial autologin did not start"
                        ) from None
                if time.monotonic() >= deadline:
                    raise GateFailure(
                        f"boot {boot_number} shell did not become ready"
                    ) from None
                continue
            return

    @staticmethod
    def _wait_for_line(serial: Any, marker: str, deadline: float) -> None:
        """Wait until ``marker`` appears as a whole output line.

        A raw byte search would match the echoed command line, which contains
        the marker literal inside its printf arguments.
        """

        start = serial.checkpoint()
        while time.monotonic() < deadline:
            try:
                serial.wait_for(
                    b"\n", min(deadline, time.monotonic() + 0.5), start=start
                )
            except TimeoutError:
                pass
            start = serial.checkpoint()
            if marker in _lines(serial.transcript):
                return
        raise GateFailure(f"serial line was not observed: {marker}")

    @classmethod
    def _run_command(cls, serial: Any, command: DailyCommand, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        serial.send((command.payload + "\n").encode(), deadline)
        cls._wait_for_line(serial, command.end_marker, deadline)

    @classmethod
    def _send_marker_command(
        cls, serial: Any, marker: str, body: str, deadline: float
    ) -> None:
        serial.send(f"printf '%s\\n' '{marker}'; {body}\n".encode(), deadline)
        cls._wait_for_line(serial, marker, deadline)

    def run_protocol(self, session: Mapping[str, Any], config: GateConfig) -> None:
        serial = session["serial"]
        nonce = secrets.token_hex(32)
        host_epoch = int(time.time())
        boot_one, boot_two = daily_use_commands(nonce, host_epoch)
        self._nonce = nonce
        self._host_epoch = host_epoch
        self._boot_one_commands = boot_one
        self._boot_two_commands = boot_two

        self._boot_once(session, config, wait_prompt=True)
        first_start = serial.transcript.rfind(b"Starting kernel ...")
        self._wait_for_multi_user(serial, config, start=first_start)
        self._wait_for_autologin(serial, 1, nonce, config, start=first_start)
        for command in boot_one:
            timeout = (
                APT_TIMEOUT_SECONDS
                if command.name == "apt"
                else config.command_timeout
            )
            self._run_command(serial, command, timeout)

        reboot_marker = f"__ASTERINAS_DAILY_REBOOT_{nonce}__"
        reboot_start = serial.checkpoint()
        reboot_deadline = time.monotonic() + config.command_timeout
        self._send_marker_command(
            serial,
            reboot_marker,
            "sync; sudo systemctl reboot",
            reboot_deadline,
        )
        autoboot = b"Hit any key to stop autoboot"
        serial.wait_for(
            autoboot,
            time.monotonic() + config.boot_timeout,
            start=reboot_start,
        )
        prompt_start = serial.transcript.rfind(autoboot) + len(autoboot)
        reboot_deadline = time.monotonic() + config.boot_timeout
        serial.send(b" \n", reboot_deadline)
        serial.wait_for(b"=> ", reboot_deadline, start=prompt_start)
        second_boot_start = serial.checkpoint()
        self._boot_once(
            session, config, wait_prompt=False, start=second_boot_start
        )
        second_start = serial.transcript.rfind(b"Starting kernel ...")
        self._wait_for_multi_user(serial, config, start=second_start)
        self._wait_for_autologin(serial, 2, nonce, config, start=second_start)
        for command in boot_two:
            self._run_command(serial, command, config.command_timeout)

        sigpwr_marker = f"__ASTERINAS_DAILY_SIGPWR_{nonce}__"
        self._send_marker_command(
            serial,
            sigpwr_marker,
            "sleep 1; sudo kill -s PWR 1",
            time.monotonic() + config.command_timeout,
        )
        process = session["process"]
        try:
            returncode = process.wait(
                time.monotonic() + POWEROFF_TIMEOUT_SECONDS
            )
        except TimeoutError:
            fallback_marker = f"__ASTERINAS_DAILY_SIGPWR_FALLBACK_{nonce}__"
            self._send_marker_command(
                serial,
                fallback_marker,
                "sync; sudo systemctl poweroff",
                time.monotonic() + config.command_timeout,
            )
            try:
                returncode = process.wait(
                    time.monotonic() + POWEROFF_TIMEOUT_SECONDS
                )
            except TimeoutError as error:
                raise GateFailure(
                    "guest did not power off after SIGPWR or fallback"
                ) from error
            self._sigpwr_fallback = True
        if returncode != 0:
            raise GateFailure(
                f"guest poweroff exited with QEMU status {returncode}"
            )

    def request_quit(self, session: Mapping[str, Any], config: GateConfig) -> None:
        # A successful guest poweroff already ended QEMU; sending HMP quit to
        # the closed monitor would turn a pass into a BrokenPipe failure.
        process = session.get("process")
        if process is not None and process.poll() is not None:
            return
        super().request_quit(session, config)

    def classify_transcript(
        self, transcript: bytes, *, expected_debian_release: str
    ) -> GateResult:
        return classify_daily_use(
            transcript,
            expected_debian_release=expected_debian_release,
            nonce=self._nonce,
            host_epoch=self._host_epoch,
            boot_one=self._boot_one_commands,
            boot_two=self._boot_two_commands,
        )

    def hash_final_root(self, config: GateConfig, prepared: Any) -> str:
        del prepared
        self._require_config(config)
        output = self._require_output()
        root = config.output_directory / "debian-root.run.ext2"
        try:
            completed = subprocess.run(
                ["e2fsck", "-fn", str(root)],
                check=False,
                capture_output=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GateFailure(f"e2fsck did not complete: {error}") from error
        output.atomic_write(
            "fsck.txt", completed.stdout + completed.stderr
        )
        if completed.returncode != 0:
            raise GateFailure(
                f"e2fsck rejected the final root: status {completed.returncode}"
            )
        return output.sha256("debian-root.run.ext2")

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        del prepared
        self._require_config(config)
        output = self._require_output()
        result["qemu_argv"] = self._attempted_argv
        nonce = getattr(self, "_nonce", "")
        result["daily_use"] = {
            "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            "sigpwr_fallback": bool(getattr(self, "_sigpwr_fallback", False)),
            "host_epoch": getattr(self, "_host_epoch", None),
        }
        published = (
            transcript.replace(nonce.encode("ascii"), b"<nonce>")
            if nonce
            else transcript
        )
        output.atomic_write("daily-use.serial.log", published)
        document = json.dumps(result, indent=2, sort_keys=True) + "\n"
        output.atomic_write("result.json", document.encode("utf-8"))


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DailyUseOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config, operations, classifier=operations.classify_transcript
            )
        return 0 if result["passed"] else 1
    except GateFailure as error:
        print(f"daily-use gate failed: {error.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
