#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess

from tools.riscv.firefox_debug_tool import manifest, summarize
from tools.riscv.debian.rootfs import firefox_startup_profile
from tools.riscv.debian.rootfs.firefox_startup_profile import (
    _UBOOT_COMMAND_SAFE_LIMIT,
    _diagnostic_kernel_args,
    _profile_boot_commands,
    _wait_for_marker_line,
    _write_profile_result,
)


class _StaticSerial:
    def __init__(self, transcript: bytes) -> None:
        self.transcript = transcript

    def wait_for(self, marker: bytes, deadline: float, *, start: int = 0) -> bytes:
        if self.transcript.find(marker, start) < 0:
            raise TimeoutError(f"missing {marker!r}")
        return self.transcript

    def wait_for_any(self, markers, deadline: float, *, start: int = 0) -> bytes:
        found = [(self.transcript.find(marker, start), marker) for marker in markers]
        found = [entry for entry in found if entry[0] >= 0]
        if not found:
            raise TimeoutError("missing startup marker")
        return min(found)[1]


class FirefoxDebugToolTests(unittest.TestCase):
    def test_startup_capture_accepts_serial_events_without_user_console_markers(self) -> None:
        self.assertTrue(hasattr(firefox_startup_profile, "_capture_startup_markers"))
        records = firefox_startup_profile._capture_startup_markers(
            _StaticSerial(
                b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready pid=10\n"
                b"ASTERINAS_FIREFOX_WEB_EXEC pid=20\n"
                b"Marionette\tINFO\tListening on port 2828\n"
            ),
            42.0,
            0.0,
        )

        self.assertEqual(
            [record["name"] for record in records],
            ["x-socket-ready", "firefox-exec", "marionette"],
        )

    def test_startup_capture_rejects_missing_marionette_endpoint(self) -> None:
        with self.assertRaisesRegex(TimeoutError, "Marionette"):
            firefox_startup_profile._capture_startup_markers(
                _StaticSerial(
                    b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready pid=10\n"
                    b"ASTERINAS_FIREFOX_WEB_EXEC pid=20\n"
                ),
                42.0,
                0.0,
            )

    def test_startup_capture_accepts_firefox_exec_before_x_socket_log(self) -> None:
        records = firefox_startup_profile._capture_startup_markers(
            _StaticSerial(
                b"ASTERINAS_FIREFOX_WEB_EXEC pid=20\n"
                b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready pid=10\n"
                b"Marionette\tINFO\tListening on port 2828\n"
            ),
            42.0,
            0.0,
        )
        self.assertEqual(
            [record["name"] for record in records],
            ["firefox-exec", "x-socket-ready", "marionette"],
        )

    def test_startup_profile_publishes_private_structured_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "startup-profile.json"
            records = [
                {
                    "name": "marionette",
                    "host_elapsed_seconds": 12.5,
                    "evidence": "BOOT_MARIONETTE_PORT_READY guest_monotonic_ns=9",
                }
            ]

            _write_profile_result(output, records, b"serial\n", 12.5)

            value = json.loads(output.read_text())
            self.assertEqual(value["schema_version"], 1)
            self.assertEqual(value["markers"], records)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_startup_marker_capture_waits_for_the_complete_line(self) -> None:
        class FragmentedSerial:
            def __init__(self) -> None:
                self.transcript = b"prefix\nBOOT_MARIONETTE_PORT_READY"
                self.calls = []

            def wait_for(self, marker, deadline, *, start=0):
                self.calls.append((marker, deadline, start))
                if marker == b"\n":
                    self.transcript += b" guest_monotonic_ns=123\n"
                return self.transcript

        serial = FragmentedSerial()
        transcript = _wait_for_marker_line(serial, b"BOOT_MARIONETTE_PORT_READY", 42.0)

        self.assertTrue(transcript.endswith(b"guest_monotonic_ns=123\n"))
        self.assertEqual(serial.calls[1][0], b"\n")
        self.assertGreater(serial.calls[1][2], len(b"prefix\n"))

    def test_local_icache_profile_is_explicitly_diagnostic(self) -> None:
        args = _diagnostic_kernel_args(local_icache_diagnostic=True)

        self.assertIn("asterinas.vm_profile=1", args)
        self.assertIn("asterinas.vm_local_icache=1", args)

    def test_live_pc_sampler_is_bounded_loopback_only_and_binfmt_read_only(
        self,
    ) -> None:
        sampler = Path("tools/riscv/qemu_live_pc_sampler.sh")
        subprocess.run(["bash", "-n", sampler], check=True)
        source = sampler.read_text(encoding="utf-8")
        self.assertIn("target remote 127.0.0.1:$PORT", source)
        self.assertIn("thread apply all info registers pc ra sp", source)
        self.assertIn("ASTERINAS_KERNEL_SYMBOLS", source)
        self.assertIn("file $KERNEL_SYMBOLS", source)
        self.assertIn("timeout --foreground 10", source)
        self.assertIn("consecutive_failures >= 3", source)
        self.assertIn("detach", source)
        self.assertIn("binfmt_qemu_riscv64=absent", source)
        self.assertNotIn("/proc/sys/fs/binfmt_misc/register", source)

    def test_summarize_distinguishes_gdb_milestones_and_syscalls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gdb = root / "gdb.txt"
            trace = root / "trace.log"
            gdb.write_text(
                "ASTERINAS_SYSTEM_GDB_CONNECTED\n"
                "pc             0x1000\n"
                "Breakpoint 1, __libc_start_main@plt\n",
                encoding="utf-8",
            )
            trace.write_text(
                "1 mmap(NULL,1,0) = 0\n1 clone(...) = 2\n"
                "1 futex(...) = 0\n1 futex(...) = 0\n1 ppoll(...) = 0\n",
                encoding="utf-8",
            )
            value = summarize([gdb], trace)
        self.assertTrue(value["gdb_connected"])
        self.assertTrue(value["firefox_libc_start_breakpoint"])
        self.assertFalse(value["kernel_start_hit"])
        self.assertEqual(value["pc_values"], [0x1000])
        self.assertEqual(value["syscall_counts"]["futex"], 2)
        self.assertEqual(value["syscall_counts"]["clone"], 1)

    def test_manifest_is_deterministic_and_skips_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.txt").write_text("b\n", encoding="utf-8")
            (root / "a.txt").write_text("a\n", encoding="utf-8")
            (root / "link").symlink_to("a.txt")
            value = manifest(root)
        self.assertEqual(value["schema"], 1)
        self.assertEqual(
            [entry["path"] for entry in value["files"]], ["a.txt", "b.txt"]
        )
        json.dumps(value, sort_keys=True)

    def test_qemu_args_use_absolute_persistent_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "qemu-logs"
            result = subprocess.run(
                ["bash", "tools/qemu_args.sh", "riscv"],
                env={**os.environ, "ASTERINAS_QEMU_LOG_DIR": str(log_dir)},
                check=True,
                capture_output=True,
                text=True,
            )
        args = result.stdout
        repo_root = Path("tools/qemu_args.sh").resolve().parent.parent
        self.assertIn(f"file={repo_root}/test/initramfs/build/ext2.img", args)
        self.assertIn(f"file={repo_root}/test/initramfs/build/exfat.img", args)
        self.assertIn(f"file={repo_root}/test/initramfs/build/ltp_dev.img", args)
        self.assertIn(f"logfile={log_dir}/qemu.log", args)
        self.assertNotIn("file=./test/initramfs/build", args)

    def test_kernel_ktest_wrapper_isolated_and_persistent(self) -> None:
        wrapper = Path("tools/riscv/kernel_ktest.sh")
        subprocess.run(["bash", "-n", wrapper], check=True)
        source = wrapper.read_text(encoding="utf-8")
        self.assertIn('cd "$KERNEL_DIR"', source)
        self.assertIn("osdk-by-repo/main", source)
        self.assertIn("ASTERINAS_QEMU_LOG_DIR", source)
        self.assertIn("backups/asterinas-riscv-ktest", source)
        self.assertNotIn("cargo-osdk", source)

    def test_startup_profile_splits_long_diagnostic_bootargs(self) -> None:
        class FakeOperations:
            BOOTARGS = "console=ttyS0 " + ("asterinas.diagnostic=1 " * 80)

            def _boot_commands(self, framebuffer_address: int) -> tuple[str, ...]:
                return ("virtio scan", f'setenv bootargs "{self.BOOTARGS}"')

        commands = _profile_boot_commands(FakeOperations(), 0)
        self.assertEqual(commands[0], "virtio scan")
        self.assertTrue(commands[-1].startswith('setenv bootargs "${ast_bootargs_0}'))
        self.assertTrue(
            all(len(command) <= _UBOOT_COMMAND_SAFE_LIMIT for command in commands)
        )
        self.assertGreaterEqual(len(commands), 4)
        self.assertTrue(
            all("asterinas.diagnostic=1" in command for command in commands[1:-1])
        )


if __name__ == "__main__":
    unittest.main()
