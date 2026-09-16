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
            self.assertEqual(
                result.stdout,
                "--root-init=systemd\n--debug-console=root\n",
            )


if __name__ == "__main__":
    unittest.main()
