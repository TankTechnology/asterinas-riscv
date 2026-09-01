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
from tools.riscv.debian.rootfs.firefox_startup_profile import (
    _UBOOT_COMMAND_SAFE_LIMIT,
    _profile_boot_commands,
)


class FirefoxDebugToolTests(unittest.TestCase):
    def test_live_pc_sampler_is_bounded_loopback_only_and_binfmt_read_only(self) -> None:
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
        self.assertEqual([entry["path"] for entry in value["files"]], ["a.txt", "b.txt"])
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
        self.assertIn("file=/home/", args)
        self.assertIn("/test/initramfs/build/ext2.img", args)
        self.assertIn("/test/initramfs/build/exfat.img", args)
        self.assertIn("/test/initramfs/build/ltp_dev.img", args)
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
        self.assertTrue(all(len(command) <= _UBOOT_COMMAND_SAFE_LIMIT for command in commands))
        self.assertGreaterEqual(len(commands), 4)
        self.assertTrue(all("asterinas.diagnostic=1" in command for command in commands[1:-1]))


if __name__ == "__main__":
    unittest.main()
