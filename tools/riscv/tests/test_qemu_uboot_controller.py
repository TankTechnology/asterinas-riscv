#!/usr/bin/env python3

from __future__ import annotations

from tools.riscv.tests.support.qemu_uboot import *


class QemuControllerTests(unittest.TestCase):
    def test_serial_interaction_executes_guarded_steps_in_order(self) -> None:
        self.assertTrue(hasattr(SESSION_MODULE, "SerialInputStep"))

        first_input = b"first\n"
        interrupt_input = b"\x03"
        final_input = b"final\n"
        interrupt_ready_line = b"INTERRUPT_READY"
        prompt = b"~ # "

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "staged_interactive_qemu.py"
            received_path = directory / "received.bin"
            fake.write_text(
                f"""\
import sys
import time

print("=> ", end="", flush=True)
for line in sys.stdin.buffer:
    if line.startswith(b"booti "):
        sys.stdout.buffer.write({RX_READY_LINE!r} + b"\\n" + {prompt!r})
        sys.stdout.buffer.flush()
        first = sys.stdin.buffer.read({len(first_input)})
        sys.stdout.buffer.write({interrupt_ready_line!r} + b"\\n")
        sys.stdout.buffer.flush()
        interrupt = sys.stdin.buffer.read({len(interrupt_input)})
        sys.stdout.buffer.write({prompt!r})
        sys.stdout.buffer.flush()
        final = sys.stdin.buffer.read({len(final_input)})
        open(sys.argv[1], "wb").write(first + interrupt + final)
        sys.stdout.buffer.write({RX_ACK_LINE!r} + b"\\n")
        sys.stdout.buffer.flush()
        while True:
            time.sleep(1)
"""
            )
            interaction = SESSION_MODULE.SerialInteraction(
                ready_line=RX_READY_LINE,
                input_steps=(
                    SESSION_MODULE.SerialInputStep(
                        ready_token=prompt,
                        input_bytes=first_input,
                    ),
                    SESSION_MODULE.SerialInputStep(
                        ready_line=interrupt_ready_line,
                        input_bytes=interrupt_input,
                    ),
                    SESSION_MODULE.SerialInputStep(
                        ready_token=prompt,
                        input_bytes=final_input,
                    ),
                ),
                completion_line=RX_ACK_LINE,
            )
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake), str(received_path)],
                commands=(
                    qemu_uboot_booti.BootCommand(
                        "booti",
                        "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
                        "Starting kernel ...",
                    ),
                ),
                raw_log_path=directory / "serial.log",
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=0.5,
                serial_interaction=interaction,
            )

            self.assertTrue(result.marker_seen)
            self.assertFalse(result.timed_out)
            self.assertIsNone(result.failure)
            self.assertTrue(result.cleanup_complete)
            self.assertEqual(
                received_path.read_bytes(),
                first_input + interrupt_input + final_input,
            )

    def _run_serial_interaction(
        self,
        *,
        ready_chunks: tuple[bytes, ...],
        completion_chunks: tuple[bytes, ...],
        boot_timeout: float = 0.2,
        input_ready_token: bytes | None = None,
        terminal_action: Callable[[], None] | None = None,
    ) -> tuple[object, bytes]:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "interactive_qemu.py"
            received_path = directory / "received.bin"
            fake.write_text(
                f"""\
import sys
import time

ready_chunks = {ready_chunks!r}
completion_chunks = {completion_chunks!r}
print("=> ", end="", flush=True)
for line in sys.stdin.buffer:
    if line.startswith(b"booti "):
        for chunk in ready_chunks:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            time.sleep(0.01)
        received = sys.stdin.buffer.readline()
        open(sys.argv[1], "wb").write(received)
        for chunk in completion_chunks:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            time.sleep(0.01)
        while True:
            time.sleep(1)
"""
            )
            interaction = SESSION_MODULE.SerialInteraction(
                ready_line=RX_READY_LINE,
                input_steps=(
                    SESSION_MODULE.SerialInputStep(
                        input_bytes=RX_INPUT_BYTES,
                        ready_token=input_ready_token,
                    ),
                ),
                completion_line=RX_ACK_LINE,
            )
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake), str(received_path)],
                commands=(
                    qemu_uboot_booti.BootCommand(
                        "booti",
                        "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
                        "Starting kernel ...",
                    ),
                ),
                raw_log_path=directory / "serial.log",
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=boot_timeout,
                termination_grace=0.5,
                serial_interaction=interaction,
                terminal_action=terminal_action,
            )
            received = received_path.read_bytes() if received_path.exists() else b""

        return result, received

    def test_serial_interaction_runs_terminal_action_before_cleanup(self) -> None:
        events: list[str] = []

        result, _ = self._run_serial_interaction(
            ready_chunks=(RX_READY_LINE + b"\n",),
            completion_chunks=(RX_ACK_LINE + b"\n",),
            terminal_action=lambda: events.append("capture"),
        )

        self.assertTrue(result.cleanup_complete)
        self.assertEqual(events, ["capture"])

    def test_serial_interaction_does_not_run_terminal_action_without_terminal(self) -> None:
        events: list[str] = []

        result, _ = self._run_serial_interaction(
            ready_chunks=(RX_READY_LINE,),
            completion_chunks=(),
            boot_timeout=0.05,
            terminal_action=lambda: events.append("capture"),
        )

        self.assertTrue(result.timed_out)
        self.assertEqual(events, [])

    def test_serial_interaction_cleans_up_when_terminal_action_fails(self) -> None:
        def fail_capture() -> None:
            raise RuntimeError("capture failed")

        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            self._run_serial_interaction(
                ready_chunks=(RX_READY_LINE + b"\n",),
                completion_chunks=(RX_ACK_LINE + b"\n",),
                terminal_action=fail_capture,
            )

    def test_terminal_action_timeout_cleans_process_and_joins_released_worker(self) -> None:
        released = threading.Event()
        cleanup_seen = threading.Event()
        real_cleanup = SESSION_MODULE._cleanup_serial_process

        def action() -> None:
            self.assertTrue(cleanup_seen.wait(1))
            released.set()

        def cleanup(*args: object, **kwargs: object) -> object:
            result = real_cleanup(*args, **kwargs)
            cleanup_seen.set()
            return result

        with (
            mock.patch.object(SESSION_MODULE, "TERMINAL_ACTION_TIMEOUT_SECONDS", 0.02),
            mock.patch.object(SESSION_MODULE, "_cleanup_serial_process", side_effect=cleanup),
        ):
            with self.assertRaisesRegex(TimeoutError, "terminal action exceeded"):
                self._run_serial_interaction(
                    ready_chunks=(RX_READY_LINE + b"\n",),
                    completion_chunks=(RX_ACK_LINE + b"\n",),
                    terminal_action=action,
                )
        self.assertTrue(released.wait(1))

    def test_terminal_action_protocol_error_does_not_block_cleanup(self) -> None:
        cleanup_seen = threading.Event()
        released = threading.Event()
        real_cleanup = SESSION_MODULE._cleanup_serial_process
        real_read = SESSION_MODULE._SerialProtocol._read_serial_chunk

        def action() -> None:
            self.assertTrue(cleanup_seen.wait(1))
            released.set()

        def cleanup(*args: object, **kwargs: object) -> object:
            result = real_cleanup(*args, **kwargs)
            cleanup_seen.set()
            return result

        def fail_drain(protocol: object, *, deadline: float, needle: bytes):
            if needle == b"terminal action drain":
                raise OSError("injected terminal drain failure")
            return real_read(protocol, deadline=deadline, needle=needle)

        with (
            mock.patch.object(SESSION_MODULE, "_cleanup_serial_process", side_effect=cleanup),
            mock.patch.object(SESSION_MODULE._SerialProtocol, "_read_serial_chunk", autospec=True, side_effect=fail_drain),
        ):
            with self.assertRaisesRegex(TimeoutError, "terminal action exceeded"):
                self._run_serial_interaction(
                    ready_chunks=(RX_READY_LINE + b"\n",),
                    completion_chunks=(RX_ACK_LINE + b"\n",),
                    terminal_action=action,
                )
        self.assertTrue(released.wait(1))

    def test_terminal_action_base_exception_does_not_block_cleanup(self) -> None:
        cleanup_seen = threading.Event()
        released = threading.Event()
        real_cleanup = SESSION_MODULE._cleanup_serial_process
        real_read = SESSION_MODULE._SerialProtocol._read_serial_chunk

        def action() -> None:
            self.assertTrue(cleanup_seen.wait(1))
            released.set()

        def cleanup(*args: object, **kwargs: object) -> object:
            result = real_cleanup(*args, **kwargs)
            cleanup_seen.set()
            return result

        def interrupt_drain(protocol: object, *, deadline: float, needle: bytes):
            if needle == b"terminal action drain":
                raise KeyboardInterrupt()
            return real_read(protocol, deadline=deadline, needle=needle)

        with (
            mock.patch.object(SESSION_MODULE, "TERMINAL_ACTION_TIMEOUT_SECONDS", 0.02),
            mock.patch.object(SESSION_MODULE, "_cleanup_serial_process", side_effect=cleanup),
            mock.patch.object(SESSION_MODULE._SerialProtocol, "_read_serial_chunk", autospec=True, side_effect=interrupt_drain),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._run_serial_interaction(
                    ready_chunks=(RX_READY_LINE + b"\n",),
                    completion_chunks=(RX_ACK_LINE + b"\n",),
                    terminal_action=action,
                )
        self.assertTrue(cleanup_seen.is_set())
        self.assertTrue(released.wait(1))

    def test_terminal_action_late_milestone_is_command_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            release = directory / "release"
            fake = directory / "late_milestone_qemu.py"
            fake.write_text(
                "import os\nimport sys\nimport time\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('>>> Hello from RISC-V userspace on Asterinas! <<<', flush=True)\n"
                "  while not os.path.exists(sys.argv[1]): time.sleep(.001)\n"
                "  print('KERNEL_READY', flush=True)\n"
                "  time.sleep(60)\n"
            )
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake), str(release)],
                commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                raw_log_path=directory / "serial",
                startup_timeout=1,
                command_timeout=1,
                boot_timeout=1,
                termination_grace=0.5,
                milestone_expectations=(
                    SESSION_MODULE.MilestoneExpectation(
                        SESSION_MODULE.BootMilestone.FIRMWARE_READY,
                        b"FIRMWARE_READY",
                    ),
                    SESSION_MODULE.MilestoneExpectation(
                        SESSION_MODULE.BootMilestone.KERNEL_READY,
                        b"KERNEL_READY",
                    ),
                ),
                terminal_action=lambda: release.write_text("release"),
            )
            self.assertEqual(result.failure, "command-validation:booti")
            self.assertTrue(result.cleanup_complete)

    def test_cleanup_base_exception_supersedes_terminal_action_error(self) -> None:
        real_cleanup = SESSION_MODULE._cleanup_serial_process

        def cleanup(*args: object, **kwargs: object) -> object:
            real_cleanup(*args, **kwargs)
            raise KeyboardInterrupt()

        with mock.patch.object(
            SESSION_MODULE,
            "_cleanup_serial_process",
            side_effect=cleanup,
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self._run_serial_interaction(
                    ready_chunks=(RX_READY_LINE + b"\n",),
                    completion_chunks=(RX_ACK_LINE + b"\n",),
                    terminal_action=lambda: (_ for _ in ()).throw(RuntimeError("capture failed")),
                )
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_serial_interaction_skips_terminal_action_after_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "exiting_qemu.py"
            fake.write_text(
                f"""\
import sys

print("=> ", end="", flush=True)
for line in sys.stdin.buffer:
    if line.startswith(b"booti "):
        sys.stdout.buffer.write({RX_READY_LINE!r} + b"\\n")
        sys.stdout.buffer.flush()
        sys.stdin.buffer.readline()
        sys.stdout.buffer.write({RX_ACK_LINE!r} + b"\\n")
        sys.stdout.buffer.flush()
        raise SystemExit(0)
"""
            )
            interaction = SESSION_MODULE.SerialInteraction(
                ready_line=RX_READY_LINE,
                input_steps=(SESSION_MODULE.SerialInputStep(input_bytes=RX_INPUT_BYTES),),
                completion_line=RX_ACK_LINE,
            )
            actions: list[str] = []

            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake)],
                commands=(
                    qemu_uboot_booti.BootCommand("booti", "booti 0x80200000", "Starting kernel ..."),
                ),
                raw_log_path=directory / "serial.log",
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=0.5,
                serial_interaction=interaction,
                terminal_action=lambda: actions.append("capture"),
                post_terminal_timeout=0.05,
            )

            self.assertTrue(result.marker_seen)
            self.assertEqual(actions, [])

    def test_serial_interaction_terminal_action_observes_live_process_before_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pid_file = directory / "qemu.pid"
            fake = directory / "live_qemu.py"
            fake.write_text(
                "import os\nimport sys\nimport time\n"
                "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('>>> Hello from RISC-V userspace on Asterinas! <<<', flush=True)\n"
                "  time.sleep(60)\n"
            )
            events: list[str] = []
            def action() -> None:
                pid = int(pid_file.read_text())
                os.kill(pid, 0)
                state = (Path(f"/proc/{pid}/stat").read_text().split())[2]
                self.assertNotEqual(state, "Z")
                events.append("capture")

            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake), str(pid_file)],
                commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                raw_log_path=directory / "serial",
                startup_timeout=1,
                command_timeout=1,
                boot_timeout=1,
                termination_grace=0.5,
                terminal_action=action,
            )
            pid = int(pid_file.read_text())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertEqual(events, ["capture"])
            self.assertTrue(result.cleanup_complete)

    def test_terminal_action_worker_joins_before_termination_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "live_qemu.py"
            fake.write_text(
                "import sys\nimport time\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('>>> Hello from RISC-V userspace on Asterinas! <<<', flush=True)\n"
                "  time.sleep(60)\n"
            )
            worker_started = threading.Event()
            worker_finished = threading.Event()
            events: list[str] = []
            real_read = SESSION_MODULE._SerialProtocol._read_serial_chunk
            real_cleanup = SESSION_MODULE._cleanup_serial_process

            def action() -> None:
                worker_started.set()
                time.sleep(0.05)
                events.append("worker-finished")
                worker_finished.set()

            def terminate_drain(protocol: object, *, deadline: float, needle: bytes):
                if needle == b"terminal action drain" and worker_started.wait(1):
                    raise SESSION_MODULE._TerminationRequested(signal.SIGTERM)
                return real_read(protocol, deadline=deadline, needle=needle)

            def cleanup(*args: object, **kwargs: object) -> object:
                events.append("cleanup")
                return real_cleanup(*args, **kwargs)

            with (
                mock.patch.object(
                    SESSION_MODULE._SerialProtocol,
                    "_read_serial_chunk",
                    autospec=True,
                    side_effect=terminate_drain,
                ),
                mock.patch.object(
                    SESSION_MODULE,
                    "_cleanup_serial_process",
                    side_effect=cleanup,
                ),
            ):
                with self.assertRaises(SESSION_MODULE._TerminationRequested):
                    qemu_uboot_booti.run_serial_session(
                        [sys.executable, "-u", str(fake)],
                        commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                        raw_log_path=directory / "serial",
                        startup_timeout=1,
                        command_timeout=1,
                        boot_timeout=1,
                        termination_grace=0.5,
                        terminal_action=action,
                    )
            self.assertTrue(worker_finished.wait(1))
            self.assertEqual(events, ["worker-finished", "cleanup"])

    def test_terminal_action_is_suppressed_on_process_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            actions: list[str] = []
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-c", "import sys; print('=> ',end='',flush=True); sys.stdin.readline(); print('>>> Hello from RISC-V userspace on Asterinas! <<<',flush=True); raise SystemExit(3)"],
                commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                raw_log_path=directory / "serial",
                startup_timeout=1,
                command_timeout=1,
                boot_timeout=1,
                termination_grace=0.5,
                post_terminal_timeout=0.05,
                terminal_action=lambda: actions.append("capture"),
            )
        self.assertTrue(result.marker_seen)
        self.assertTrue((result.failure or "").startswith("process-error:"))
        self.assertEqual(actions, [])

    def test_terminal_action_failure_reaps_process_group_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pids_path = directory / "pids"
            fake = directory / "forking_qemu.py"
            fake.write_text(
                "import os\nimport subprocess\nimport sys\nimport time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(sys.argv[1], 'w').write(f'{os.getpid()} {child.pid}')\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('>>> Hello from RISC-V userspace on Asterinas! <<<', flush=True)\n"
                "  time.sleep(60)\n"
            )
            calls: list[str] = []

            def fail_capture() -> None:
                calls.append("capture")
                raise RuntimeError("capture failed")

            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                qemu_uboot_booti.run_serial_session(
                    [sys.executable, "-u", str(fake), str(pids_path)],
                    commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                    raw_log_path=directory / "serial",
                    startup_timeout=1,
                    command_timeout=1,
                    boot_timeout=1,
                    termination_grace=0.5,
                    terminal_action=fail_capture,
                )
            parent_pid, child_pid = (int(value) for value in pids_path.read_text().split())
            self.assertNotEqual(parent_pid, child_pid)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                states = []
                for pid in (parent_pid, child_pid):
                    stat_path = Path(f"/proc/{pid}/stat")
                    states.append(stat_path.read_text().split()[2] if stat_path.exists() else None)
                if states == [None, None]:
                    break
                time.sleep(0.01)
            for pid in (parent_pid, child_pid):
                stat_path = Path(f"/proc/{pid}/stat")
                self.assertFalse(stat_path.exists(), f"unreaped process {pid}")
            self.assertEqual(calls, ["capture"])

    def test_terminal_action_is_suppressed_for_reboot_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "reboot_qemu.py"
            fake.write_text(
                "import sys\nimport time\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('TRIGGER OpenSBI v1.7 U-Boot 2026.07\\n=> ', flush=True)\n"
                "  time.sleep(60)\n"
            )
            expectation = SESSION_MODULE.RebootExpectation(
                trigger_marker=b"TRIGGER",
                recovery_timeout=1,
            )
            calls: list[str] = []
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake)],
                commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                raw_log_path=directory / "serial",
                startup_timeout=1,
                command_timeout=1,
                boot_timeout=1,
                termination_grace=0.5,
                reboot_expectation=expectation,
                terminal_action=lambda: calls.append("capture"),
            )
            self.assertTrue(result.marker_seen)
            self.assertTrue(result.recovery_complete)
            self.assertFalse(result.timed_out)
            self.assertIsNone(result.failure)
            self.assertTrue(result.cleanup_complete)
            self.assertEqual(calls, [])

    def test_terminal_action_drains_late_serial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            release = directory / "release"
            emitted = directory / "emitted"
            raw_log = directory / "serial"
            fake = directory / "late_qemu.py"
            fake.write_text(
                "import os\nimport sys\nimport time\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('>>> Hello from RISC-V userspace on Asterinas! <<<', flush=True)\n"
                "  while not os.path.exists(sys.argv[1]): time.sleep(.001)\n"
                "  print('Uncaught panic: late during capture', flush=True)\n"
                "  open(sys.argv[2], 'w').write('emitted')\n"
                "  time.sleep(60)\n"
            )
            calls: list[str] = []
            def action() -> None:
                calls.append("capture")
                release.write_text("release")
                deadline = time.monotonic() + 1
                while not emitted.exists() and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertTrue(emitted.exists())
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake), str(release), str(emitted)],
                commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                raw_log_path=raw_log,
                startup_timeout=1,
                command_timeout=1,
                boot_timeout=1,
                termination_grace=0.5,
                terminal_action=action,
            )
            self.assertIn("Uncaught panic: late during capture", raw_log.read_text())
            self.assertEqual(calls, ["capture"])
            self.assertTrue(result.cleanup_complete)

    def test_terminal_action_drains_serial_backpressure_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            release = directory / "release"
            acknowledged = directory / "acknowledged"
            fake = directory / "backpressure_qemu.py"
            fake.write_text(
                "import os\nimport sys\nimport time\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('>>> Hello from RISC-V userspace on Asterinas! <<<', flush=True)\n"
                "  while not os.path.exists(sys.argv[1]): time.sleep(.001)\n"
                "  sys.stdout.buffer.write(b'X' * (1024 * 1024)); sys.stdout.buffer.flush()\n"
                "  open(sys.argv[2], 'w').write('ack')\n"
                "  time.sleep(60)\n"
            )
            def action() -> None:
                release.write_text("release")
                deadline = time.monotonic() + 0.2
                while not acknowledged.exists() and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertTrue(acknowledged.exists())
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake), str(release), str(acknowledged)],
                commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                raw_log_path=directory / "serial", startup_timeout=1, command_timeout=1,
                boot_timeout=1, termination_grace=0.5, terminal_action=action,
            )
            self.assertTrue(acknowledged.exists())
            self.assertTrue(result.cleanup_complete)

    def test_terminal_action_drain_records_serial_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            release = directory / "release"
            emitted = directory / "emitted"
            raw_log = directory / "serial"
            fake = directory / "overflow_qemu.py"
            fake.write_text(
                "import os\nimport sys\nimport time\n"
                "print('=> ', end='', flush=True)\n"
                "for line in sys.stdin:\n"
                " if line.startswith('booti '):\n"
                "  print('READY', flush=True)\n"
                "  while not os.path.exists(sys.argv[1]): time.sleep(.001)\n"
                "  sys.stdout.write('X' * 128); sys.stdout.flush()\n"
                "  open(sys.argv[2], 'w').write('emitted')\n"
                "  time.sleep(60)\n"
            )
            calls: list[str] = []
            def action() -> None:
                calls.append("capture")
                release.write_text("release")
                deadline = time.monotonic() + 1
                while not emitted.exists() and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertTrue(emitted.exists())
            with mock.patch.object(SESSION_MODULE, "SERIAL_OUTPUT_LIMIT", 64):
                result = qemu_uboot_booti.run_serial_session(
                    [sys.executable, "-u", str(fake), str(release), str(emitted)],
                    commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                    raw_log_path=raw_log, startup_timeout=1, command_timeout=1,
                    boot_timeout=1, termination_grace=0.5, completion_line=b"READY",
                    terminal_action=action,
                )
            self.assertEqual(calls, ["capture"])
            self.assertEqual(result.failure, "serial-output-limit:booti")
            self.assertTrue(result.cleanup_complete)
            self.assertLessEqual(len(raw_log.read_bytes()), 64)

    def test_serial_interaction_is_immutable_and_exchanges_one_fixed_token(self) -> None:
        interaction = SESSION_MODULE.SerialInteraction(
            ready_line=RX_READY_LINE,
            input_steps=(
                SESSION_MODULE.SerialInputStep(input_bytes=RX_INPUT_BYTES),
            ),
            completion_line=RX_ACK_LINE,
        )
        with self.assertRaises(FrozenInstanceError):
            interaction.input_steps = ()

        result, received = self._run_serial_interaction(
            ready_chunks=(RX_READY_LINE[:12], RX_READY_LINE[12:] + b"\r\n"),
            completion_chunks=(RX_ACK_LINE[:9], RX_ACK_LINE[9:] + b"\n"),
        )

        self.assertTrue(result.marker_seen)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.failure)
        self.assertEqual(result.booti_sent_count, 1)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(received, RX_INPUT_BYTES)

    def test_serial_interaction_does_not_send_on_an_incomplete_ready_line(self) -> None:
        result, received = self._run_serial_interaction(
            ready_chunks=(RX_READY_LINE,),
            completion_chunks=(),
            boot_timeout=0.05,
        )

        self.assertFalse(result.marker_seen)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.failure, "boot-timeout")
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(received, b"")

    def test_serial_interaction_waits_for_an_input_ready_token(self) -> None:
        input_ready_token = b"~ # "
        result, received = self._run_serial_interaction(
            ready_chunks=(
                RX_READY_LINE + b"\n",
                input_ready_token[:2],
                input_ready_token[2:],
            ),
            completion_chunks=(RX_ACK_LINE + b"\n",),
            input_ready_token=input_ready_token,
        )

        self.assertTrue(result.marker_seen)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.failure)
        self.assertEqual(received, RX_INPUT_BYTES)

    def test_serial_interaction_does_not_send_before_input_ready_token(self) -> None:
        input_ready_token = b"~ # "
        result, received = self._run_serial_interaction(
            ready_chunks=(RX_READY_LINE + b"\n", input_ready_token[:-1]),
            completion_chunks=(),
            boot_timeout=0.05,
            input_ready_token=input_ready_token,
        )

        self.assertFalse(result.marker_seen)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.failure, "boot-timeout")
        self.assertEqual(received, b"")

    def test_serial_interaction_requires_an_exact_completion_line(self) -> None:
        result, received = self._run_serial_interaction(
            ready_chunks=(RX_READY_LINE + b"\n",),
            completion_chunks=(RX_ACK_LINE + b" extra\n",),
            boot_timeout=0.08,
        )

        self.assertFalse(result.marker_seen)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.failure, "boot-timeout")
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(received, RX_INPUT_BYTES)

    def test_serial_interaction_rejects_a_competing_completion_line(self) -> None:
        interaction = SESSION_MODULE.SerialInteraction(
            ready_line=RX_READY_LINE,
            input_steps=(
                SESSION_MODULE.SerialInputStep(input_bytes=RX_INPUT_BYTES),
            ),
            completion_line=RX_ACK_LINE,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "competing completion line"):
                qemu_uboot_booti.run_serial_session(
                    ["must-not-run"],
                    commands=(
                        qemu_uboot_booti.BootCommand(
                            "booti",
                            "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
                            "Starting kernel ...",
                        ),
                    ),
                    raw_log_path=Path(tmp) / "serial.log",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=0.5,
                    serial_interaction=interaction,
                    completion_line=b"competing marker",
                )

    def _run_completion_output(
        self,
        output: bytes | tuple[bytes, ...],
        *,
        completion_line: bytes | None = None,
        allow_completion_token: bool = False,
        boot_timeout: float = 0.05,
        pre_boot_output: bytes = b"",
    ) -> SESSION_MODULE.SessionResult:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "fake_qemu.py"
            output_chunks = (output,) if isinstance(output, bytes) else output
            fake.write_text(
                f"""\
import sys
import time

output_chunks = {output_chunks!r}
sys.stdout.buffer.write({b"=> " + pre_boot_output!r})
sys.stdout.buffer.flush()
for line in sys.stdin.buffer:
    if line.startswith(b"booti "):
        for chunk in output_chunks:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            time.sleep(0.02)
        while True:
            time.sleep(1)
"""
            )
            kwargs = {}
            if completion_line is not None:
                kwargs["completion_line"] = completion_line
            return qemu_uboot_booti.run_serial_session(
                [sys.executable, "-u", str(fake)],
                commands=(
                    qemu_uboot_booti.BootCommand(
                        "booti",
                        "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
                        "Starting kernel ...",
                    ),
                ),
                raw_log_path=directory / "serial.log",
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=boot_timeout,
                termination_grace=0.5,
                allow_completion_token=allow_completion_token,
                **kwargs,
            )

    def test_completion_token_survives_console_prefix_and_chunk_split(self) -> None:
        marker = b"ASTERINAS_GMAC_TCP_PROBE_READY exact-contract"

        result = self._run_completion_output(
            (
                b"virtio interrupt prefix " + marker[:19],
                marker[19:] + b" kernel suffix\n",
            ),
            completion_line=marker,
            allow_completion_token=True,
            boot_timeout=1.0,
        )

        self.assertTrue(result.marker_seen)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.failure)

    def test_completion_requires_an_exact_terminated_line(self) -> None:
        marker = SESSION_MODULE.USERSPACE_MARKER
        invalid_outputs = (
            b"prefix " + marker + b"\n",
            marker + b" suffix\n",
            marker,
            marker + b"\r",
        )

        for output in invalid_outputs:
            with self.subTest(output=output):
                result = self._run_completion_output(output)
                self.assertFalse(result.marker_seen)
                self.assertTrue(result.timed_out)
                self.assertEqual(result.failure, "boot-timeout")

    def test_completion_ignores_pre_boot_marker_in_startup_residue(self) -> None:
        completion_line = SESSION_MODULE.USERSPACE_MARKER

        result = self._run_completion_output(
            b"",
            completion_line=completion_line,
            pre_boot_output=completion_line + b"\n",
        )

        self.assertFalse(result.marker_seen)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.failure, "boot-timeout")

    def test_completion_accepts_lf_and_crlf_but_not_partial_fields(self) -> None:
        completion_line = (
            b"ASTERINAS_FIRST_PROCESS_DIAG "
            b"stage=user_first_write_returned fd=1 requested=50 result=50"
        )
        valid_outputs = (
            completion_line + b"\n",
            completion_line + b"\r\n",
            completion_line + b"\r\r\n",
            (
                completion_line[:32],
                completion_line[32:] + b"\n",
            ),
            (
                completion_line,
                b"\r",
                b"\n",
            ),
        )
        invalid_outputs = (
            b"ASTERINAS_FIRST_PROCESS_DIAG stage=user_first_write_returned\n",
            completion_line.removesuffix(b" result=50") + b"\n",
            completion_line + b" extra=1\n",
        )

        for output in valid_outputs:
            with self.subTest(output=output):
                result = self._run_completion_output(
                    output,
                    completion_line=completion_line,
                    boot_timeout=1.0,
                )
                self.assertTrue(result.marker_seen)
                self.assertFalse(result.timed_out)
                self.assertIsNone(result.failure)
        for output in invalid_outputs:
            with self.subTest(output=output):
                result = self._run_completion_output(
                    output,
                    completion_line=completion_line,
                )
                self.assertFalse(result.marker_seen)
                self.assertTrue(result.timed_out)
                self.assertEqual(result.failure, "boot-timeout")

    def test_serial_output_limit_fails_and_cleans_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            raw_log = directory / "serial.log"
            limit = SESSION_MODULE.SERIAL_OUTPUT_LIMIT
            fake = directory / "flooding_qemu.py"
            fake.write_text(
                f"""\
import sys
import time

print("=> ", end="", flush=True)
for _line in sys.stdin:
    sys.stdout.buffer.write(b"x" * {limit + 65536})
    sys.stdout.buffer.flush()
    time.sleep(60)
"""
            )

            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, str(fake)],
                commands=(qemu_uboot_booti.BootCommand("gate", "echo gate", "GATE"),),
                raw_log_path=raw_log,
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=0.5,
            )

            self.assertEqual(result.failure, "serial-output-limit:gate")
            self.assertTrue(result.cleanup_complete)
            self.assertLessEqual(raw_log.stat().st_size, limit)

    @unittest.skipUnless(hasattr(os, "WNOWAIT"), "requires POSIX waitid")
    def test_reaper_preserves_the_process_group_leader_exit_status(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import os; os._exit(3)"],
            start_new_session=True,
        )
        try:
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            SESSION_MODULE._reap_process_group(process, process.pid)

            self.assertEqual(process.poll(), 3)
        finally:
            if process.returncode is None:
                process.wait(timeout=1.0)

    def test_does_not_spawn_before_the_log_directory_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            blocked_parent = directory / "not-a-directory"
            blocked_parent.write_text("blocked")
            spawned: list[subprocess.Popen[bytes]] = []
            real_popen = SESSION_MODULE.subprocess.Popen

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process

            try:
                with mock.patch.object(
                    SESSION_MODULE.subprocess,
                    "Popen",
                    side_effect=recording_popen,
                ):
                    with self.assertRaises(OSError):
                        qemu_uboot_booti.run_serial_session(
                            [sys.executable, "-c", "import time; time.sleep(60)"],
                            commands=(),
                            raw_log_path=blocked_parent / "serial.log",
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=0.5,
                        )

                self.assertEqual(spawned, [])
            finally:
                for process in spawned:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1.0)

    def test_cleans_the_process_group_if_selector_registration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spawned: list[subprocess.Popen[bytes]] = []
            real_popen = SESSION_MODULE.subprocess.Popen
            failing_selector = mock.Mock()
            failing_selector.register.side_effect = RuntimeError("register failed")

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process

            try:
                with (
                    mock.patch.object(
                        SESSION_MODULE.subprocess,
                        "Popen",
                        side_effect=recording_popen,
                    ),
                    mock.patch.object(
                        SESSION_MODULE.selectors,
                        "DefaultSelector",
                        return_value=failing_selector,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "register failed"):
                        qemu_uboot_booti.run_serial_session(
                            [sys.executable, "-c", "import time; time.sleep(60)"],
                            commands=(),
                            raw_log_path=Path(tmp) / "serial.log",
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=0.5,
                        )

                self.assertEqual(len(spawned), 1)
                with self.assertRaises(ProcessLookupError):
                    os.kill(spawned[0].pid, 0)
                failing_selector.close.assert_called_once_with()
            finally:
                for process in spawned:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=1.0)

    def test_selector_close_failure_is_recorded_after_process_group_cleanup(
        self,
    ) -> None:
        real_selector_factory = SESSION_MODULE.selectors.DefaultSelector

        class CloseFailingSelector:
            def __init__(self) -> None:
                self.delegate = real_selector_factory()

            def register(self, *args, **kwargs):
                return self.delegate.register(*args, **kwargs)

            def select(self, *args, **kwargs):
                return self.delegate.select(*args, **kwargs)

            def close(self) -> None:
                self.delegate.close()
                raise RuntimeError("selector close failed")

        with tempfile.TemporaryDirectory() as tmp:
            spawned: list[subprocess.Popen[bytes]] = []
            real_popen = SESSION_MODULE.subprocess.Popen

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process

            try:
                with (
                    mock.patch.object(
                        SESSION_MODULE.subprocess,
                        "Popen",
                        side_effect=recording_popen,
                    ),
                    mock.patch.object(
                        SESSION_MODULE.selectors,
                        "DefaultSelector",
                        side_effect=CloseFailingSelector,
                    ),
                ):
                    result = qemu_uboot_booti.run_serial_session(
                        [
                            sys.executable,
                            "-c",
                            'import time; print("=> ", end="", flush=True); time.sleep(60)',
                        ],
                        commands=(),
                        raw_log_path=Path(tmp) / "serial.log",
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=0.5,
                    )

                self.assertEqual(result.failure, "selector-cleanup:startup")
                self.assertEqual(
                    EXECUTION_MODULE._run_status(False, result),
                    EXECUTION_MODULE.RunStatus.ERROR,
                )
                self.assertTrue(result.cleanup_complete)
                self.assertEqual(len(spawned), 1)
                with self.assertRaises(ProcessLookupError):
                    os.kill(spawned[0].pid, 0)
            finally:
                for process in spawned:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=1.0)

    def test_post_terminal_window_captures_a_following_panic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_log = Path(tmp) / "serial.log"
            result = qemu_uboot_booti.run_serial_session(
                [
                    sys.executable,
                    "-c",
                    (
                        'import sys, time; print("=> ", end="", flush=True); '
                        'sys.stdin.readline(); print("READY", flush=True); '
                        'time.sleep(0.05); print("Uncaught panic", flush=True); '
                        "time.sleep(60)"
                    ),
                ],
                commands=(qemu_uboot_booti.BootCommand("booti", "booti image", ""),),
                raw_log_path=raw_log,
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=0.5,
                completion_line=b"READY",
                post_terminal_timeout=0.2,
            )

            self.assertTrue(result.marker_seen)
            self.assertIn("Uncaught panic", raw_log.read_text())

    def test_cleans_a_child_after_the_process_group_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "forking_qemu.py"
            child_pid_path = directory / "child.pid"
            fake.write_text(
                """\
import os
import signal
import sys
import time

print("=> ", end="", flush=True)
for line in sys.stdin:
    if line.startswith("booti "):
        child = os.fork()
        if child == 0:
            signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
            time.sleep(0.05)
            print(">>> Hello from RISC-V userspace on Asterinas! <<<", flush=True)
            while True:
                time.sleep(1)
        with open(sys.argv[1], "w") as pid_file:
            pid_file.write(str(child))
        os._exit(0)
"""
            )

            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, str(fake), str(child_pid_path)],
                commands=(
                    qemu_uboot_booti.BootCommand(
                        "booti",
                        "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
                        "Starting kernel ...",
                    ),
                ),
                raw_log_path=directory / "serial.log",
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=0.5,
            )
            child_pid = int(child_pid_path.read_text())

            try:
                self.assertTrue(result.marker_seen)
                self.assertIsNone(result.failure)
                self.assertTrue(result.cleanup_complete)
                self.assertEqual(result.termination_action, "SIGTERM")
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_rejects_a_process_that_exits_nonzero_after_the_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "fake_qemu.py"
            fake.write_text(
                """\
import os
import sys

print("=> ", end="", flush=True)
for line in sys.stdin:
    if line.startswith("booti "):
        print(">>> Hello from RISC-V userspace on Asterinas! <<<", flush=True)
        os._exit(3)
"""
            )

            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, str(fake)],
                commands=(
                    qemu_uboot_booti.BootCommand(
                        "booti",
                        "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
                        "Starting kernel ...",
                    ),
                ),
                raw_log_path=directory / "serial.log",
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=0.5,
            )

            self.assertTrue(result.marker_seen)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.failure, "process-error:booti")
            self.assertIn(result.termination_action, ("SIGTERM", "already-exited"))

    def test_stops_the_process_after_one_booti_reaches_userspace(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "run_serial_session"))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "fake_qemu.py"
            raw_log = directory / "serial.log"
            fake.write_text(
                """\
import sys

print("U-Boot test\\n=> ", end="", flush=True)
for line in sys.stdin:
    command = line.strip()
    print(command, flush=True)
    if command == "echo gate":
        print("GATE\\n=> ", end="", flush=True)
    elif command.startswith("booti "):
        print("Starting kernel ...", flush=True)
        print(">>> Hello from RISC-V userspace on Asterinas! <<<", flush=True)
        while True:
            pass
"""
            )
            commands = (
                qemu_uboot_booti.BootCommand("gate", "echo gate", "GATE"),
                qemu_uboot_booti.BootCommand(
                    "booti",
                    "booti 0x80200000 0x83000000:${initrd_size} 0x88000000",
                    "Starting kernel ...",
                ),
            )

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                result = qemu_uboot_booti.run_serial_session(
                    [sys.executable, str(fake)],
                    commands=commands,
                    raw_log_path=raw_log,
                    startup_timeout=2.0,
                    command_timeout=2.0,
                    boot_timeout=2.0,
                    termination_grace=1.0,
                )

            self.assertTrue(result.marker_seen)
            self.assertEqual(result.booti_sent_count, 1)
            self.assertFalse(result.timed_out)
            self.assertIsNone(result.failure)
            self.assertTrue(result.cleanup_complete)
            self.assertEqual(result.termination_action, "SIGTERM")
            self.assertIn("Hello from RISC-V userspace", raw_log.read_text())
            self.assertFalse(
                [warning for warning in caught if warning.category is ResourceWarning]
            )

    def test_classifies_a_pre_boot_timeout_and_kills_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "hanging_qemu.py"
            pid_file = directory / "pid"
            fake.write_text(
                """\
import os
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], "w").write(str(os.getpid()))
print("=> ", end="", flush=True)
for _line in sys.stdin:
    while True:
        time.sleep(1)
"""
            )
            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, str(fake), str(pid_file)],
                commands=(qemu_uboot_booti.BootCommand("gate", "echo gate", "GATE"),),
                raw_log_path=directory / "serial.log",
                startup_timeout=1.0,
                command_timeout=0.05,
                boot_timeout=1.0,
                termination_grace=0.05,
            )

            self.assertTrue(hasattr(result, "failure"))
            self.assertEqual(result.failure, "command-timeout:gate")
            self.assertTrue(result.timed_out)
            self.assertTrue(result.killed)
            self.assertTrue(result.cleanup_complete)
            self.assertEqual(result.termination_action, "SIGKILL")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_rejects_bdinfo_overlap_before_sending_the_first_load(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "memory_layout_observer"))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "fake_qemu.py"
            raw_log = directory / "serial.log"
            fake.write_text(
                """\
import sys

print("=> ", end="", flush=True)
for line in sys.stdin:
    command = line.strip()
    print(command, flush=True)
    if command == "bdinfo":
        print("memory[0] [0x80000000-0xffffffff]", flush=True)
        print("reserved[0] [0x80200000-0x80200fff]", flush=True)
        print("=> ", end="", flush=True)
    elif command.startswith("ext4load"):
        print("64 bytes read\\n=> ", end="", flush=True)
"""
            )
            artifacts = qemu_uboot_booti.ArtifactExpectations(
                kernel_size=64,
                kernel_crc32="11111111",
                dtb_size=64,
                dtb_crc32="22222222",
                initrd_size=64,
                initrd_crc32="33333333",
            )

            result = qemu_uboot_booti.run_serial_session(
                [sys.executable, str(fake)],
                commands=(
                    qemu_uboot_booti.BootCommand(
                        "memory-layout", "bdinfo", "reserved[0]"
                    ),
                    qemu_uboot_booti.BootCommand(
                        "kernel-load", "ext4load kernel", "64 bytes read"
                    ),
                ),
                raw_log_path=raw_log,
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=0.5,
                command_observer=qemu_uboot_booti.memory_layout_observer(artifacts),
            )

            self.assertEqual(result.failure, "command-validation:memory-layout")
            self.assertNotIn("ext4load kernel", raw_log.read_text())

    def test_cli_preserves_result_when_qemu_exits_during_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake_qemu = directory / "qemu-system-riscv64"
            fake_qemu.write_text(
                """#!/usr/bin/env python3
import sys

if "--version" in sys.argv:
    print("QEMU emulator version test")
    raise SystemExit(0)
print("OpenSBI v1.7")
print("U-Boot 2026.07")
print("=> ", end="", flush=True)
for line in sys.stdin:
    print(line.strip(), flush=True)
    raise SystemExit(3)
"""
            )
            fake_qemu.chmod(0o755)
            payloads = {
                "asterinas.booti": bytes(valid_linux_image()),
                "qemu-virt.dtb": b"d" * 64,
                "initramfs.cpio.gz": b"i" * 64,
            }
            for name, payload in payloads.items():
                (directory / name).write_bytes(payload)
            fake_debugfs = directory / "debugfs"
            fake_debugfs.write_text(
                """#!/usr/bin/env python3
import pathlib
import shutil
import sys

command = sys.argv[sys.argv.index("-R") + 1].split()
source = pathlib.Path(command[-2]).name
destination = pathlib.Path(command[-1])
shutil.copyfile(pathlib.Path(__file__).parent / source, destination)
"""
            )
            fake_debugfs.chmod(0o755)
            manifest = directory / "artifacts.json"
            artifacts = qemu_uboot_booti.artifact_expectations_from_paths(
                kernel=directory / "asterinas.booti",
                dtb=directory / "qemu-virt.dtb",
                initrd=directory / "initramfs.cpio.gz",
            )
            manifest.write_text(json.dumps(artifacts.__dict__))
            serial_log = directory / "serial.log"
            marker_event = directory / "marker-event.txt"
            result_path = directory / "result.json"
            (directory / "u-boot").write_bytes(b"immutable U-Boot payload")
            (directory / "boot.ext4").write_bytes(b"immutable boot disk")
            env = os.environ.copy()
            env["PATH"] = f"{directory}{os.pathsep}{env['PATH']}"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "run",
                    "--uboot",
                    str(directory / "u-boot"),
                    "--boot-disk",
                    str(directory / "boot.ext4"),
                    "--manifest",
                    str(manifest),
                    "--serial-log",
                    str(serial_log),
                    "--marker-event",
                    str(marker_event),
                    "--result",
                    str(result_path),
                ],
                capture_output=True,
                env=env,
                text=True,
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue(result_path.exists(), result.stderr)
            evidence = json.loads(result_path.read_text())
            self.assertFalse(evidence["passed"])
            self.assertEqual(evidence["profile"], "generic-sv39")
            self.assertIn("qemu_version", evidence)
            self.assertEqual(evidence["qemu_version"], "QEMU emulator version test")
            self.assertEqual(evidence["session"]["failure"], "process-error:version")
            self.assertIn("marker_seen=no", marker_event.read_text())


if __name__ == "__main__":
    unittest.main()
