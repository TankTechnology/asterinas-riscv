#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.systemd_ext4_gate import (
    SYSTEMD_EXT4_BOOTARGS,
    classify_systemd_ext4,
    systemd_ext4_qemu_argv,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"


def _transcript() -> str:
    return """OpenSBI v1
U-Boot 2025
Starting kernel ...
DEBIAN_STAGE1_PROGRESS step=start mode=systemd
DEBIAN_STAGE1_PROGRESS step=root-found device=/dev/vdb
DEBIAN_STAGE1_PROGRESS step=handoff-done action=root-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=dev-bind
DEBIAN_STAGE1_PROGRESS step=handoff-done action=api-directories
DEBIAN_STAGE1_PROGRESS step=handoff-done action=run-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=tmp-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=chroot
DEBIAN_STAGE1_PROGRESS step=handoff-done action=chdir
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
ASTERINAS_LOGIN_PASS uid=1000 home=/home/debian shell=/bin/bash tty=/dev/console term=linux
DEBIAN_EXT4_READY boot=1 arch=riscv64 release=13.6 pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=1 package=hello dpkg=1 login=1 user=1 apt_install=1 apt_remove=1 service=1 network=1 persist=1
U-Boot 2025
Starting kernel ...
DEBIAN_STAGE1_PROGRESS step=start mode=systemd
DEBIAN_STAGE1_PROGRESS step=root-found device=/dev/vdb
DEBIAN_STAGE1_PROGRESS step=handoff-done action=root-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=dev-bind
DEBIAN_STAGE1_PROGRESS step=handoff-done action=api-directories
DEBIAN_STAGE1_PROGRESS step=handoff-done action=run-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=tmp-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=chroot
DEBIAN_STAGE1_PROGRESS step=handoff-done action=chdir
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
DEBIAN_EXT4_READY boot=2 arch=riscv64 release=13.6 pid1=systemd rootfs=ext4 shell=1 process=1 filesystem=1 syscall=1 apt_update=0 package=hello dpkg=1 login=1 user=1 apt_install=1 apt_remove=1 service=1 network=1 persist=1
DEBIAN_EXT4_PASS boot=2 persist=1
"""


class SystemdExt4ClassifierTests(unittest.TestCase):
    def test_accepts_two_boot_apt_and_persistence_evidence(self) -> None:
        result = classify_systemd_ext4(
            _transcript(), expected_debian_release="13.6"
        )
        self.assertTrue(result.passed, result.reason)

    def test_rejects_non_ext4_or_missing_apt_evidence(self) -> None:
        broken = _transcript().replace("rootfs=ext4", "rootfs=ext2").replace(
            "apt_update=1", "apt_update=0"
        )
        result = classify_systemd_ext4(broken, expected_debian_release="13.6")
        self.assertFalse(result.passed)

    def test_rejects_missing_login_or_service_evidence(self) -> None:
        for field in ("login", "service"):
            broken = _transcript().replace(f"{field}=1", f"{field}=0")
            result = classify_systemd_ext4(
                broken, expected_debian_release="13.6"
            )
            self.assertFalse(result.passed, field)

    def test_qemu_contract_has_one_slirp_virtio_nic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                (root / name).write_bytes(b"input")
            argv = systemd_ext4_qemu_argv(
                uboot=root / "u-boot",
                boot_disk=root / "boot.ext4",
                root_disk=root / "root.ext2",
                monitor_socket=root / "monitor.sock",
                smp=4,
                dtb_enabled_cpu_count=4,
            )
        self.assertEqual(argv.count("-netdev"), 1)
        self.assertIn("user,id=debian-ext4", argv)
        self.assertIn("virtio-net-device,netdev=debian-ext4", argv)
        self.assertNotIn("-nic", argv)


class SystemdExt4BuilderTests(unittest.TestCase):
    def test_ext4_profile_injects_real_root_evidence_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            stage = work / "stage"
            for relative in (
                "etc",
                "etc/systemd/system",
                "usr/bin",
                "var/lib/dbus",
                "var/lib/dpkg",
                "var/cache/apt/archives",
                "var/lib/apt/lists",
                "var/log",
                "tmp",
                "var/tmp",
            ):
                (stage / relative).mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                (
                    "/bin/bash",
                    "-c",
                    'source "$1"; PROFILE="$3"; configure_profile 1; '
                    'WORK_DIR="$2"; configure_and_normalize_rootfs',
                    "builder-test",
                    str(BUILD_SCRIPT),
                    str(work),
                    "systemd-ext4-m3",
                ),
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = stage / "usr/lib/asterinas/systemd-ext4-evidence"
            login_helper = stage / "usr/local/sbin/asterinas-login"
            unit = stage / "etc/systemd/system/asterinas-debian-ext4.service"
            wanted = (
                stage
                / "etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service"
            )
            serial_override = (
                stage
                / "etc/systemd/system/console-getty.service.d/asterinas-serial.conf"
            )
            self.assertEqual(
                evidence.read_bytes(),
                (BUILD_SCRIPT.parent / "systemd_ext4_evidence.sh").read_bytes(),
            )
            self.assertEqual(
                login_helper.read_bytes(),
                (BUILD_SCRIPT.parent / "asterinas_login.sh").read_bytes(),
            )
            self.assertIn(
                "ExecStart=/usr/lib/asterinas/systemd-ext4-evidence", unit.read_text()
            )
            self.assertTrue(wanted.is_symlink())
            self.assertIn("TTYPath=/dev/ttyS0", serial_override.read_text())
            self.assertIn("StandardInput=tty-force", serial_override.read_text())
            self.assertIn("TTYReset=yes", serial_override.read_text())


class SystemdExt4BootargsTests(unittest.TestCase):
    def test_bootargs_select_ext4_systemd_handoff(self) -> None:
        self.assertIn("--root-fs=ext4", SYSTEMD_EXT4_BOOTARGS)
        self.assertIn("--root-init=systemd", SYSTEMD_EXT4_BOOTARGS)


if __name__ == "__main__":
    unittest.main()
