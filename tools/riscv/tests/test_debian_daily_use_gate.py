#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Unit tests for the Debian daily-use QEMU gate."""

from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.riscv.debian.rootfs.daily_use_gate import (
    PROFILE_NAME,
    DailyCommand,
    DailyUseOperations,
    classify_daily_use,
    daily_use_commands,
    require_daily_use_manifest,
)
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
NONCE = "0123456789abcdef" * 4
HOST_EPOCH = 1761436800
RELEASE = "13.7"
BOOT_ONE_VALUES = {
    "identity": "1000\nasterinas\n/dev/ttyS0\nriscv64",
    "release": RELEASE,
    "clock": str(HOST_EPOCH),
    "persistence": NONCE,
    "sudo": "SUDO_OK",
    "resolver": (
        "nameserver 10.0.2.3\n"
        "203.0.113.10 STREAM mirrors.tuna.tsinghua.edu.cn"
    ),
    "systemd": "running",
    "apt": "APT_RC=0\nReading package lists...",
}


def _append_command(
    lines: list[str], command: DailyCommand, value: str, status: int = 0
) -> None:
    lines.append(command.begin_marker)
    lines.extend(value.splitlines())
    lines.append(f"{command.status_prefix}{status}")
    lines.append(command.end_marker)


def _transcript(
    *, boot_one_values: dict[str, str] | None = None
) -> str:
    values = dict(BOOT_ONE_VALUES if boot_one_values is None else boot_one_values)
    boot_one, boot_two = daily_use_commands(NONCE, HOST_EPOCH)
    lines = [
        "Starting kernel ...",
        "Reached target multi-user.target - Multi-User System.",
        f"__ASTERINAS_DAILY_READY_B1_{NONCE}_1000_asterinas__",
    ]
    for command in boot_one:
        _append_command(lines, command, values[command.name])
    lines.append(f"__ASTERINAS_DAILY_REBOOT_{NONCE}__")
    lines.extend(
        (
            "Starting kernel ...",
            "Reached target multi-user.target - Multi-User System.",
            f"__ASTERINAS_DAILY_READY_B2_{NONCE}_1000_asterinas__",
        )
    )
    for command in boot_two:
        _append_command(lines, command, NONCE)
    lines.append(f"__ASTERINAS_DAILY_SIGPWR_{NONCE}__")
    return "\n".join(lines) + "\n"


def _classify(
    transcript: str, *, expected_release: str = RELEASE
):
    boot_one, boot_two = daily_use_commands(NONCE, HOST_EPOCH)
    return classify_daily_use(
        transcript,
        expected_debian_release=expected_release,
        nonce=NONCE,
        host_epoch=HOST_EPOCH,
        boot_one=boot_one,
        boot_two=boot_two,
    )


class DailyUseProtocolTests(unittest.TestCase):
    def test_commands_are_nonce_bound_and_bounded(self) -> None:
        boot_one, boot_two = daily_use_commands(NONCE, HOST_EPOCH)

        self.assertEqual(
            tuple(command.name for command in boot_one),
            (
                "identity",
                "release",
                "clock",
                "persistence",
                "sudo",
                "resolver",
                "systemd",
                "apt",
            ),
        )
        self.assertEqual(tuple(command.name for command in boot_two), ("persistence",))
        markers = [
            marker
            for command in (*boot_one, *boot_two)
            for marker in (command.begin_marker, command.end_marker)
        ]
        self.assertEqual(len(markers), len(set(markers)))
        for command in (*boot_one, *boot_two):
            self.assertLessEqual(len(command.payload.encode()), 64 * 1024)
            self.assertIn(f"__ASTERINAS_DAILY_B", command.begin_marker)
        self.assertIn(NONCE, boot_one[3].payload)
        other_boot_two = daily_use_commands("f" * 64, HOST_EPOCH)[1]
        self.assertNotEqual(
            boot_two[0].begin_marker, other_boot_two[0].begin_marker
        )

    def test_rejects_invalid_nonce_and_epoch(self) -> None:
        with self.assertRaises(ValueError):
            daily_use_commands("xyz", HOST_EPOCH)
        with self.assertRaises(ValueError):
            daily_use_commands(NONCE, True)

    def test_accepts_complete_two_boot_transcript(self) -> None:
        result = _classify(_transcript())

        self.assertTrue(result.passed, result.reason)

    def test_rejects_login_and_password_prompts(self) -> None:
        for prompt in ("ubuntu-MS-7D76 login:", "Password:"):
            with self.subTest(prompt=prompt):
                result = _classify(_transcript() + prompt + "\n")
                self.assertFalse(result.passed)
                self.assertIn("autologin", result.reason)

    def test_rejects_missing_autologin_identity(self) -> None:
        transcript = _transcript().replace(
            f"__ASTERINAS_DAILY_READY_B2_{NONCE}_1000_asterinas__", ""
        )
        result = _classify(transcript)

        self.assertFalse(result.passed)
        self.assertIn("boot 2 serial autologin", result.reason)

    def test_rejects_apt_failure(self) -> None:
        values = dict(BOOT_ONE_VALUES)
        values["apt"] = "APT_RC=1\nAPT_LOG_BEGIN\nerror\nAPT_LOG_END"
        result = _classify(_transcript(boot_one_values=values))

        self.assertFalse(result.passed)
        self.assertIn("apt-get update failed", result.reason)

    def test_rejects_nonzero_command_status(self) -> None:
        boot_one, _ = daily_use_commands(NONCE, HOST_EPOCH)
        transcript = _transcript().replace(
            f"{boot_one[0].status_prefix}0",
            f"{boot_one[0].status_prefix}1",
        )
        result = _classify(transcript)

        self.assertFalse(result.passed)
        self.assertIn("command identity exited with status 1", result.reason)

    def test_rejects_nonce_mismatch(self) -> None:
        _, boot_two = daily_use_commands(NONCE, HOST_EPOCH)
        marker = boot_two[0]
        transcript = _transcript().replace(
            f"{marker.begin_marker}\n{NONCE}\n{marker.status_prefix}0",
            f"{marker.begin_marker}\n{'f' * 64}\n{marker.status_prefix}0",
        )
        result = _classify(transcript)

        self.assertFalse(result.passed)
        self.assertIn("boot 2 did not read back the nonce", result.reason)

    def test_rejects_out_of_order_milestones(self) -> None:
        transcript = _transcript().replace(
            f"Starting kernel ...\nReached target multi-user.target - Multi-User System.\n"
            f"__ASTERINAS_DAILY_READY_B1_{NONCE}_1000_asterinas__",
            f"__ASTERINAS_DAILY_READY_B1_{NONCE}_1000_asterinas__\n"
            "Starting kernel ...\nReached target multi-user.target - Multi-User System.",
        )
        result = _classify(transcript)

        self.assertFalse(result.passed)
        self.assertIn("out of order", result.reason)

    def test_rejects_wrong_tty(self) -> None:
        values = dict(BOOT_ONE_VALUES)
        values["identity"] = "1000\nasterinas\n/dev/console\nriscv64"
        result = _classify(_transcript(boot_one_values=values))

        self.assertFalse(result.passed)
        self.assertIn("identity mismatch", result.reason)

    def test_rejects_stale_guest_clock(self) -> None:
        values = dict(BOOT_ONE_VALUES)
        values["clock"] = "0"
        result = _classify(_transcript(boot_one_values=values))

        self.assertFalse(result.passed)
        self.assertIn("clock skew", result.reason)

    def test_rejects_wrong_resolver_and_dns_failure(self) -> None:
        values = dict(BOOT_ONE_VALUES)
        values["resolver"] = (
            "nameserver 1.1.1.1\n"
            "203.0.113.10 STREAM mirrors.tuna.tsinghua.edu.cn"
        )
        result = _classify(_transcript(boot_one_values=values))
        self.assertFalse(result.passed)
        self.assertIn("slirp resolver", result.reason)

        values["resolver"] = "nameserver 10.0.2.3\nDNS_LOOKUP_FAILED"
        result = _classify(_transcript(boot_one_values=values))
        self.assertFalse(result.passed)
        self.assertIn("DNS resolution failed", result.reason)

    def test_accepts_a_last_login_line(self) -> None:
        result = _classify(
            _transcript() + "Last login: Tue Oct 27 12:00:00 UTC 2026 from 10.0.2.2\n"
        )

        self.assertTrue(result.passed, result.reason)

    def test_rejects_sigpwr_fallback(self) -> None:
        result = _classify(
            _transcript() + f"__ASTERINAS_DAILY_SIGPWR_FALLBACK_{NONCE}__\n"
        )

        self.assertFalse(result.passed)
        self.assertIn("fallback was required", result.reason)

    def test_rejects_kernel_panic_and_release_mismatch(self) -> None:
        panic = _classify(_transcript() + "Kernel panic - not syncing: test\n")
        self.assertFalse(panic.passed)
        self.assertEqual(panic.reason, "kernel panic")
        release = _classify(_transcript(), expected_release="13.8")
        self.assertFalse(release.passed)
        self.assertIn("Debian release mismatch", release.reason)

    def test_rejects_duplicate_or_missing_kernel_starts(self) -> None:
        duplicate = _classify(_transcript() + "Starting kernel ...\n")
        self.assertFalse(duplicate.passed)
        self.assertIn("two kernel starts", duplicate.reason)
        missing = _classify(
            _transcript().replace("Starting kernel ...\n", "", 1)
        )
        self.assertFalse(missing.passed)
        self.assertIn("two kernel starts", missing.reason)


class _ScriptedSerial:
    """Echo every sent payload and append one scripted response per send."""

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self._buffer = bytearray()
        self._responses = list(responses or [])
        self.sent: list[bytes] = []

    @property
    def transcript(self) -> bytes:
        return bytes(self._buffer)

    def checkpoint(self) -> int:
        return len(self._buffer)

    def send(self, payload: bytes, deadline: float) -> None:
        del deadline
        self.sent.append(payload)
        self._buffer.extend(payload)
        if self._responses:
            self._buffer.extend(self._responses.pop(0))

    def wait_for(self, marker: bytes, deadline: float, *, start: int = 0) -> bytes:
        del deadline
        if self._buffer.find(marker, start) < 0:
            raise TimeoutError("marker not seen")
        return self.transcript

    def wait_for_any(
        self, markers, deadline: float, *, start: int = 0
    ) -> bytes:
        del deadline
        found = [
            (self._buffer.find(marker, start), marker)
            for marker in markers
            if self._buffer.find(marker, start) >= 0
        ]
        if not found:
            raise TimeoutError("markers not seen")
        return min(found)[1]


class DailyUseSerialProtocolTests(unittest.TestCase):
    def test_wait_for_line_ignores_the_echoed_command(self) -> None:
        command = DailyCommand(
            "probe",
            "printf '%s\\n' '__DAILY_END__'",
            "__DAILY_BEGIN__",
            "__DAILY_STATUS__",
            "__DAILY_END__",
        )
        with self.assertRaises(GateFailure):
            DailyUseOperations._run_command(
                _ScriptedSerial(), command, timeout=0.2
            )

        serial = _ScriptedSerial([b"__DAILY_END__\n"])
        DailyUseOperations._run_command(serial, command, timeout=1.0)
        self.assertEqual(len(serial.sent), 1)

    def test_wait_for_line_rejects_a_marker_only_inside_the_echo(self) -> None:
        serial = _ScriptedSerial()
        serial.send(b"printf '__DAILY_END__'\n", 1.0)
        with self.assertRaises(GateFailure):
            DailyUseOperations._wait_for_line(serial, "__DAILY_END__", time.monotonic() + 0.2)

    def test_autologin_rejects_a_login_prompt(self) -> None:
        serial = _ScriptedSerial([b"debian login: "])
        with self.assertRaises(GateFailure) as raised:
            DailyUseOperations._wait_for_autologin(
                serial,
                1,
                NONCE,
                SimpleNamespace(boot_timeout=0.5),
                start=0,
            )
        self.assertIn("autologin", raised.exception.reason)


class DailyUseQemuArgvTests(unittest.TestCase):
    def test_argv_adds_one_slirp_nic_and_allows_guest_reboot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uboot = root / "u-boot"
            boot = root / "boot.ext4"
            root_disk = root / "root.ext2"
            monitor = root / "monitor.sock"
            for path in (uboot, boot, root_disk):
                path.write_bytes(b"fixture")
            argv = DailyUseOperations._qemu_argv(
                uboot=uboot,
                boot_disk=boot,
                root_disk=root_disk,
                monitor_socket=monitor,
            )

        self.assertEqual(argv.count("-netdev"), 1)
        self.assertIn("user,id=net0", argv)
        self.assertIn("virtio-net-device,netdev=net0", argv)
        self.assertNotIn("-nic", argv)
        self.assertNotIn("-no-reboot", argv)


class DailyUseMakefileTests(unittest.TestCase):
    def test_make_target_is_explicit_and_networkless(self) -> None:
        variables = (
            "DEBIAN_KERNEL",
            "DEBIAN_UBOOT",
            "DEBIAN_DTB",
            "DEBIAN_STAGE1_INITRAMFS",
            "DEBIAN_ROOT_IMAGE",
            "DEBIAN_ROOT_MANIFEST",
            "DEBIAN_PACKAGES_LOCK",
            "DEBIAN_PACKAGE_CHECKSUMS",
            "DEBIAN_DAILY_USE_GATE_OUTPUT",
        )
        dry_run = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-n",
                "test_riscv_debian_daily_use_gate",
                *(f"{name}=/tmp/{name.lower()}" for name in variables),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

        self.assertIn(
            "python3 -m tools.riscv.debian.rootfs.daily_use_gate", dry_run
        )
        for value in variables:
            self.assertIn(f"/tmp/{value.lower()}", dry_run)
        for forbidden in ("curl", "wget", "debootstrap", "http://", "https://"):
            self.assertNotIn(forbidden, dry_run)


class DailyUseManifestTests(unittest.TestCase):
    def test_requires_the_schema_four_daily_use_profile(self) -> None:
        require_daily_use_manifest(
            SimpleNamespace(schema_version=4, profile=PROFILE_NAME)
        )
        for manifest in (
            SimpleNamespace(schema_version=5, profile=PROFILE_NAME),
            SimpleNamespace(schema_version=4, profile="desktop-m4"),
            SimpleNamespace(schema_version=4, profile="browser-web"),
        ):
            with self.subTest(manifest=manifest):
                with self.assertRaises(GateFailure):
                    require_daily_use_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
