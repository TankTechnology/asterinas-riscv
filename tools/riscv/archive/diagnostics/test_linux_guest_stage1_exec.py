# SPDX-License-Identifier: MPL-2.0

"""The Linux-only comparison shim must not change Stage1's strict protocol."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "diagnostics/linux_guest_stage1_exec.c"


class LinuxGuestStage1ExecTests(unittest.TestCase):
    def test_linux_guest_mounts_devtmpfs_and_filters_kernel_extra_arguments(self) -> None:
        self.assertTrue(SOURCE.is_file(), "Linux comparison shim source is missing")
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler, "host C compiler is required")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake_stage1 = directory / "stage1"
            fake_stage1.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8"
            )
            fake_stage1.chmod(0o755)
            # Point the module list at paths that cannot exist, so the loading
            # loop is exercised without the test needing a built initramfs.
            # The shim must report each one and carry on: stage1's own failure
            # is what explains a boot that cannot find its root.
            missing_modules = f'-DBOOT_MODULES="{directory}/missing-1.ko", "{directory}/missing-2.ko",'
            mount_stub = directory / "mount_stub.c"
            mount_stub.write_text(
                '#include <stdio.h>\n#include <string.h>\n#include <sys/mount.h>\n'
                'int mount(const char *source, const char *target, '
                'const char *filesystemtype, unsigned long mountflags, '
                'const void *data) {\n'
                '  (void)mountflags; (void)data;\n'
                '  if (strcmp(source, "devtmpfs") != 0 || '
                'strcmp(target, "/dev") != 0 || '
                'strcmp(filesystemtype, "devtmpfs") != 0) return -1;\n'
                '  fputs("DEV_TMPFS_MOUNT_CALLED\\n", stderr);\n'
                '  return 0;\n}\n',
                encoding="utf-8",
            )
            shim = directory / "linux-init"
            subprocess.run(
                [
                    compiler,
                    "-std=c11",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    f'-DSTAGE1_EXEC_PATH="{fake_stage1}"',
                    missing_modules,
                    str(SOURCE),
                    str(mount_stub),
                    "-o",
                    str(shim),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                [
                    str(shim),
                    "--root-init=probe",
                    "--debug-console=user",
                    "earlycon=sbi",
                    "console=ttyS0,115200n8",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DEV_TMPFS_MOUNT_CALLED", result.stderr)
            # Every module is attempted, in the order it was listed, and a
            # failure to load one does not stop the handoff.
            self.assertIn("missing-1.ko", result.stderr)
            self.assertIn("missing-2.ko", result.stderr)
            self.assertLess(
                result.stderr.index("missing-1.ko"), result.stderr.index("missing-2.ko")
            )
            self.assertEqual(
                result.stdout,
                "--root-init=systemd\n--debug-console=root\n",
            )

    def test_boot_modules_are_listed_in_dependency_order(self) -> None:
        # The kernel cannot mount anything until these are in, and the order is
        # a dependency order rather than a preference: loading ext4 before jbd2
        # or crc16 fails. A silently reordered list would show up only as a
        # control boot that cannot find its root.
        source = SOURCE.read_text(encoding="utf-8")
        order = [
            "kernel/lib/crc16.ko",
            "kernel/crypto/crc32c_generic.ko",
            "kernel/fs/mbcache.ko",
            "kernel/fs/jbd2.ko",
            "kernel/fs/ext4.ko",
            "kernel/drivers/virtio/virtio_mmio.ko",
            "kernel/drivers/block/virtio_blk.ko",
            "kernel/drivers/gpu/drm/drm.ko",
            "kernel/drivers/gpu/drm/drm_kms_helper.ko",
            "kernel/drivers/gpu/drm/drm_shmem_helper.ko",
            "kernel/drivers/virtio/virtio_dma_buf.ko",
            "kernel/drivers/gpu/drm/virtio/virtio-gpu.ko",
            "kernel/drivers/virtio/virtio_input.ko",
        ]
        positions = [source.find(entry) for entry in order]
        # Collected rather than formatted into the assertion message: an
        # argument to assertX is evaluated eagerly, so indexing for a message
        # would raise on the very case the assertion exists to report.
        missing = [entry for entry, position in zip(order, positions) if position < 0]
        self.assertEqual(missing, [], "module entries missing from the shim")
        self.assertEqual(positions, sorted(positions), "module list is out of order")


if __name__ == "__main__":
    unittest.main()
