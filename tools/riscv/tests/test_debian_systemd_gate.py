#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Systemd/ext4 two-boot gate tests split from the rootfs suite."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs.gate_protocol import (
    MAX_TRANSCRIPT_BYTES,
    classify_systemd_m2,
)
from tools.riscv.debian.rootfs.systemd_m2_gate import (
    SYSTEMD_EXT4_M3_BOOTARGS,
    SYSTEMD_M2_BOOTARGS,
    SystemdM2Operations,
    orchestrate_systemd_m2_gate,
    systemd_m2_bootargs,
    systemd_m2_qemu_argv,
)


class DebianSystemdM2GateTests(unittest.TestCase):
    RELEASE = "13.6"

    @staticmethod
    def successful_transcript() -> bytes:
        return b"\n".join(
            (
                b"Starting kernel ...",
                b"[\x1b[0;32m  OK  \x1b[0m] Mounted \x1b[0;1;39mrun-lock.mount\x1b[0m - Legacy Locks Directory /run/lock.",
                b"DEBIAN_SYSTEMD_M2_TMPFS boot=1",
                b"DEBIAN_SYSTEMD_M2_LOGIND boot=1 state=active",
                b"DEBIAN_SYSTEMD_M2_READY boot=1 arch=riscv64 release=13.6",
                b"OpenSBI v1.7",
                b"U-Boot 2025.07",
                b"Starting kernel ...",
                b"[  OK  ] Mounted run-lock.mount - Legacy Locks Directory /run/lock.",
                b"\x1b[!p\x1b]104\x07\x1b[?7h\x1b[6n\x1b[32766;32766H\x1b[6n"
                b"DEBIAN_SYSTEMD_M2_TMPFS boot=2",
                b"DEBIAN_SYSTEMD_M2_READY boot=2 arch=riscv64 release=13.6",
                b"DEBIAN_SYSTEMD_M2_PASS boot=2",
                b"",
            )
        )

    def test_classifier_requires_ordered_two_boot_normal_reboot_evidence(self) -> None:
        result = classify_systemd_m2(
            self.successful_transcript(), expected_debian_release=self.RELEASE
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "pass")

    def test_classifier_rejects_failure_reorder_duplicates_overflow_and_third_boot(
        self,
    ) -> None:
        good = self.successful_transcript()
        ready1 = b"DEBIAN_SYSTEMD_M2_READY boot=1 arch=riscv64 release=13.6"
        ready2 = b"DEBIAN_SYSTEMD_M2_READY boot=2 arch=riscv64 release=13.6"
        cases = (
            (good + b"DEBIAN_SYSTEMD_M2_FAIL reason=systemd-packages\n", "failure"),
            (good.replace(ready1, ready2, 1), "boot 1"),
            (good.replace(ready1, ready1 + b"\n" + ready1), "duplicate"),
            (good.replace(b"Starting kernel ...\n", b"", 1), "reboot"),
            (
                good.replace(b"OpenSBI v1.7\nU-Boot 2025.07\n", b""),
                "firmware restart",
            ),
            (good.replace(b"DEBIAN_SYSTEMD_M2_PASS boot=2\n", b""), "PASS"),
            (
                good + b"DEBIAN_SYSTEMD_M2_READY boot=3 arch=riscv64 release=13.6\n",
                "third",
            ),
            (b"x" * (MAX_TRANSCRIPT_BYTES + 1), "8 MiB"),
            (good + b"Kernel panic - not syncing\n", "fatal"),
            (
                good + b"Failed to acquire watch file descriptor: Invalid argument\n",
                "mount monitor",
            ),
            (
                good + b"Failed to drain libmount events: Invalid argument\n",
                "mount monitor",
            ),
            (
                good + b"Mount process finished, but there is no mount.\n",
                "mount protocol",
            ),
            (
                good.replace(
                    b"DEBIAN_SYSTEMD_M2_TMPFS boot=2\n",
                    b"",
                    1,
                ),
                "tmpfs",
            ),
            (
                good.replace(
                    b"DEBIAN_SYSTEMD_M2_LOGIND boot=1 state=active\n",
                    b"",
                    1,
                ),
                "logind",
            ),
            (
                good.replace(
                    b"[  OK  ] Mounted run-lock.mount - Legacy Locks Directory /run/lock.\n",
                    b"",
                    1,
                ),
                "run-lock.mount",
            ),
        )
        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = classify_systemd_m2(
                    transcript, expected_debian_release=self.RELEASE
                )
                self.assertFalse(result.passed)
                self.assertIn(reason.lower(), result.reason.lower())

    def test_qemu_contract_allows_firmware_reboot_and_forwards_exact_init_argv(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            paths = []
            for filename in ("u-boot", "boot.ext4", "root.ext2"):
                path = directory / filename
                path.write_bytes(filename.encode())
                paths.append(path)
            argv = systemd_m2_qemu_argv(
                uboot=paths[0],
                boot_disk=paths[1],
                root_disk=paths[2],
                monitor_socket=directory / "monitor.sock",
            )

        self.assertNotIn("-no-reboot", argv)
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertEqual(argv[argv.index("-m") + 1], "2G")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")
        self.assertEqual(argv[argv.index("-display") + 1], "none")
        self.assertEqual(
            SYSTEMD_M2_BOOTARGS,
            "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd",
        )
        self.assertEqual(systemd_m2_bootargs(), SYSTEMD_M2_BOOTARGS)
        self.assertEqual(
            systemd_m2_bootargs("ext4"),
            SYSTEMD_EXT4_M3_BOOTARGS,
        )
        with self.assertRaises(ValueError):
            systemd_m2_bootargs("xfs")

    def test_orchestrator_invalidates_stale_pass_and_always_tears_down(self) -> None:
        class FakeOperations:
            def __init__(self, failure: str | None = None) -> None:
                self.failure = failure
                self.calls: list[str] = []
                self.published: dict[str, object] | None = {"passed": True}

            def call(self, name: str, value: object = None) -> object:
                self.calls.append(name)
                if self.failure == name:
                    raise RuntimeError(name)
                return value

            def invalidate(self, config: object) -> None:
                del config
                self.published = None
                self.call("invalidate")

            def snapshot_inputs(self, config: object) -> dict[str, str]:
                del config
                return self.call("snapshot", {"root_image": "before"})

            def validate_inputs(
                self, config: object, snapshots: object
            ) -> dict[str, str]:
                del config, snapshots
                return self.call("validate", {"debian_release": "13.6"})

            def prepare(
                self, config: object, snapshots: object, identity: object
            ) -> str:
                del config, snapshots, identity
                return self.call("prepare", "prepared")

            def launch(self, config: object, prepared: object) -> str:
                del config, prepared
                return self.call("launch", "session")

            def run_protocol(self, session: object, config: object) -> None:
                del session, config
                self.call("protocol")

            def request_quit(self, session: object, config: object) -> None:
                del session, config
                self.call("quit")

            def close_monitor(self, session: object) -> None:
                del session
                self.call("close")

            def cleanup_process(self, session: object, config: object) -> None:
                del session, config
                self.call("cleanup")

            def drain_serial(self, session: object, config: object) -> bytes:
                del session, config
                return self.call(
                    "drain", DebianSystemdM2GateTests.successful_transcript()
                )

            def hash_final_root(self, config: object, prepared: object) -> str:
                del config, prepared
                return self.call("hash", "after")

            def publish(
                self,
                config: object,
                prepared: object,
                transcript: bytes,
                result: dict[str, object],
            ) -> None:
                del config, prepared, transcript
                self.call("publish")
                self.published = dict(result)

        for failure in ("protocol", "quit", "close", "cleanup", "drain"):
            with self.subTest(failure=failure):
                operations = FakeOperations(failure)
                result = orchestrate_systemd_m2_gate(object(), operations)
                self.assertFalse(result["passed"])
                self.assertNotEqual(operations.published, {"passed": True})
                self.assertEqual(
                    [
                        name
                        for name in operations.calls
                        if name in ("close", "cleanup", "drain")
                    ],
                    ["close", "cleanup", "drain"],
                )

    def test_concrete_protocol_drives_asterinas_twice_without_saved_environment(
        self,
    ) -> None:
        class FakeSerial:
            def __init__(self) -> None:
                self.waited: list[bytes] = []
                self.sent: list[bytes] = []

            def wait_for(
                self, marker: bytes, deadline: float, *, start: int = 0
            ) -> bytes:
                del deadline, start
                self.waited.append(marker)
                return b"\n".join(self.waited)

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                self.sent.append(payload)

            def checkpoint(self) -> int:
                return len(b"\n".join(self.waited))

        serial = FakeSerial()
        operations = object.__new__(SystemdM2Operations)
        config = mock.Mock(boot_timeout=1.0)
        with mock.patch.object(operations, "_send_uboot") as send_uboot:
            operations.run_protocol({"serial": serial}, config)

        self.assertEqual(send_uboot.call_count, 14)
        commands = [call.args[1] for call in send_uboot.call_args_list]
        self.assertEqual(commands.count(f'setenv bootargs "{SYSTEMD_M2_BOOTARGS}"'), 2)
        self.assertNotIn("saveenv", "\n".join(commands))
        self.assertEqual(
            serial.waited.count(b"Starting kernel ..."),
            2,
        )
        self.assertIn(b"DEBIAN_SYSTEMD_M2_READY boot=1", serial.waited)
        self.assertIn(b"DEBIAN_SYSTEMD_M2_PASS boot=2", serial.waited)
        self.assertIn(b" \n", serial.sent)

    def test_concrete_protocol_selects_ext4_stage1_handoff(self) -> None:
        serial = mock.Mock()
        serial.wait_for.return_value = b"Starting kernel ..."
        operations = object.__new__(SystemdM2Operations)
        config = mock.Mock(boot_timeout=1.0, root_fs="ext4")
        with mock.patch.object(operations, "_send_uboot") as send_uboot:
            operations._boot_once(
                {"serial": serial},
                config,
                wait_prompt=False,
            )

        commands = [call.args[1] for call in send_uboot.call_args_list]
        self.assertIn(
            f'setenv bootargs "{SYSTEMD_EXT4_M3_BOOTARGS}"',
            commands,
        )


if __name__ == "__main__":
    unittest.main()
