# SPDX-License-Identifier: MPL-2.0

"""Unit tests for the x86_64 Debian boot and LMbench disk tooling."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BOOT_SCRIPT = REPOSITORY_ROOT / "tools/x86/debian/boot_debian.sh"
PREPARE_SCRIPT = REPOSITORY_ROOT / "tools/x86/debian/prepare_lmbench_disk.sh"


def run_script(
    script: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class DebianBootScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.kernel = self.directory / "kernel.elf"
        self.initramfs = self.directory / "initramfs.cpio"
        self.root_image = self.directory / "debian-root.ext2"
        for path in (self.kernel, self.initramfs, self.root_image):
            path.write_bytes(b"fixture")

    def required_arguments(self) -> list[str]:
        return [
            "--kernel",
            str(self.kernel),
            "--initramfs",
            str(self.initramfs),
            "--root-image",
            str(self.root_image),
        ]

    def parse(self, *arguments: str) -> list[str]:
        result = run_script(BOOT_SCRIPT, *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_append_routes_init_arguments_past_the_kernel(self) -> None:
        # Stage1 takes `--root-init` and `--root-fs` as init argv, which the
        # kernel only produces for tokens after a literal `--`.
        append = self.parse("--print-append", *self.required_arguments())

        self.assertEqual(len(append), 1)
        self.assertIn("console=hvc0", append[0])
        self.assertIn("init=/init", append[0])
        self.assertIn("-- --root-init=systemd --root-fs=ext4", append[0])

    def test_argv_attaches_stdio_to_the_virtio_console_only(self) -> None:
        """Only one character device may own stdio.

        `-nographic` gives stdio to the serial port implicitly, which then
        collides with the mux chardev: QEMU refuses to start with "cannot use
        stdio by multiple character devices".
        """
        argv = self.parse("--print-argv", *self.required_arguments())

        self.assertNotIn("-nographic", argv)
        stdio_users = [
            value
            for value in argv
            if value.startswith("stdio") or value.endswith("=stdio")
        ]
        self.assertEqual(stdio_users, ["stdio,id=mux,mux=on,signal=off"])
        self.assertEqual(argv[argv.index("-serial") + 1], "none")
        self.assertEqual(argv[argv.index("-device") + 1], "virtio-serial-pci")
        self.assertIn("virtconsole,chardev=mux", argv)

    def test_print_argv_boots_the_kernel_without_a_grub_device(self) -> None:
        argv = self.parse("--print-argv", *self.required_arguments())

        self.assertEqual(argv[0], "qemu-system-x86_64")
        self.assertIn("-kernel", argv)
        self.assertEqual(argv[argv.index("-kernel") + 1], str(self.kernel))
        self.assertIn("-initrd", argv)
        self.assertEqual(argv[argv.index("-initrd") + 1], str(self.initramfs))
        # The root disk is the only drive, so no auxiliary image has to exist.
        self.assertEqual(argv.count("-drive"), 1)
        drive = argv[argv.index("-drive") + 1]
        self.assertIn(f"file={self.root_image}", drive)
        # A transitional virtio-blk-pci is not recognised by this kernel: with
        # the default interface the guest gets no block device at all and
        # Stage1 reports `root-discovery-timeout`.
        block = argv[argv.index("virtio-blk-pci,drive=rootdisk,"
                                "disable-legacy=on,disable-modern=off")]
        self.assertTrue(block.endswith("disable-legacy=on,disable-modern=off"))
        # No `-bios`: the kernel is handed straight to the VMM.
        self.assertNotIn("-bios", argv)
        self.assertNotIn("-cdrom", argv)

    def test_loglevel_defaults_quiet_enough_to_boot(self) -> None:
        # Above this the kernel prints a line per syscall and the guest slows
        # down by an order of magnitude.
        append = self.parse("--print-append", *self.required_arguments())

        self.assertIn("loglevel=4", append[0])
        raised = self.parse(
            "--print-append", *self.required_arguments(), "--loglevel", "7"
        )
        self.assertIn("loglevel=7", raised[0])

    def test_rejects_a_missing_required_path(self) -> None:
        result = run_script(BOOT_SCRIPT, "--kernel", str(self.kernel))

        self.assertEqual(result.returncode, 2)
        self.assertIn("--initramfs is required", result.stderr)

    def test_rejects_an_unsafe_memory_value(self) -> None:
        result = run_script(
            BOOT_SCRIPT, *self.required_arguments(), "--memory", "4G; rm -rf /"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe --memory value", result.stderr)

    def test_rejects_an_unknown_argument(self) -> None:
        result = run_script(BOOT_SCRIPT, *self.required_arguments(), "--nope")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)


class PrepareLmbenchDiskTests(unittest.TestCase):
    def test_help_documents_the_required_inputs(self) -> None:
        result = run_script(PREPARE_SCRIPT, "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        for required in ("--base-root", "--runtime", "--output"):
            self.assertIn(required, result.stdout)

    def test_refuses_to_run_without_the_base_root(self) -> None:
        result = run_script(PREPARE_SCRIPT, "--runtime", "/dev/null")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--base-root is required", result.stderr)

    def test_guest_assets_are_present(self) -> None:
        assets = REPOSITORY_ROOT / "tools/x86/debian/lmbench_guest"

        for name in ("lmbench-run.sh", "lmbench-run.service"):
            with self.subTest(name=name):
                self.assertTrue((assets / name).is_file())

    def test_workload_masks_what_this_kernel_cannot_run(self) -> None:
        # logind and rsyslog restart in a tight loop and add scheduling jitter
        # to the RPC and signal measurements.
        workload = (
            REPOSITORY_ROOT / "tools/x86/debian/lmbench_guest/lmbench-run.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("systemd-logind.service", workload)
        self.assertIn("rsyslog.service", workload)
        self.assertIn("make results", workload)
        self.assertIn("LMBENCH_RUN_NATIVE_EXIT", workload)


if __name__ == "__main__":
    sys.exit(unittest.main())
