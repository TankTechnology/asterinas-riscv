#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from tools.riscv.tests.support.debian_rootfs import *


class DebianStage1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def make_stage1_publication_sources(self) -> tuple[Path, Path]:
        source_directory = self.directory / "stage1 publication sources"
        source_directory.mkdir(exist_ok=True)
        init_source = source_directory / "init"
        archive_source = source_directory / "initramfs.cpio"
        init_source.write_bytes(b"new-stage1-init")
        archive_source.write_bytes(b"new-stage1-archive")
        return init_source, archive_source

    def stage1_publication_snapshot(
        self, output_directory: Path
    ) -> dict[str, tuple[bytes, int] | None]:
        snapshot = {}
        for name in ("init", "initramfs.cpio"):
            path = output_directory / name
            snapshot[name] = (
                (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                if path.exists()
                else None
            )
        return snapshot

    def wait_for_child(self, process_id: int) -> int:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            waited_id, status = os.waitpid(process_id, os.WNOHANG)
            if waited_id == process_id:
                return status
            time.sleep(0.01)
        os.kill(process_id, signal.SIGKILL)
        os.waitpid(process_id, 0)
        self.fail("stage1 publication child did not exit within two seconds")

    def compile_stage1(
        self,
        output: Path,
        define: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "cc",
            "-std=c11",
            "-O2",
            "-static",
            "-Wall",
            "-Wextra",
            "-Werror",
        ]
        if define is not None:
            command.append(f"-D{define}")
        command.extend(
            (
                str(STAGE1_SOURCE),
                str(STAGE1_DEBUG_CONSOLE_SOURCE),
                str(STAGE1_PROBE_SOURCE),
                "-o",
                str(output),
            )
        )
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def compile_debug_console_harness(
        self, output: Path
    ) -> subprocess.CompletedProcess[str]:
        harness = self.directory / "debug-console-harness.c"
        harness.write_text(
            '#include "stage1_debug_console.h"\n'
            "#include <string.h>\n"
            "int main(int argc, char **argv)\n"
            "{\n"
            "    if (argc == 2) {\n"
            "        return stage1_prepare_debug_console(argv[1]);\n"
            "    }\n"
            '    if (argc == 3 && strcmp(argv[2], "isolated") == 0) {\n'
            "        return stage1_prepare_isolated_debug_console(argv[1]);\n"
            "    }\n"
            "    return 2;\n"
            "}\n"
        )
        return subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(STAGE1_DEBUG_CONSOLE_INCLUDE),
                str(harness),
                str(STAGE1_DEBUG_CONSOLE_SOURCE),
                "-o",
                str(output),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def run_builder(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
        cwd: Path = REPOSITORY_ROOT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(STAGE1_BUILD_SCRIPT), *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_debug_console_exits_on_sigterm(self) -> None:
        binary = self.directory / "debug-console-harness"
        compilation = self.compile_debug_console_harness(binary)
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        root = self.directory / "root"
        root.mkdir()
        subprocess.run([binary, root], check=True, capture_output=True)
        bashrc = root / "run/asterinas-debug-console.bashrc"

        with subprocess.Popen(
            ["/bin/bash", "--noprofile", "--rcfile", str(bashrc), "-i"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as shell:
            try:
                shell.stdin.write(b"printf 'CONSOLE_%s\\n' LIVE\n")
                shell.stdin.flush()
                output = b""
                deadline = time.monotonic() + 5
                while b"\nCONSOLE_LIVE\n" not in output:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, output)
                    readable, _, _ = select.select([shell.stdout], [], [], remaining)
                    self.assertTrue(readable, output)
                    chunk = os.read(shell.stdout.fileno(), 4096)
                    self.assertTrue(chunk, output)
                    output += chunk
                # Keep stdin open: EOF must not accidentally satisfy this test.
                shell.send_signal(signal.SIGTERM)
                # Bash defers traps while waiting in an interactive read.  A
                # newline wakes that read so the TERM trap can run normally;
                # systemd supplies the separate bounded fallback asserted below.
                shell.stdin.write(b"\n")
                shell.stdin.flush()
                try:
                    shell.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail("interactive debug console ignored SIGTERM")
                self.assertEqual(shell.returncode, 0)
            finally:
                if shell.poll() is None:
                    shell.kill()
                shell.wait(timeout=3)

    def test_debug_console_runtime_tree_is_exact(self) -> None:
        binary = self.directory / "debug-console-harness"
        compilation = self.compile_debug_console_harness(binary)
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        root = self.directory / "root"
        root.mkdir()

        result = subprocess.run(
            [binary, root], check=False, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        runtime = root / "run"
        service = runtime / "systemd/system/asterinas-debug-console.service"
        target = runtime / "systemd/system/asterinas-debug-console.target"
        bashrc = runtime / "asterinas-debug-console.bashrc"
        marker = runtime / "asterinas-debug-console.enabled"
        drop_in = (
            runtime
            / "systemd/system/console-getty.service.d/asterinas-debug-console.conf"
        )
        service_link = (
            runtime
            / "systemd/system/getty.target.wants/asterinas-debug-console.service"
        )
        ready_service = runtime / "systemd/system/asterinas-desktop-ready.service"
        ready_link = (
            runtime
            / "systemd/system/getty.target.wants/"
            "asterinas-desktop-ready.service"
        )

        self.assertEqual(marker.read_bytes(), b"")
        service_text = service.read_text()
        self.assertIn("TTYPath=/dev/ttyS0\n", service_text)
        self.assertIn("StandardInput=tty-force\n", service_text)
        self.assertIn("Restart=always\n", service_text)
        self.assertIn("TimeoutStopSec=2s\n", service_text)
        self.assertIn("SendSIGKILL=yes\n", service_text)
        self.assertIn("DefaultDependencies=no\n", service_text)
        self.assertIn(
            "ConditionPathExists=/run/asterinas-debug-console.enabled\n",
            service_text,
        )
        self.assertEqual(
            bashrc.read_text(),
            "trap 'exit 0' TERM\n"
            "printf 'ASTERINAS_DEBUG_CONSOLE_READY uid=%s\\n' \"$(id -u)\"\n"
            "bind 'set enable-bracketed-paste off' 2>/dev/null\n"
            "PS1='root@asterinas-debug:\\w# '\n",
        )
        self.assertEqual(
            drop_in.read_text(),
            "[Unit]\nConditionPathExists=!/run/asterinas-debug-console.enabled\n",
        )
        self.assertTrue(service_link.is_symlink())
        self.assertEqual(
            os.readlink(service_link), "../asterinas-debug-console.service"
        )
        self.assertEqual(
            target.read_text(),
            "[Unit]\n"
            "Description=Asterinas opt-in root serial console target\n"
            "DefaultDependencies=no\n"
            "Wants=asterinas-debug-console.service\n"
            "After=asterinas-debug-console.service\n",
        )
        self.assertIn(
            "ExecStart=/run/asterinas-tools/physical-graphics-control startup-ready\n",
            ready_service.read_text(),
        )
        self.assertIn("TimeoutStartSec=285s\n", ready_service.read_text())
        self.assertTrue(ready_link.is_symlink())
        self.assertEqual(os.readlink(ready_link), "../asterinas-desktop-ready.service")
        self.assertFalse((runtime / "systemd/system/default.target").exists())
        self.assertFalse((runtime / "systemd/system.control/default.target").exists())

    def test_debug_console_uses_stage1_safe_reboot(self) -> None:
        binary = self.directory / "debug-console-harness"
        compilation = self.compile_debug_console_harness(binary)
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        root = self.directory / "root"
        root.mkdir()

        result = subprocess.run(
            [binary, root], check=False, capture_output=True, text=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        drop_in = (
            root / "run/systemd/system/asterinas-safe-reboot.service.d/10-stage1.conf"
        )
        self.assertEqual(
            drop_in.read_text(),
            "[Service]\nExecStart=\n"
            "ExecStart=/run/asterinas-tools/megrez-safe-reboot\n",
        )

    def test_isolated_debug_console_selects_runtime_default_target(self) -> None:
        binary = self.directory / "debug-console-harness"
        compilation = self.compile_debug_console_harness(binary)
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        root = self.directory / "root"
        root.mkdir()

        result = subprocess.run(
            [binary, root, "isolated"], check=False, capture_output=True, text=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        default_target = root / "run/systemd/system.control/default.target"
        self.assertTrue(default_target.is_symlink())
        self.assertEqual(
            os.readlink(default_target), "../system/asterinas-debug-console.target"
        )
        self.assertEqual(
            default_target.resolve(),
            root / "run/systemd/system/asterinas-debug-console.target",
        )
        ready_link = (
            root
            / "run/systemd/system/asterinas-debug-console.target.wants/"
            "asterinas-desktop-ready.service"
        )
        self.assertTrue(ready_link.is_symlink())
        self.assertEqual(os.readlink(ready_link), "../asterinas-desktop-ready.service")

    def test_isolated_debug_console_rejects_existing_default_target(self) -> None:
        binary = self.directory / "debug-console-harness"
        compilation = self.compile_debug_console_harness(binary)
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        root = self.directory / "root"
        systemd = root / "run/systemd/system.control"
        systemd.mkdir(parents=True)
        default_target = systemd / "default.target"
        default_target.write_text("unchanged")

        result = subprocess.run(
            [binary, root, "isolated"], check=False, capture_output=True, text=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(default_target.read_text(), "unchanged")
        self.assertFalse((root / "run/asterinas-debug-console.enabled").exists())

    def test_debug_console_rejects_symlink_destination(self) -> None:
        binary = self.directory / "debug-console-harness"
        compilation = self.compile_debug_console_harness(binary)
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        root = self.directory / "root"
        systemd = root / "run/systemd/system"
        systemd.mkdir(parents=True)
        sentinel = self.directory / "sentinel"
        sentinel.write_text("unchanged")
        service = systemd / "asterinas-debug-console.service"
        service.symlink_to(sentinel)

        result = subprocess.run(
            [binary, root], check=False, capture_output=True, text=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertTrue(service.is_symlink())
        self.assertEqual(service.resolve(), sentinel)
        self.assertFalse((root / "run/asterinas-debug-console.enabled").exists())

    def test_native_self_test_covers_discovery_and_handoff_failures(self) -> None:
        binary = self.directory / "stage1-self-test"
        compilation = self.compile_stage1(binary, "DEBIAN_STAGE1_SELF_TEST")
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        cases = (
            "one-valid-device",
            "one-valid-mmc-device",
            "no-match",
            "two-matching-devices",
            "virtio-and-mmc-ambiguous",
            "bad-ext2-magic",
            "wrong-label",
            "non-block-device",
            "delayed-valid-device",
            "device-before-deadline",
            "device-at-deadline",
            "device-after-deadline",
            "root-mount-failure",
            "dev-bind-failure",
            "proc-mount-failure",
            "sysfs-mount-failure",
            "run-mount-failure",
            "debug-console-failure",
            "tmp-mount-failure",
            "chroot-failure",
            "chdir-failure",
            "exec-failure",
            "discovery-deadline",
            "root-init-default-interactive",
            "root-init-explicit-interactive",
            "root-init-systemd",
            "root-init-systemd-debug-root",
            "root-init-systemd-debug-isolated-root",
            "root-init-probe",
            "root-init-basic",
            "root-init-probe-auto",
            "root-init-volatile-home",
            "root-init-probe-debug-conflict",
            "root-init-probe-duplicate",
            "root-init-debug-with-interactive",
            "root-init-debug-duplicate",
            "root-init-debug-unknown",
            "root-init-debug-control-character",
            "root-init-duplicate",
            "root-init-unknown",
            "root-init-control-character",
            "systemd-root-label",
            "systemd-tools-mount-policy",
            "systemd-desktop-root-label",
            "systemd-application-desktop-root-label",
            "systemd-network-desktop-root-label",
            "systemd-browser-root-label",
            "systemd-software-desktop-root-label",
            "systemd-handoff-sequence",
            "systemd-debug-handoff-sequence",
            "systemd-volatile-home-handoff",
            "systemd-exec",
        )

        for case in cases:
            with self.subTest(case=case):
                result = subprocess.run(
                    [binary, case],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout,
                    f"DEBIAN_STAGE1_SELF_TEST PASS case={case}\n",
                )

    def test_stage1_probe_agent_protocol(self) -> None:
        binary = self.directory / "stage1-probe-self-test"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DDEBIAN_STAGE1_PROBE_SELF_TEST",
                STAGE1_PROBE_SOURCE,
                "-o",
                binary,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        automatic = subprocess.run(
            [binary, "--auto"], input="", capture_output=True, text=True, timeout=2
        )
        self.assertEqual(automatic.returncode, 0, automatic.stderr)
        self.assertIn("name=boot detail=", automatic.stdout)
        self.assertIn("count=1 status=pass", automatic.stdout)
        self.assertIn("ASTERINAS_PROBE_AUTO_REBOOT", automatic.stdout)
        nonce = "00112233445566778899aabbccddeeff"
        request = (
            f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} "
            "probes=boot,syscall213,syscall272 shell=0\n"
        )

        result = subprocess.run(
            [binary],
            input=request,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        protocol = [
            line
            for line in result.stdout.splitlines()
            if line.startswith("ASTERINAS_PROBE_")
        ]
        self.assertEqual(
            protocol,
            [
                "ASTERINAS_PROBE_READY v=1 pid=1",
                f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq=0 name=boot",
                f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq=0 name=boot detail=boot-ok",
                f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq=1 name=syscall213",
                f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq=1 name=syscall213 detail=enosys",
                f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq=2 name=syscall272",
                f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq=2 name=syscall272 detail=enosys",
                f"ASTERINAS_PROBE_DONE v=1 nonce={nonce} count=3 status=pass",
                f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}",
            ],
        )

    def test_stage1_probe_reloads_uuid_on_the_same_descriptor(self) -> None:
        source = STAGE1_PROBE_SOURCE.read_text(encoding="utf-8")
        function = source.split(
            "static struct ProbeResult probe_systemd_compat(void)", 1
        )[1].split("static struct ProbeResult execute_probe", 1)[0]

        self.assertEqual(function.count('open("/proc/sys/kernel/random/uuid"'), 1)
        self.assertGreaterEqual(function.count("pread(uuid_fd"), 2)
        self.assertIn("lseek(uuid_fd, 0, SEEK_SET)", function)
        self.assertIn("memcmp(first_uuid, second_uuid", function)
        self.assertIn("memcmp(first_uuid, seek_uuid", function)

    def test_stage1_probe_disables_tty_input_echo_before_ready(self) -> None:
        binary = self.directory / "stage1-probe-tty-self-test"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DDEBIAN_STAGE1_PROBE_SELF_TEST",
                STAGE1_PROBE_SOURCE,
                "-o",
                binary,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        master, slave = os.openpty()
        process = subprocess.Popen(
            [binary],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.addCleanup(os.close, master)
        transcript = bytearray()
        ready = b"ASTERINAS_PROBE_READY v=1 pid=1"
        deadline = time.monotonic() + 2
        while ready not in transcript:
            readable, _, _ = select.select(
                [master], [], [], max(0, deadline - time.monotonic())
            )
            self.assertTrue(readable, bytes(transcript))
            transcript.extend(os.read(master, 4096))
        request = (
            b"ASTERINAS_PROBE_RUN v=1 "
            b"nonce=00112233445566778899aabbccddeeff probes=boot shell=0\n"
        )
        os.write(master, request)
        process.wait(timeout=2)
        while select.select([master], [], [], 0)[0]:
            try:
                transcript.extend(os.read(master, 4096))
            except OSError:
                break

        self.assertNotIn(request.rstrip(b"\n"), transcript)
        self.assertIn(b"ASTERINAS_PROBE_DONE", transcript)

    def test_stage1_probe_agent_rejects_noncanonical_requests(self) -> None:
        binary = self.directory / "stage1-probe-self-test"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DDEBIAN_STAGE1_PROBE_SELF_TEST",
                STAGE1_PROBE_SOURCE,
                "-o",
                binary,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        nonce = "00112233445566778899aabbccddeeff"
        invalid_requests = (
            f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} probes=boot,boot shell=0\n",
            f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} probes=unknown shell=0\n",
            f"ASTERINAS_PROBE_RUN v=1 nonce={nonce.upper()} probes=boot shell=0\n",
            f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} probes=boot shell=2\n",
            f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} probes=boot shell=0 extra\n",
            f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} probes=boot shell=0 \n",
            "x" * 513 + "\n",
        )
        for request in invalid_requests:
            with self.subTest(request=request[:80]):
                result = subprocess.run(
                    [binary],
                    input=request,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [
                        "ASTERINAS_PROBE_READY v=1 pid=1",
                        "ASTERINAS_PROBE_REQUEST_FAIL v=1 reason=invalid-request",
                    ],
                )

    def test_stage1_probe_shell_is_fixed_and_bounded_by_kernel_timer(self) -> None:
        binary = self.directory / "stage1-probe-self-test"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DDEBIAN_STAGE1_PROBE_SELF_TEST",
                STAGE1_PROBE_SOURCE,
                "-o",
                binary,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        nonce = "00112233445566778899aabbccddeeff"
        result = subprocess.run(
            [binary],
            input=(
                f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} probes=boot shell=1\n"
                "help\n"
                "uname -a\n"
                "exit\n"
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"ASTERINAS_PROBE_SHELL_READY v=1 nonce={nonce}\n", result.stdout)
        self.assertIn(
            "ASTERINAS_PROBE_SHELL_COMMANDS help,dmesg,mounts,boot,syscall213,syscall272,ext2-writeback,systemd-compat,exit\n",
            result.stdout,
        )
        self.assertIn(
            "ASTERINAS_PROBE_SHELL_REJECT reason=unknown-command\n", result.stdout
        )
        self.assertIn(
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}\n", result.stdout
        )

    def test_stage1_probe_agent_stops_after_first_failure(self) -> None:
        binary = self.directory / "stage1-probe-failure-self-test"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-DDEBIAN_STAGE1_PROBE_SELF_TEST",
                "-DDEBIAN_STAGE1_PROBE_SELF_TEST_FAIL_BOOT",
                STAGE1_PROBE_SOURCE,
                "-o",
                binary,
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        nonce = "00112233445566778899aabbccddeeff"
        result = subprocess.run(
            [binary],
            input=(
                f"ASTERINAS_PROBE_RUN v=1 nonce={nonce} "
                "probes=boot,syscall213 shell=0\n"
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"ASTERINAS_PROBE_FAIL v=1 nonce={nonce} seq=0 name=boot errno=5 detail=uname-failed\n",
            result.stdout,
        )
        self.assertNotIn("name=syscall213", result.stdout)
        self.assertIn(
            f"ASTERINAS_PROBE_DONE v=1 nonce={nonce} count=1 status=fail\n",
            result.stdout,
        )

    def test_normal_failure_lifecycle_flushes_one_marker_and_holds(self) -> None:
        binary = self.directory / "stage1-lifecycle-test"
        compilation = self.compile_stage1(binary, "DEBIAN_STAGE1_LIFECYCLE_TEST")
        self.assertEqual(compilation.returncode, 0, compilation.stderr)

        with subprocess.Popen(
            [binary],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                self.assertIsNotNone(process.stdout)
                readable, _, _ = select.select([process.stdout], [], [], 2.0)
                self.assertTrue(readable, "stage1 did not flush its failure marker")
                self.assertEqual(
                    process.stdout.readline(),
                    "DEBIAN_ROOTFS_FAIL reason=test-lifecycle\n",
                )
                readable, _, _ = select.select([process.stdout], [], [], 0.1)
                self.assertFalse(readable, "stage1 printed more than one marker")
                self.assertIsNone(
                    process.poll(), "stage1 exited after terminal failure"
                )
            finally:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=2)

    def test_console_duplication_clears_cloexec_for_same_number_fd(self) -> None:
        harness_source = self.directory / "console-fd-harness.c"
        harness_source.write_text(
            f'''#define main stage1_production_main
#include "{STAGE1_SOURCE}"
#undef main

int main(int argc, char **argv)
{{
    if (argc != 2 || argv[1][0] < '0' || argv[1][0] > '2' || argv[1][1] != '\\0')
        return 2;
    int target_fd = argv[1][0] - '0';
    if (close(target_fd) != 0)
        return 3;
    int console_fd = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (console_fd != target_fd)
        return 4;
    if (duplicate_console_fd(console_fd, target_fd) < 0)
        return 5;
    int flags = fcntl(target_fd, F_GETFD);
    if (flags < 0)
        return 6;
    return (flags & FD_CLOEXEC) != 0;
}}
''',
            encoding="utf-8",
        )
        binary = self.directory / "console-fd-harness"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-return-type",
                harness_source,
                STAGE1_DEBUG_CONSOLE_SOURCE,
                STAGE1_PROBE_SOURCE,
                "-o",
                binary,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)

        for target_fd in range(3):
            with self.subTest(target_fd=target_fd):
                result = subprocess.run([binary, str(target_fd)], check=False)
                self.assertEqual(result.returncode, 0)

    def test_stage1_progress_markers_are_flushed_and_machine_readable(self) -> None:
        harness_source = self.directory / "stage1-progress-harness.c"
        harness_source.write_text(
            f'''#define main stage1_production_main
#include "{STAGE1_SOURCE}"
#undef main

int main(void)
{{
    report_progress("start", "mode", "systemd");
    report_progress("probe-block", "device", "/dev/mmcblk0p2");
    report_progress("handoff-enter", "action", "root-mount");
    return 0;
}}
''',
            encoding="utf-8",
        )
        binary = self.directory / "stage1-progress-harness"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-return-type",
                harness_source,
                STAGE1_DEBUG_CONSOLE_SOURCE,
                STAGE1_PROBE_SOURCE,
                "-o",
                binary,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)

        result = subprocess.run(
            [binary], check=False, capture_output=True, text=True, timeout=2
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "DEBIAN_STAGE1_PROGRESS step=start mode=systemd\n"
            "DEBIAN_STAGE1_PROGRESS step=probe-block device=/dev/mmcblk0p2\n"
            "DEBIAN_STAGE1_PROGRESS step=handoff-enter action=root-mount\n",
        )

    def test_builder_declares_exact_tools_and_entries(self) -> None:
        tools = self.run_builder("--print-tools")
        entries = self.run_builder("--print-entries")

        self.assertEqual(tools.returncode, 0, tools.stderr)
        self.assertEqual(
            tools.stdout.splitlines(),
            ["riscv64-linux-gnu-gcc", "cpio", "python3"],
        )

        self.assertEqual(entries.returncode, 0, entries.stderr)
        self.assertEqual(
            entries.stdout.splitlines(),
            [
                ".",
                "init",
                "usr",
                "usr/lib",
                "usr/lib/asterinas",
                "usr/lib/asterinas/browser_interaction_perf.py",
                "usr/lib/asterinas/browser_system_time.py",
                "usr/lib/asterinas/browser_latency_contract.py",
                "usr/lib/asterinas/browser_daily_use_contract.py",
                "usr/lib/asterinas/browser_perf_capture.py",
                "usr/lib/asterinas/browser_workload_contract.py",
                "usr/lib/asterinas/browser_composite_capture.py",
                "usr/lib/asterinas/browser-web-marionette-gate",
                "usr/lib/asterinas/browser_web_marionette_gate.py",
                "usr/lib/asterinas/browser_marionette_transport.py",
                "usr/lib/asterinas/browser-daily-use-gate",
                "usr/lib/asterinas/browser-daily-use-upload",
                "usr/lib/asterinas/megrez-clock-sync",
                "usr/lib/asterinas/physical-external-services-quiesce",
                "usr/lib/asterinas/desktop-input-identity",
                "usr/lib/asterinas/physical-graphics-control",
                "usr/lib/asterinas/megrez-safe-reboot",
                "usr/lib/asterinas/physical-graphics-gate",
                "usr/lib/asterinas/physical-graphics-interaction.html",
                "usr/lib/asterinas/physical-system-probe",
                "usr/lib/asterinas/g",
                "usr/lib/asterinas/q",
                "usr/lib/asterinas/s",
            ],
        )

    def test_stage1_carries_safe_reboot_override(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        output = self.directory / "safe-reboot" / "initramfs.cpio"

        result = self.run_builder(str(output), environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        entries = {entry[0]: entry for entry in _parse_newc_entries(output.read_bytes())}
        helper = entries["usr/lib/asterinas/megrez-safe-reboot"]
        self.assertEqual(stat.S_IMODE(helper[1]), 0o755)
        self.assertEqual(helper[5], STAGE1_SAFE_REBOOT_SOURCE.read_bytes())

    def test_stage1_carries_nonblocking_desktop_input_identity(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        output = self.directory / "input-identity" / "initramfs.cpio"

        result = self.run_builder(str(output), environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        entries = {entry[0]: entry for entry in _parse_newc_entries(output.read_bytes())}
        helper = entries["usr/lib/asterinas/desktop-input-identity"]
        self.assertEqual(stat.S_IMODE(helper[1]), 0o755)
        self.assertEqual(helper[5], STAGE1_DESKTOP_INPUT_IDENTITY.read_bytes())
        self.assertIn(b"os.O_NONBLOCK", helper[5])

    def test_stage1_includes_daily_use_uploader(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        output = self.directory / "daily-use-upload" / "initramfs.cpio"

        result = self.run_builder(str(output), environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        entries = {entry[0]: entry for entry in _parse_newc_entries(output.read_bytes())}
        self.assertTrue(
            "usr/lib/asterinas/browser-daily-use-upload" in entries,
            "daily-use uploader missing from Stage1 archive",
        )
        uploader = entries["usr/lib/asterinas/browser-daily-use-upload"]
        self.assertEqual(stat.S_IMODE(uploader[1]), 0o755)
        self.assertEqual(uploader[5], STAGE1_BROWSER_DAILY_USE_UPLOAD.read_bytes())

    def test_stage1_carries_importable_marionette_module_for_daily_use(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        output = self.directory / "daily-use-import" / "initramfs.cpio"

        result = self.run_builder(str(output), environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        entries = {entry[0]: entry for entry in _parse_newc_entries(output.read_bytes())}
        module = entries.get("usr/lib/asterinas/browser_web_marionette_gate.py")
        self.assertIsNotNone(
            module,
            "daily-use gate cannot import browser_web_marionette_gate from Stage1",
        )
        assert module is not None
        self.assertEqual(stat.S_IMODE(module[1]), 0o644)
        self.assertEqual(module[5], STAGE1_BROWSER_GATE.read_bytes())

    def test_physical_graphics_control_uses_stage1_input_identity(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()

        self.assertIn(
            'runpy.run_path("/run/asterinas-tools/desktop-input-identity")',
            source,
        )
        self.assertNotIn(
            'runpy.run_path("/usr/lib/asterinas/desktop-input-identity")',
            source,
        )

    def test_physical_graphics_control_starts_the_resolver_shim(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()
        browser = source[source.index("start_browser() {") :]
        browser = browser[: browser.index("\nboot_phase() {")]

        # The isolated debug console replaces systemd's default.target with a
        # target that wants only the console service, so nothing under
        # basic.target.wants starts on its own and the shim has to be started
        # here -- with the ignore-dependencies mode the browser units need.
        self.assertIn(
            "start --no-block --job-mode=ignore-dependencies \\\n"
            "            asterinas-dns-shim.service",
            browser,
        )
        # And before the browser starts, so the tunnel can publish a resolver
        # before anything tries to resolve a name.
        self.assertLess(
            browser.index("asterinas-dns-shim.service"),
            browser.index("browser_stage browser-start"),
        )
        # A shim that will not start costs the guest its resolver but not the
        # desktop, so it is reported instead of failing the boot.
        self.assertIn(
            "boot_phase 'ASTERINAS_DESKTOP_DNS_SHIM_START status=1'",
            browser,
        )

    def test_physical_browser_resets_persistent_timeline_before_firefox(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()
        browser = source[source.index("start_browser() {") :]
        browser = browser[: browser.index("\nboot_phase() {")]

        self.assertIn("browser-web-timeline reset-physical", browser)
        self.assertIn("runuser --user asterinas", browser)
        self.assertLess(
            browser.index("browser-web-timeline reset-physical"),
            browser.index("browser_stage browser-start"),
        )
    def test_physical_graphics_control_keeps_browser_optional(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text(encoding="utf-8")
        browser = source[source.index("browser_stage browser-start"):]
        self.assertIn("ASTERINAS_DESKTOP_BROWSER_OPTIONAL start=failed", browser)
        self.assertIn(
            "__ASTERINAS_PHYSICAL_BROWSER_START__ status=%s\\n' \"$status\"",
            browser,
        )
        readiness = source[
            source.index("startup_ready_once() {"):source.index("startup_ready() {")
        ]
        self.assertIn("readiness_reason=openbox", readiness)
        self.assertNotIn("readiness_reason=firefox", readiness)
        self.assertIn("ASTERINAS_DESKTOP_FIREFOX_READY pid=$pid user=1000 optional=1", source)

    def test_physical_graphics_control_disarms_only_after_desktop_readiness(
        self,
    ) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()

        self.assertIn(
            'startup-ready) [ "$#" -eq 0 ] || die_usage; startup_ready ;;',
            source,
        )
        self.assertIn("/proc/sys/kernel/asterinas_reboot_watchdog", source)
        self.assertIn("ASTERINAS_DESKTOP_DISPLAY_READY", source)
        self.assertIn("ASTERINAS_DESKTOP_FIREFOX_READY", source)
        self.assertIn("ASTERINAS_DESKTOP_WATCHDOG_DISARMED", source)
        self.assertIn("ASTERINAS_DESKTOP_WATCHDOG_ABSENT", source)
        self.assertIn("ASTERINAS_DESKTOP_BOOT_READY", source)

    def test_desktop_readiness_hands_off_safe_reboot_after_kernel_disarm(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()
        startup = source[source.index("startup_ready() {") :]
        startup = startup[: startup.index("\nstartup_snapshot() {")]

        disarm = startup.index("printf '0\\n' >\"$watchdog\"")
        readback = startup.index('"$(cat "$watchdog" 2>/dev/null || true)" = 0')
        handoff = startup.index("/run/asterinas-desktop-ready")
        complete = startup.index("ASTERINAS_DESKTOP_BOOT_READY")
        self.assertLess(disarm, readback)
        self.assertLess(readback, handoff)
        self.assertLess(handoff, complete)
        self.assertIn("/proc/sys/kernel/random/boot_id", startup)

    def test_physical_graphics_control_has_offline_local_firefox_mode(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()
        self.assertIn("ASTERINAS_DESKTOP_OFFLINE", source)
        self.assertIn("input-disabled", source)

    def test_offline_start_publishes_the_no_input_display_config(self) -> None:
        source_root = self.directory / "display-source"
        config_directory = source_root / "fbdev/xorg.conf.d"
        config_directory.mkdir(parents=True)
        config = (REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_fbdev_no_input.conf").read_text()
        (config_directory / "20-asterinas-no-input.conf").write_text(config)
        runtime_root = self.directory / "display-runtime"
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()
        start = source[source.index("start_browser() {") : source.index("\nboot_phase() {")]
        start = start.replace("/etc/asterinas/display-providers", str(source_root))
        start = start.replace("/run/asterinas-physical-display-providers", str(runtime_root))
        result = subprocess.run(
            ["/bin/sh", "-c", "offline_desktop=1\n"
             "systemctl_bounded() { return 0; }\n"
             "browser_stage() { :; }\n"
             "runuser() { return 0; }\n" + start + "\nstart_browser 0\n"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        published = runtime_root / "fbdev/xorg.conf.d/20-asterinas.conf"
        self.assertTrue(published.is_file(), "offline display provider has no configuration")
        self.assertEqual(published.read_text(), config)
        environment = os.environ.copy()
        environment["ASTERINAS_DISPLAY_PROVIDER_ROOT"] = str(runtime_root)
        environment["ASTERINAS_DISPLAY_PROVIDER"] = "fbdev"
        provider = subprocess.run(
            ["/bin/bash", REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_display_provider.sh"],
            env=environment, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(provider.returncode, 0, provider.stderr)
        self.assertEqual(provider.stdout.strip(), str(published.parent))

    def test_browser_device_access_allows_offline_framebuffer_without_usb(self) -> None:
        source = (REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m3_device_access.sh").read_text()
        self.assertIn("ASTERINAS_DESKTOP_OFFLINE", source)
        self.assertIn("desktop-input-disabled", source)

    def test_builder_installs_a_no_input_framebuffer_xorg_profile(self) -> None:
        builder = BUILD_SCRIPT.read_text()
        self.assertIn("20-asterinas-no-input.conf", builder)
        self.assertIn('"$script_directory/desktop_fbdev_no_input.conf"', builder)

    def test_startup_readiness_uses_one_monotonic_deadline(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()
        startup = source[source.index("startup_ready() {") :]
        startup = startup[: startup.index("\nbrowser_identity() {")]

        self.assertIn("/proc/uptime", source)
        self.assertIn("ASTERINAS_DESKTOP_BOOT_WAIT", startup)
        self.assertNotIn('if [ "$attempt" -ge 240 ]', startup)
        self.assertIn("readiness_deadline", startup)
        self.assertIn("readiness_deadline=$((readiness_started + 220))", startup)
        self.assertIn("printf '<6>%s\\n' \"$1\" >/dev/kmsg", source)
        self.assertNotIn("printf '%s\\n' \"$1\"\n", source)
        self.assertNotIn("printf '%s\\n' \"$1\" >/dev/console", source)

    def test_physical_graphics_control_has_bounded_startup_snapshot(self) -> None:
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text()
        snapshot = source[source.index("startup_snapshot() {") :]
        snapshot = snapshot[: snapshot.index("\nbrowser_identity() {")]

        self.assertIn(
            'startup-snapshot) [ "$#" -eq 0 ] || die_usage; startup_snapshot ;;',
            source,
        )
        self.assertIn("/proc/$pid/status", snapshot)
        self.assertIn("/proc/$pid/stat", snapshot)
        self.assertIn("/proc/$pid/io", snapshot)
        self.assertIn("/proc/$pid/task/", snapshot)
        self.assertIn("voluntary_ctxt_switches", snapshot)
        self.assertIn("/proc/loadavg", snapshot)
        self.assertIn("procs_running", snapshot)
        self.assertIn("/proc/sys/kernel/asterinas_reboot_watchdog", snapshot)
        self.assertGreaterEqual(snapshot.count("/usr/bin/timeout"), 2)
        self.assertIn("__ASTERINAS_STARTUP_SNAPSHOT__", snapshot)

    def test_stage1_exposes_ephemeral_tools_from_run_without_rootfs_writes(
        self,
    ) -> None:
        source = STAGE1_SOURCE.read_text()
        normalized = " ".join(source.split())
        self.assertIn('ensure_directory("/newroot/run/asterinas-tools")', source)
        self.assertIn(
            'mount("/usr/lib/asterinas", "/newroot/run/asterinas-tools", '
            "NULL, MS_BIND, NULL)",
            normalized,
        )
        mount_run = source[source.index("case HANDOFF_MOUNT_RUN:") :]
        mount_run = mount_run[: mount_run.index("case HANDOFF_PREPARE_DEBUG_CONSOLE:")]
        self.assertNotIn("O_WRONLY", mount_run)
        self.assertNotIn("/newroot/usr", mount_run)

    def test_stage1_ext4_handoff_allows_journal_replay(self) -> None:
        source = STAGE1_SOURCE.read_text()
        normalized = " ".join(source.split())
        self.assertIn(
            'mount(root_device, "/newroot", "ext4", 0, NULL)',
            normalized,
        )
        self.assertNotIn(
            'mount(root_device, "/newroot", "ext4", 0, "noload")',
            normalized,
        )

    def test_physical_gate_imports_its_stage1_carried_dependencies(self) -> None:
        stage_tools = self.directory / "stage-tools"
        stage_tools.mkdir()
        for source in (
            STAGE1_PHYSICAL_GRAPHICS_GATE,
            STAGE1_BROWSER_INTERACTION_PERF,
            STAGE1_BROWSER_MARIONETTE_TRANSPORT,
        ):
            (stage_tools / source.name).write_bytes(source.read_bytes())
        environment = os.environ.copy()
        environment["PYTHONPATH"] = ""

        result = subprocess.run(
            [sys.executable, stage_tools / "physical_graphics_gate.py", "--help"],
            cwd=stage_tools,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_builder_rejects_unknown_arguments(self) -> None:
        result = self.run_builder("--unknown")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option", result.stderr)

    def test_physical_graphics_control_rejects_untrusted_arguments(self) -> None:
        for arguments in (
            (),
            ("unknown",),
            ("preflight", "extra"),
            ("cycle", "1", "not-a-nonce", "180", "540", "41", "1920", "1080"),
            ("final", "2", "0123456789abcdef", "180", "540", "41"),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["/bin/sh", STAGE1_PHYSICAL_GRAPHICS_CONTROL, *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def _run_daily_use_control(
        self,
        *,
        arguments: tuple[str, ...] = (
            "daily-use",
            "0123456789abcdef0123456789abcdef",
            "120",
            "4242",
        ),
        firefox_pid: str = "4242",
        xorg_pids: str = "4343",
        manager_environment: str = (
            "ASTERINAS_PHYSICAL_DAILY_USE=1\n"
            "ASTERINAS_DESKTOP_FIXTURE_URL="
            "http://10.100.19.216:17894/asterinas-network-probe.bin\n"
        ),
        gate_status: int = 0,
        existing_evidence: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        fake_bin = self.directory / f"daily-use-bin-{len(list(self.directory.iterdir()))}"
        fake_bin.mkdir()
        log = fake_bin / "calls.log"
        control = fake_bin / "physical-graphics-control"
        source = STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_text(encoding="utf-8")
        source = source.replace(
            "systemctl_bounded() {\n"
            '    /usr/bin/timeout --kill-after=1s 10s /usr/bin/systemctl "$@"\n'
            "}",
            "systemctl_bounded() { systemctl \"$@\"; }",
        )
        if existing_evidence:
            evidence = fake_bin / "existing-evidence"
            evidence.mkdir()
            source = source.replace(
                '[ -e "$evidence_dir" ] || [ -L "$evidence_dir" ]',
                f'[ -e "{evidence}" ] || [ -L "{evidence}" ]',
            )
        control.write_text(source, encoding="utf-8")
        control.chmod(0o755)
        (fake_bin / "systemctl").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  'show --property MainPID --value asterinas-browser-web.service') "
            'printf "%s\\n" "$ASTERINAS_TEST_FIREFOX_PID" ;;\n'
            "  'show --property NRestarts --value asterinas-browser-web.service') "
            "printf '0\\n' ;;\n"
            "  'show-environment') printf '%s' \"$ASTERINAS_TEST_MANAGER_ENV\" ;;\n"
            "  *) exit 97 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fake_bin / "pgrep").write_text(
            "#!/bin/sh\nprintf '%s' \"$ASTERINAS_TEST_XORG_PIDS\"\n",
            encoding="utf-8",
        )
        (fake_bin / "nsenter").write_text(
            "#!/bin/sh\n"
            "printf 'nsenter %s\\n' \"$*\" >>\"$ASTERINAS_TEST_LOG\"\n"
            "case \"$*\" in\n"
            "  *browser-daily-use-gate*) exit \"$ASTERINAS_TEST_GATE_STATUS\" ;;\n"
            "  *browser-daily-use-upload*) exit 0 ;;\n"
            "  *) exit 98 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        for name in ("systemctl", "pgrep", "nsenter"):
            (fake_bin / name).chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "ASTERINAS_TEST_FIREFOX_PID": firefox_pid,
                "ASTERINAS_TEST_XORG_PIDS": xorg_pids,
                "ASTERINAS_TEST_MANAGER_ENV": manager_environment,
                "ASTERINAS_TEST_GATE_STATUS": str(gate_status),
                "ASTERINAS_TEST_LOG": str(log),
            }
        )
        result = subprocess.run(
            ["/bin/sh", control, *arguments],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        return result, log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def test_physical_graphics_control_accepts_closed_daily_use_action(self) -> None:
        result, calls = self._run_daily_use_control()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                "nsenter -t 4242 -m -n /run/asterinas-tools/browser-daily-use-gate "
                "--firefox-pid 4242 --xorg-pid 4343 --fixture-index-url "
                "http://10.100.19.216:17894/browser-quality/index.html "
                "--evidence-dir /run/asterinas-browser-daily-use-"
                "0123456789abcdef0123456789abcdef --mode profile --physical "
                "--timeout-seconds 120",
                "nsenter -t 4242 -m -n /run/asterinas-tools/browser-daily-use-upload "
                "/run/asterinas-browser-daily-use-0123456789abcdef0123456789abcdef "
                "0123456789abcdef0123456789abcdef pass "
                "http://10.100.19.216:17894/browser-quality/daily-use-evidence/"
                "0123456789abcdef0123456789abcdef --timeout 15",
            ],
        )
        self.assertEqual(
            result.stdout,
            "__ASTERINAS_PHYSICAL_DAILY_USE__ "
            "experiment_id=0123456789abcdef0123456789abcdef outcome=pass "
            "gate_status=0 upload_status=0\n",
        )

    def test_physical_graphics_control_rejects_untrusted_daily_use_arguments(
        self,
    ) -> None:
        valid_id = "0123456789abcdef0123456789abcdef"
        for arguments in (
            ("daily-use",),
            ("daily-use", "0123", "120", "4242"),
            ("daily-use", valid_id.upper(), "120", "4242"),
            ("daily-use", "g" * 32, "120", "4242"),
            ("daily-use", valid_id, "0", "4242"),
            ("daily-use", valid_id, "120.1", "4242"),
            ("daily-use", valid_id, "121", "4242"),
            ("daily-use", valid_id, "120", "1"),
            ("daily-use", valid_id, "120", "4242", "extra"),
        ):
            with self.subTest(arguments=arguments):
                result, calls = self._run_daily_use_control(arguments=arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual((result.stdout, result.stderr, calls), ("", "", []))

        prerequisite_cases = (
            ({"firefox_pid": "4243"}, 124),
            ({"existing_evidence": True}, 123),
            ({"xorg_pids": ""}, 122),
            ({"xorg_pids": "4343\n4344\n"}, 122),
            ({"manager_environment": ""}, 121),
            (
                {
                    "manager_environment": (
                        "ASTERINAS_PHYSICAL_DAILY_USE=1\n"
                        "ASTERINAS_DESKTOP_FIXTURE_URL=http://example.invalid/file\n"
                    )
                },
                121,
            ),
        )
        for overrides, expected_status in prerequisite_cases:
            with self.subTest(overrides=overrides):
                result, calls = self._run_daily_use_control(**overrides)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"gate_status={expected_status}", result.stdout)
                self.assertEqual(calls, [])

    def test_physical_graphics_control_preserves_failed_daily_use_outcome(
        self,
    ) -> None:
        result, calls = self._run_daily_use_control(gate_status=17)

        self.assertEqual(result.returncode, 1)
        self.assertIn(" outcome=fail gate_status=17 upload_status=0\n", result.stdout)
        self.assertIn(" fail http://10.100.19.216:17894/", calls[1])

    def _run_physical_quiesce(self, manager_environment: str) -> str:
        root = self.directory / f"quiesce-{len(list(self.directory.iterdir()))}"
        control = root / "system.control"
        home = root / "home"
        fake_bin = root / "bin"
        fake_bin.mkdir(parents=True)
        script = root / "quiesce.sh"
        source = STAGE1_PHYSICAL_EXTERNAL_SERVICES_QUIESCE.read_text(encoding="utf-8")
        source = source.replace(
            "readonly _asterinas_control=/run/systemd/system.control",
            f"readonly _asterinas_control={control}",
        ).replace(
            "readonly _asterinas_home=/run/asterinas-physical-home",
            f"readonly _asterinas_home={home}",
        ).replace(
            "systemctl_bounded() {\n"
            '    /usr/bin/timeout --kill-after=1s 5s /usr/bin/systemctl "$@"\n'
            "}",
            "systemctl_bounded() { systemctl \"$@\"; }",
        ).replace("/usr/bin/mountpoint", "/bin/true").replace(
            "/usr/bin/mount --bind", "/bin/true"
        ).replace("/usr/bin/sleep 1", "/bin/true")
        script.write_text(source, encoding="utf-8")
        (fake_bin / "systemctl").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  show-environment) printf '%s' \"$ASTERINAS_TEST_MANAGER_ENV\" ;;\n"
            "  'is-active '* ) printf 'inactive\\n'; exit 3 ;;\n"
            "  'show --property MainPID --value '* ) printf '0\\n' ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fake_bin / "systemctl").chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "ASTERINAS_TEST_MANAGER_ENV": manager_environment,
            }
        )
        result = subprocess.run(
            ["/bin/bash", script],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return (control / "asterinas-browser-web.service.d/physical.conf").read_text(
            encoding="utf-8"
        )

    def test_daily_use_quiesce_allows_only_fixture_host(self) -> None:
        daily_use = self._run_physical_quiesce(
            "ASTERINAS_PHYSICAL_DAILY_USE=1\n"
            "ASTERINAS_DESKTOP_FIXTURE_URL="
            "http://10.100.19.216:17894/asterinas-network-probe.bin\n"
        )
        ordinary = self._run_physical_quiesce("")

        self.assertIn("Environment=ASTERINAS_WEB_NETWORK_MODE=proxy\n", daily_use)
        self.assertIn("Environment=ASTERINAS_DESKTOP_PROXY_HOST=10.100.19.216\n", daily_use)
        self.assertIn("Environment=ASTERINAS_DESKTOP_PROXY_PORT=9\n", daily_use)
        self.assertIn("Environment=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1\n", ordinary)
        self.assertIn("Environment=ASTERINAS_DESKTOP_PROXY_PORT=9\n", ordinary)

    def test_physical_graphics_control_accepts_a_six_digit_linux_pid(self) -> None:
        result = subprocess.run(
            [
                "/bin/sh",
                STAGE1_PHYSICAL_GRAPHICS_CONTROL,
                "final",
                "3",
                "0123",
                "180",
                "540",
                "123456",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertRegex(
            result.stdout, r"^__ASTERINAS_PHYSICAL_FINAL_STATUS__ status=12[46]\n$"
        )
        self.assertEqual(result.stderr, "")

    def test_builder_is_deterministic_with_exact_raw_newc_metadata(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        environment["SOURCE_DATE_EPOCH"] = "1700000000"
        first = self.directory / "first output" / "initramfs.cpio"
        second = self.directory / "second output" / "initramfs.cpio"

        for output in (first, second):
            result = self.run_builder(
                str(output),
                environment=environment,
                cwd=self.directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            time.sleep(1.1)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((first.parent / "init").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((second.parent / "init").stat().st_mode), 0o755)
        entries = _parse_newc_entries(first.read_bytes())
        self.assertEqual(
            [
                (name, mode, uid, gid, mtime)
                for name, mode, uid, gid, mtime, _ in entries
            ],
            [
                (".", stat.S_IFDIR | 0o755, 0, 0, 1700000000),
                ("init", stat.S_IFREG | 0o755, 0, 0, 1700000000),
                ("usr", stat.S_IFDIR | 0o755, 0, 0, 1700000000),
                ("usr/lib", stat.S_IFDIR | 0o755, 0, 0, 1700000000),
                ("usr/lib/asterinas", stat.S_IFDIR | 0o755, 0, 0, 1700000000),
                (
                    "usr/lib/asterinas/browser_interaction_perf.py",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_system_time.py",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_latency_contract.py",
                    stat.S_IFREG | 0o644,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_daily_use_contract.py",
                    stat.S_IFREG | 0o644,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_perf_capture.py",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_workload_contract.py",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_composite_capture.py",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser-web-marionette-gate",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_web_marionette_gate.py",
                    stat.S_IFREG | 0o644,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser_marionette_transport.py",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser-daily-use-gate",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/browser-daily-use-upload",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/megrez-clock-sync",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/physical-external-services-quiesce",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/desktop-input-identity",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/physical-graphics-control",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/megrez-safe-reboot",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/physical-graphics-gate",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/physical-graphics-interaction.html",
                    stat.S_IFREG | 0o644,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/physical-system-probe",
                    stat.S_IFREG | 0o755,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/g",
                    stat.S_IFLNK | 0o777,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/q",
                    stat.S_IFLNK | 0o777,
                    0,
                    0,
                    1700000000,
                ),
                (
                    "usr/lib/asterinas/s",
                    stat.S_IFLNK | 0o777,
                    0,
                    0,
                    1700000000,
                ),
            ],
        )
        self.assertTrue(entries[1][5].startswith(b"\x7fELF"))
        self.assertEqual(entries[1][5], (first.parent / "init").read_bytes())
        self.assertEqual(entries[5][5], STAGE1_BROWSER_INTERACTION_PERF.read_bytes())
        self.assertEqual(entries[6][5], STAGE1_BROWSER_SYSTEM_TIME.read_bytes())
        self.assertEqual(entries[7][5], STAGE1_BROWSER_LATENCY_CONTRACT.read_bytes())
        self.assertEqual(entries[8][5], STAGE1_BROWSER_DAILY_USE_CONTRACT.read_bytes())
        self.assertEqual(entries[9][5], STAGE1_BROWSER_PERF_CAPTURE.read_bytes())
        self.assertEqual(entries[10][5], STAGE1_BROWSER_WORKLOAD_CONTRACT.read_bytes())
        self.assertEqual(entries[11][5], STAGE1_BROWSER_COMPOSITE_CAPTURE.read_bytes())
        self.assertEqual(entries[12][5], STAGE1_BROWSER_GATE.read_bytes())
        self.assertEqual(entries[13][5], STAGE1_BROWSER_GATE.read_bytes())
        self.assertEqual(entries[14][5], STAGE1_BROWSER_MARIONETTE_TRANSPORT.read_bytes())
        self.assertEqual(entries[15][5], STAGE1_BROWSER_DAILY_USE_GATE.read_bytes())
        self.assertEqual(entries[16][5], STAGE1_BROWSER_DAILY_USE_UPLOAD.read_bytes())
        self.assertEqual(entries[17][5], STAGE1_CLOCK_SYNC.read_bytes())
        self.assertEqual(
            entries[18][5], STAGE1_PHYSICAL_EXTERNAL_SERVICES_QUIESCE.read_bytes()
        )
        self.assertEqual(entries[19][5], STAGE1_DESKTOP_INPUT_IDENTITY.read_bytes())
        self.assertEqual(entries[20][5], STAGE1_PHYSICAL_GRAPHICS_CONTROL.read_bytes())
        self.assertEqual(entries[21][5], STAGE1_SAFE_REBOOT_SOURCE.read_bytes())
        self.assertEqual(entries[22][5], STAGE1_PHYSICAL_GRAPHICS_GATE.read_bytes())
        self.assertEqual(entries[23][5], STAGE1_PHYSICAL_GRAPHICS_PAGE.read_bytes())
        self.assertEqual(entries[24][5], STAGE1_PHYSICAL_SYSTEM_PROBE.read_bytes())
        self.assertEqual(entries[25][5], b"physical-graphics-control")
        self.assertEqual(entries[26][5], b"physical-external-services-quiesce")
        self.assertEqual(entries[27][5], b"physical-system-probe")

    def test_builder_rejects_invalid_source_date_epoch(self) -> None:
        for value in ("", "00", "01", "+1", "-1", "1.0", "4294967296"):
            with self.subTest(value=value):
                environment = os.environ.copy()
                environment["RISC_V_CC"] = "cc"
                environment["SOURCE_DATE_EPOCH"] = value
                output = self.directory / f"invalid-{value.replace('/', '_')}.cpio"

                result = self.run_builder(str(output), environment=environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("SOURCE_DATE_EPOCH", result.stderr)
                self.assertFalse(output.exists())

    def test_builder_rejects_directory_destination_without_mutation(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        destination = self.directory / "archive.cpio"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_bytes(b"preserve directory")

        result = self.run_builder(str(destination), environment=environment)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unsafe output path", result.stderr)
        self.assertEqual(list(destination.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"preserve directory")

    def test_builder_failure_preserves_existing_archive(self) -> None:
        failing_compiler = self.directory / "failing-compiler"
        failing_compiler.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        failing_compiler.chmod(0o755)
        destination = self.directory / "existing archive.cpio"
        destination.write_bytes(b"known-good-archive")
        destination.chmod(0o640)
        existing_init = destination.parent / "init"
        existing_init.write_bytes(b"known-good-init")
        existing_init.chmod(0o750)
        environment = os.environ.copy()
        environment["RISC_V_CC"] = str(failing_compiler)

        result = self.run_builder(str(destination), environment=environment)

        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertEqual(destination.read_bytes(), b"known-good-archive")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o640)
        self.assertEqual(existing_init.read_bytes(), b"known-good-init")
        self.assertEqual(stat.S_IMODE(existing_init.stat().st_mode), 0o750)

    def test_builder_publication_failure_preserves_existing_pair(self) -> None:
        fake_bin = self.directory / "publication-failure-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 96\n", encoding="utf-8")
        fake_python.chmod(0o755)
        output = self.directory / "publication failure" / "initramfs.cpio"
        output.parent.mkdir()
        output.write_bytes(b"old-archive")
        output.chmod(0o640)
        init_output = output.parent / "init"
        init_output.write_bytes(b"old-init")
        init_output.chmod(0o750)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        environment["RISC_V_CC"] = "cc"

        result = self.run_builder(str(output), environment=environment)

        self.assertEqual(result.returncode, 96, result.stderr)
        self.assertEqual(output.read_bytes(), b"old-archive")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)
        self.assertEqual(init_output.read_bytes(), b"old-init")
        self.assertEqual(stat.S_IMODE(init_output.stat().st_mode), 0o750)

    def test_stage1_publication_rolls_back_second_replace_failure(self) -> None:
        init_source, archive_source = self.make_stage1_publication_sources()
        real_replace = os.replace

        for had_existing_pair in (False, True):
            with self.subTest(had_existing_pair=had_existing_pair):
                output = self.directory / f"replace-failure-{had_existing_pair}"
                output.mkdir(mode=0o710)
                if had_existing_pair:
                    (output / "init").write_bytes(b"old-init")
                    (output / "init").chmod(0o751)
                    (output / "initramfs.cpio").write_bytes(b"old-archive")
                    (output / "initramfs.cpio").chmod(0o640)
                original = self.stage1_publication_snapshot(output)
                replace_count = 0

                def fail_second_replace(*args, **kwargs):
                    nonlocal replace_count
                    replace_count += 1
                    if replace_count == 2:
                        raise OSError("injected stage1 second replace failure")
                    return real_replace(*args, **kwargs)

                with (
                    mock.patch.object(
                        fsops_module.os,
                        "replace",
                        new=fail_second_replace,
                    ),
                    self.assertRaisesRegex(OSError, "injected stage1 second replace"),
                ):
                    fsops_module.publish_stage1(
                        output,
                        init_source,
                        archive_source,
                    )

                self.assertEqual(self.stage1_publication_snapshot(output), original)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o710)
                self.assertEqual(
                    sorted(path.name for path in output.iterdir()),
                    sorted(
                        name for name, value in original.items() if value is not None
                    ),
                )

    def test_stage1_publication_rolls_back_hup_and_term(self) -> None:
        init_source, archive_source = self.make_stage1_publication_sources()

        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                output = self.directory / f"signal-{signum}"
                output.mkdir(mode=0o711)
                (output / "init").write_bytes(b"old-init")
                (output / "init").chmod(0o750)
                (output / "initramfs.cpio").write_bytes(b"old-archive")
                (output / "initramfs.cpio").chmod(0o640)
                original = self.stage1_publication_snapshot(output)
                process_id = os.fork()
                if process_id == 0:
                    real_replace = os.replace
                    replace_count = 0

                    def signal_second_replace(*args, **kwargs):
                        nonlocal replace_count
                        replace_count += 1
                        if replace_count == 2:
                            os.kill(os.getpid(), signum)
                        return real_replace(*args, **kwargs)

                    try:
                        with mock.patch.object(
                            fsops_module.os,
                            "replace",
                            new=signal_second_replace,
                        ):
                            fsops_module.publish_stage1(
                                output,
                                init_source,
                                archive_source,
                            )
                    except fsops_module.PublishInterrupted as error:
                        os._exit(128 + error.signum)
                    except BaseException:
                        os._exit(99)
                    os._exit(0)

                wait_status = self.wait_for_child(process_id)
                self.assertTrue(os.WIFEXITED(wait_status))
                self.assertEqual(os.WEXITSTATUS(wait_status), 128 + signum)
                self.assertEqual(self.stage1_publication_snapshot(output), original)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o711)
                self.assertEqual(
                    sorted(path.name for path in output.iterdir()),
                    ["init", "initramfs.cpio"],
                )

    def test_stage1_publication_defers_post_commit_signal(self) -> None:
        init_source, archive_source = self.make_stage1_publication_sources()

        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                output = self.directory / f"post-commit-{signum}"
                output.mkdir(mode=0o711)
                (output / "init").write_bytes(b"old-init")
                (output / "initramfs.cpio").write_bytes(b"old-archive")
                real_cleanup = fsops_module._cleanup_publication_files

                def signal_during_cleanup(entries):
                    os.kill(os.getpid(), signum)
                    real_cleanup(entries)

                with mock.patch.object(
                    fsops_module,
                    "_cleanup_publication_files",
                    new=signal_during_cleanup,
                ):
                    result = fsops_module.main(
                        [
                            "publish-stage1",
                            "--output-dir",
                            str(output),
                            "--init-source",
                            str(init_source),
                            "--archive-source",
                            str(archive_source),
                        ]
                    )

                self.assertEqual(result, 128 + signum)
                self.assertEqual(
                    self.stage1_publication_snapshot(output),
                    {
                        "init": (b"new-stage1-init", 0o755),
                        "initramfs.cpio": (b"new-stage1-archive", 0o644),
                    },
                )
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o711)


if __name__ == "__main__":
    unittest.main()
