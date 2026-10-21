from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from drm.firefox_drm_scene_gate import (  # noqa: E402
    classify_browser_log,
    classify_scene,
    classify_scene_with_runtime,
)
from tools.riscv.debian.rootfs.firefox_drm_scene_gate import (  # noqa: E402
    _browser_exit_watch,
    _browser_log_marker,
    _browser_postmortem,
    _browser_renderer_from_log,
    _clear_profile_lock,
    _new_session_capabilities,
    _spawn,
    _start_browser_watchers,
    _stop_browser_watchers,
    _write_profile,
    _probe_compact_snapshot,
    _parse_probe_tcp_table,
    _probe_poke_plan,
    _probe_sample,
    _probe_socket_marker,
    _probe_syscall_summary,
)
from tools.riscv.debian.rootfs.browser_m5_marionette_gate import (  # noqa: E402
    GateError,
    Marionette,
)
from tools.riscv.debian.rootfs.desktop_m4_gate import (  # noqa: E402
    DesktopM4DrmOperations,
    GateFailure,
    firefox_probe_guest_timeout_seconds,
    validate_firefox_probe_boot_timeout,
)


class _FakeProcess:
    """Minimal Popen surface for the browser watcher tests."""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.pid, timeout or 0)
        return self.returncode


class FirefoxDrmSceneGateTests(unittest.TestCase):
    def test_qemu_memory_override_is_bounded(self) -> None:
        from tools.riscv.debian.rootfs.gate_protocol import qemu_argv

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("u-boot", "boot.ext4", "root.ext2"):
                (root / name).write_bytes(b"x")
            arguments = {
                "uboot": root / "u-boot",
                "boot_disk": root / "boot.ext4",
                "root_disk": root / "root.ext2",
                "monitor_socket": root / "monitor.sock",
            }
            environment = dict(os.environ)
            environment.pop("ASTERINAS_QEMU_MEMORY", None)
            with patch.dict("os.environ", environment, clear=True):
                default = qemu_argv(**arguments)
            self.assertEqual(default[default.index("-m") + 1], "2G")
            with patch.dict("os.environ", {"ASTERINAS_QEMU_MEMORY": "4G"}):
                raised = qemu_argv(**arguments)
            self.assertEqual(raised[raised.index("-m") + 1], "4G")
            with patch.dict("os.environ", {"ASTERINAS_QEMU_MEMORY": "512M"}):
                with self.assertRaisesRegex(ValueError, "at least 1024M"):
                    qemu_argv(**arguments)
            with patch.dict("os.environ", {"ASTERINAS_QEMU_MEMORY": "4T"}):
                with self.assertRaisesRegex(ValueError, "must be"):
                    qemu_argv(**arguments)

    def test_browser_exit_watch_reports_returncode(self) -> None:
        process = _FakeProcess(2**31 - 1, returncode=-11)
        stop = threading.Event()
        output = StringIO()
        with redirect_stdout(output):
            thread = threading.Thread(
                target=_browser_exit_watch, args=(process, stop), daemon=True
            )
            thread.start()
            thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        text = output.getvalue()
        self.assertIn("FIREFOX_DRM_BROWSER_EXIT", text)
        self.assertIn(f"pid={process.pid}", text)
        self.assertIn("returncode=-11", text)

    def test_browser_exit_watch_reports_a_death_that_races_stop(self) -> None:
        class RacyProcess:
            pid = 2**31 - 2

            def poll(self) -> int:
                return -7

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired(self.pid, timeout or 0)

        stop = threading.Event()
        stop.set()
        output = StringIO()
        with redirect_stdout(output):
            _browser_exit_watch(RacyProcess(), stop)
        self.assertIn("returncode=-7", output.getvalue())

    def test_browser_postmortem_reaps_a_zombie_that_poll_missed(self) -> None:
        class ZombieProcess:
            pid = 2**31 - 3

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                return -11

        output = StringIO()
        with redirect_stdout(output):
            _browser_postmortem(ZombieProcess())
        text = output.getvalue()
        self.assertIn("state=exited", text)
        self.assertIn("returncode=-11", text)

    def test_browser_renderer_evidence_uses_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "browser.log"
            log.write_text(
                "Crash Annotation GraphicsCriticalError: |[0][GFX1-]: "
                "RenderThread::InitDeviceTask is slow: 3644\n"
                "[GFX1-]: NewRenderer::Run is slow: 4863\n",
                encoding="utf-8",
            )
            output = StringIO()
            with patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.FIREFOX_LOG",
                log,
            ), redirect_stdout(output):
                values = _browser_renderer_from_log("Generic Renderer")
        self.assertEqual(values, ("WebRender", "available", "Generic Renderer"))
        self.assertIn("FIREFOX_DRM_BROWSER_EVIDENCE", output.getvalue())

    def test_browser_renderer_evidence_prefers_virgl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "browser.log"
            log.write_text(
                "RenderThread initialised\nrenderer=virgl (Mesa 25.0)\n",
                encoding="utf-8",
            )
            with patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.FIREFOX_LOG",
                log,
            ):
                values = _browser_renderer_from_log("Generic Renderer")
        self.assertEqual(values, ("WebRender", "available", "virgl"))

    def test_browser_renderer_evidence_rejects_software(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "browser.log"
            log.write_text(
                "RenderThread initialised\nRenderer: llvmpipe (LLVM 19)\n",
                encoding="utf-8",
            )
            with patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.FIREFOX_LOG",
                log,
            ):
                with self.assertRaisesRegex(GateError, "software"):
                    _browser_renderer_from_log("Generic Renderer")

    def test_recovery_clears_a_stale_profile_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            lock = profile / "lock"
            lock.write_text("stale", encoding="utf-8")
            with patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate._profile_path",
                return_value=profile,
            ):
                _clear_profile_lock()
            self.assertFalse(lock.exists())

    def test_browser_postmortem_reports_exited_browser(self) -> None:
        process = _FakeProcess(2**31 - 1, returncode=-6)
        output = StringIO()
        with redirect_stdout(output):
            _browser_postmortem(process)
        text = output.getvalue()
        self.assertIn("state=exited", text)
        self.assertIn("returncode=-6", text)
        self.assertNotIn("POSTMORTEM_THREAD", text)

    def test_browser_postmortem_reports_live_browser(self) -> None:
        process = _FakeProcess(2**31 - 1)
        output = StringIO()
        with patch(
            "tools.riscv.debian.rootfs.firefox_drm_scene_gate.socket.create_connection",
            side_effect=ConnectionRefusedError,
        ), redirect_stdout(output):
            _browser_postmortem(process)
        text = output.getvalue()
        self.assertIn("state=running", text)
        self.assertIn("FIREFOX_DRM_POSTMORTEM_STATUS", text)
        self.assertIn("FIREFOX_DRM_POSTMORTEM_RECONNECT ok=0", text)

    def test_browser_watchers_start_and_stop(self) -> None:
        process = _FakeProcess(2**31 - 1, returncode=0)
        output = StringIO()
        with redirect_stdout(output):
            stop, threads = _start_browser_watchers(process)
            _stop_browser_watchers(process, stop, threads)
        self.assertEqual(len(threads), 2)
        text = output.getvalue()
        self.assertIn("FIREFOX_DRM_BROWSER_EXIT", text)
        self.assertIn("FIREFOX_DRM_POSTMORTEM", text)

    def test_new_session_capability_arms_are_explicit(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            self.assertEqual(
                _new_session_capabilities(),
                {"strictFileInteractability": True},
            )
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_NEWSESSION_CAPS": "legacy"}):
            self.assertEqual(
                _new_session_capabilities(),
                {
                    "acceptInsecureCerts": False,
                    "pageLoadStrategy": "none",
                    "strictFileInteractability": True,
                },
            )
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_NEWSESSION_CAPS": "envelope"}):
            self.assertEqual(
                _new_session_capabilities(),
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "acceptInsecureCerts": False,
                            "pageLoadStrategy": "none",
                            "strictFileInteractability": True,
                        },
                        "firstMatch": [],
                    }
                },
            )
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_NEWSESSION_CAPS": "unknown"}):
            with self.assertRaisesRegex(ValueError, "unknown NewSession capability arm"):
                _new_session_capabilities()

    def test_probe_socket_marker_distinguishes_missing_procfs(self) -> None:
        marker = _probe_socket_marker(
            {
                "tcp": {"error": "<unavailable:FileNotFoundError>"},
                "tcp6": [],
            }
        )
        self.assertIn("tcp=error:FileNotFoundError", marker)
        self.assertIn("tcp6=none", marker)

    def test_probe_tcp_table_keeps_only_requested_port_and_state(self) -> None:
        payload = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0B0C 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 12345 1 0000000000000000 100 0 0 10 0
   1: 00000000:1616 00000000:0000 01 00000000:00000000 00:00000000 00000000 1000 0 67890 1 0000000000000000 20 4 31 10 -1
   2: 0100007F:0B0C 0100007F:9ABC 01 00000000:00000000 00:00000000 00000000 1000 0 54321 1 0000000000000000 20 4 31 10 -1
"""
        entries = _parse_probe_tcp_table(payload, 2828)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["state"], "0A")
        self.assertEqual(entries[0]["uid"], "1000")
        self.assertEqual(entries[0]["inode"], "12345")
        self.assertEqual(entries[1]["state"], "01")

    def test_probe_tcp_table_ignores_malformed_and_other_ports(self) -> None:
        payload = "header\nshort\n0: 0100007F:0B0C bad\n"
        self.assertEqual(_parse_probe_tcp_table(payload, 2828), [])

    def test_marionette_read_exact_uses_bounded_readiness(self) -> None:
        left, right = socket.socketpair()
        try:
            client = object.__new__(Marionette)
            client._socket = left
            client._deadline = time.monotonic() + 1.0
            client._frame_body = bytearray()
            client._body_received = 0
            right.sendall(b"ok")
            self.assertEqual(client._read_exact(2), b"ok")
        finally:
            left.close()
            right.close()

    def test_probe_syscall_summary_keeps_futex_arguments(self) -> None:
        snapshot = json.dumps(
            {"current": {"sequence": 7, "number": 98, "args": [123, 4, 9, 0]}}
        )
        summary = _probe_syscall_summary(snapshot)
        self.assertIn("arg0=123", summary)
        self.assertIn("arg1=4", summary)
        self.assertIn("arg2=9", summary)

    def test_probe_syscall_summary_keeps_socket_arguments(self) -> None:
        snapshot = json.dumps(
            {"current": {"sequence": 8, "number": 212, "args": [11, 22, 33]}}
        )
        summary = _probe_syscall_summary(snapshot)
        self.assertIn("arg0=11", summary)
        self.assertIn("arg1=22", summary)
        self.assertIn("arg2=33", summary)

    def test_probe_syscall_summary_keeps_io_and_mapping_arguments(self) -> None:
        snapshot = json.dumps(
            {"current": {"sequence": 9, "number": 222, "args": [4096, 8192, 3, 0]}}
        )
        summary = _probe_syscall_summary(snapshot)
        self.assertIn("arg0=4096", summary)
        self.assertIn("arg1=8192", summary)
        self.assertIn("arg2=3", summary)

    def test_probe_timeline_snapshot_is_bounded(self) -> None:
        snapshot = {
            "threads": [{"tid": str(i), "asterinas_syscall": "x" * 10_000} for i in range(40)],
            "children": [{"pid": str(i), "asterinas_syscall": "y" * 10_000} for i in range(20)],
            "asterinas_syscall": "z" * 10_000,
        }
        compact = _probe_compact_snapshot(snapshot)
        self.assertEqual(len(compact["threads"]), 32)
        self.assertEqual(len(compact["children"]), 8)
        self.assertEqual(len(compact["threads"][0]["asterinas_syscall"]), 4096)
        self.assertEqual(len(compact["children"][0]["asterinas_syscall"]), 4096)
        self.assertEqual(len(compact["asterinas_syscall"]), 4096)

    def test_probe_sample_persists_process_and_log_boundary(self) -> None:
        snapshot = {
            "pid": 123,
            "status": {"State": "S (sleeping)", "Threads": "2", "VmRSS": "42 kB"},
            "wchan": "futex_wait",
            "schedstat": "1 2 3",
            "asterinas_syscall": "{\"current\":{\"number\":98}}",
            "threads": [],
            "children": [],
        }
        logs = {
            "browser": {"bytes": 12, "sha256": "a" * 64, "tail": "listening"},
            "moz": {"bytes": 0, "sha256": "b" * 64, "tail": ""},
        }

        class Process:
            pid = 123

        samples: list[dict[str, object]] = []
        output = StringIO()
        with patch("tools.riscv.debian.rootfs.firefox_drm_scene_gate._probe_proc_snapshot", return_value=snapshot), patch(
            "tools.riscv.debian.rootfs.firefox_drm_scene_gate._probe_log_snapshot",
            return_value=logs,
        ), redirect_stdout(output):
            _probe_sample(Process(), samples, threading.Lock())
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["process"]["pid"], 123)
        self.assertEqual(samples[0]["logs"]["browser"]["bytes"], 12)
        self.assertIn("sample_index=0", output.getvalue())
        self.assertIn("FIREFOX_DRM_PROBE_SAMPLE_LOG", output.getvalue())

    def test_probe_boot_enables_syscall_snapshots(self) -> None:
        bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        self.assertIn("asterinas.syscall_diag=1", bootargs)
        self.assertIn("asterinas.futex_profile=1", bootargs)
        self.assertIn("asterinas.syscall_profile=1", bootargs)
        self.assertIn("asterinas.tcp_diagnostic_port=2828", bootargs)
        self.assertIn("ASTERINAS_FIREFOX_NEWSESSION_CAPS=minimal", bootargs)

    def test_probe_boot_selects_only_known_new_session_capability_arm(self) -> None:
        config = object.__new__(DesktopM4DrmOperations)
        config._firefox_profile = True
        config._firefox_probe_only = True
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_NEWSESSION_CAPS": "legacy"}):
            config._select_firefox_probe_capability_arm()
        self.assertIn("ASTERINAS_FIREFOX_NEWSESSION_CAPS=legacy", config._bootargs)
        self.assertNotIn("ASTERINAS_FIREFOX_NEWSESSION_CAPS=minimal", config._bootargs)
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_NEWSESSION_CAPS": "envelope"}):
            config._select_firefox_probe_capability_arm()
        self.assertIn("ASTERINAS_FIREFOX_NEWSESSION_CAPS=envelope", config._bootargs)
        self.assertNotIn("ASTERINAS_FIREFOX_NEWSESSION_CAPS=minimal", config._bootargs)
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_NEWSESSION_CAPS": "invalid"}):
            with self.assertRaisesRegex(GateFailure, "firefox-newsession-capability-arm"):
                config._select_firefox_probe_capability_arm()

    def test_probe_poke_plan_is_explicit_and_bounded(self) -> None:
        with patch.dict(
            "os.environ",
            {"ASTERINAS_FIREFOX_PROBE_POKE": "io:30,compositor:60,all:90"},
        ):
            self.assertEqual(
                _probe_poke_plan(),
                [("io", 30.0), ("compositor", 60.0), ("all", 90.0)],
            )
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_PROBE_POKE": ""}):
            self.assertEqual(_probe_poke_plan(), [])
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_PROBE_POKE": "io:30,bogus:10"}):
            with self.assertRaisesRegex(ValueError, "unknown poke target"):
                _probe_poke_plan()
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_PROBE_POKE": "io:soon"}):
            with self.assertRaisesRegex(ValueError, "invalid poke offset"):
                _probe_poke_plan()
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_PROBE_POKE": "io:-1"}):
            with self.assertRaisesRegex(ValueError, "negative poke offset"):
                _probe_poke_plan()

    def test_probe_can_bind_profile_to_runtime_tmpfs(self) -> None:
        config = object.__new__(DesktopM4DrmOperations)
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict(
            "os.environ",
            {"ASTERINAS_FIREFOX_PROFILE_DIR": "/run/asterinas-firefox-probe"},
        ):
            config._select_firefox_profile_dir()
        self.assertIn(
            "ASTERINAS_FIREFOX_PROFILE_DIR=/run/asterinas-firefox-probe",
            config._bootargs,
        )

    def test_probe_can_enable_dri_trace(self) -> None:
        config = object.__new__(DesktopM4DrmOperations)
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_DRI_TRACE": "1"}):
            config._select_dri_trace()
        self.assertIn("asterinas.dri_trace=1", config._bootargs)
        self.assertLess(
            config._bootargs.index("asterinas.dri_trace=1"),
            config._bootargs.index(" -- "),
            "a kernel parameter after the -- separator goes to init, not the kernel",
        )
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_DRI_TRACE": "0"}):
            config._select_dri_trace()
        self.assertNotIn("asterinas.dri_trace=1", config._bootargs)

    def test_probe_can_enable_the_refusal_only_dri_trace(self) -> None:
        config = object.__new__(DesktopM4DrmOperations)
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_DRI_TRACE_FAIL": "1"}):
            config._select_dri_trace_fail()
        self.assertIn("asterinas.dri_trace_fail=1", config._bootargs)
        self.assertLess(
            config._bootargs.index("asterinas.dri_trace_fail=1"),
            config._bootargs.index(" -- "),
            "a kernel parameter after the -- separator goes to init, not the kernel",
        )
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_DRI_TRACE_FAIL": "0"}):
            config._select_dri_trace_fail()
        self.assertNotIn("asterinas.dri_trace_fail=1", config._bootargs)

    def test_probe_can_raise_the_dri_pool(self) -> None:
        config = object.__new__(DesktopM4DrmOperations)
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_DRI_POOL_MB": "256"}):
            config._select_dri_pool_mb()
        self.assertIn("asterinas.dri_pool_mb=256", config._bootargs)
        self.assertLess(
            config._bootargs.index("asterinas.dri_pool_mb=256"),
            config._bootargs.index(" -- "),
        )
        with patch.dict("os.environ", {"ASTERINAS_DRI_POOL_MB": "8"}):
            with self.assertRaisesRegex(GateFailure, "dri-pool-mb"):
                config._select_dri_pool_mb()

    def test_remote_trace_is_opt_in_and_profile_is_explicit(self) -> None:
        config = object.__new__(DesktopM4DrmOperations)
        config._firefox_profile = True
        config._firefox_probe_only = True
        config._bootargs = DesktopM4DrmOperations.FIREFOX_PROBE_BOOTARGS
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_REMOTE_TRACE": "1"}):
            config._select_firefox_probe_capability_arm()
            config._bootargs = config._bootargs.replace(
                "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1",
                "systemd.setenv=ASTERINAS_FIREFOX_PROBE_ONLY=1 "
                "systemd.setenv=ASTERINAS_FIREFOX_REMOTE_TRACE=1",
            )
            self.assertIn("ASTERINAS_FIREFOX_REMOTE_TRACE=1", config._bootargs)
            with tempfile.TemporaryDirectory() as directory:
                with patch(
                    "tools.riscv.debian.rootfs.firefox_drm_scene_gate.PROFILE",
                    Path(directory),
                ):
                    _write_profile()
                self.assertIn(
                    'user_pref("remote.log.level", "Trace");',
                    (Path(directory) / "user.js").read_text(encoding="utf-8"),
                )

    def test_crash_probe_arm_preloads_the_guest_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "browser.log"
            mozlog = Path(directory) / "moz.log"
            probe = Path(directory) / "probe.so"
            probe.write_bytes(b"")
            captured: dict[str, object] = {}

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured.update(kwargs)
                return object()

            with patch.dict(
                "os.environ", {"ASTERINAS_FIREFOX_CRASH_PROBE": "1"}
            ), patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.FIREFOX_LOG", log
            ), patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.FIREFOX_MOZ_LOG",
                mozlog,
            ), patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.CRASH_PROBE_PATH",
                str(probe),
            ), patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate._profile_path",
                lambda: Path(directory) / "profile",
            ), patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.subprocess.Popen",
                fake_popen,
            ):
                _spawn("about:blank")
            environment = captured["env"]
            self.assertEqual(environment["LD_PRELOAD"], str(probe))

    def test_crash_probe_frames_are_reported_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "browser.log"
            log.write_text(
                "ASTERINAS_CRASH_PROBE signal=11 code=1 addr=0x1234 frames=2\n"
                "ASTERINAS_CRASH_REG pc=0x1234 ra=0x5678 sp=0x9abc\n"
                "ASTERINAS_CRASH_MODULE pc=0x1234 /usr/lib/firefox-esr/libxul.so+0x123 caller=-\n"
                "ASTERINAS_CRASH_FRAME #0 /usr/lib/firefox-esr/libxul.so+0x123456 caller=-\n"
                "ASTERINAS_CRASH_FRAME #1 /lib/riscv64-linux-gnu/libc.so.6+0x2789a caller=-\n",
                encoding="utf-8",
            )
            output = StringIO()
            with patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.FIREFOX_LOG",
                log,
            ), redirect_stdout(output):
                _browser_log_marker()
        text = output.getvalue()
        self.assertIn("FIREFOX_DRM_CRASH_TRACE lines=5", text)
        self.assertIn("ASTERINAS_CRASH_FRAME #0 /usr/lib/firefox-esr/libxul.so+0x123456", text)
        self.assertIn("ASTERINAS_CRASH_MODULE pc=0x1234", text)
        self.assertIn("FIREFOX_DRM_CRASH_TRACE_FRAME", text)
        self.assertIn("FIREFOX_DRM_BROWSER_TAIL", text)

    def test_firefox_profile_waits_for_core_milestones(self) -> None:
        from tools.riscv.debian.rootfs.desktop_m4_gate import (
            DESKTOP_M4_DRM_CORE_MILESTONES,
        )

        operations = object.__new__(DesktopM4DrmOperations)
        operations._firefox_profile = True
        operations._select_milestones()
        self.assertEqual(operations.MILESTONES, DESKTOP_M4_DRM_CORE_MILESTONES)
        operations._firefox_profile = False
        operations._select_milestones()
        self.assertEqual(operations.MILESTONES, DesktopM4DrmOperations.MILESTONES)

    def test_socket_process_arm_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "tools.riscv.debian.rootfs.firefox_drm_scene_gate.PROFILE",
                Path(directory),
            ), patch.dict(
                "os.environ",
                {"ASTERINAS_FIREFOX_SOCKET_PROCESS_DISABLED": "1"},
            ):
                output = StringIO()
                with redirect_stdout(output):
                    _write_profile()
            prefs = (Path(directory) / "user.js").read_text(encoding="utf-8")
        self.assertIn('user_pref("network.process.enabled", false);', prefs)
        self.assertIn(
            "FIREFOX_DRM_PROFILE socket_process_disabled=1 source=probe",
            output.getvalue(),
        )

    def test_probe_can_place_firefox_profile_on_runtime_tmpfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ASTERINAS_FIREFOX_PROFILE_DIR": directory},
            ):
                from tools.riscv.debian.rootfs import firefox_drm_scene_gate

                self.assertEqual(firefox_drm_scene_gate._profile_path(), Path(directory))

    def test_firefox_probe_rejects_a_budget_shorter_than_guest_probe(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            self.assertEqual(firefox_probe_guest_timeout_seconds(), 300.0)
            self.assertEqual(validate_firefox_probe_boot_timeout(360.0), 360.0)
            with self.assertRaisesRegex(GateFailure, "firefox-probe-timeout-budget"):
                validate_firefox_probe_boot_timeout(359.0)
        with patch.dict("os.environ", {"ASTERINAS_FIREFOX_PROBE_TIMEOUT": "120"}):
            self.assertEqual(firefox_probe_guest_timeout_seconds(), 120.0)
            self.assertEqual(validate_firefox_probe_boot_timeout(180.0), 180.0)
            with self.assertRaisesRegex(GateFailure, "firefox-probe-timeout-budget"):
                validate_firefox_probe_boot_timeout(179.0)

    def test_kernel_futex_probe_keeps_zero_result_wake_evidence(self) -> None:
        source = (TOOLS.parent.parent / "kernel/src/process/posix_thread/futex.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn("FUTEX_WAKE_DETAIL_ZERO_LOGGED", source)
        self.assertIn("FUTEX_WAKE_DETAIL_POSITIVE_LOGGED", source)
        self.assertIn("woken={}", source)
        self.assertIn("FUTEX_WAKE_DETAIL_ZERO_LOG_LIMIT", source)
        self.assertIn("FUTEX_WAKE_DETAIL_POSITIVE_LOG_LIMIT", source)

    def test_desktop_handoff_can_skip_firefox_only_when_requested(self) -> None:
        source = (TOOLS / "debian/rootfs/desktop_m4_gate.py").read_text(encoding="utf-8")
        self.assertIn('ASTERINAS_DESKTOP_SKIP_FIREFOX', source)
        self.assertIn('!= "1"', source)

    def test_guest_gate_uses_stable_blank_context_before_scene(self) -> None:
        guest = (TOOLS / "debian/rootfs/firefox_drm_scene_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("repository_root = Path(__file__).resolve().parents[4]", guest)
        self.assertIn('os.environ.get("ASTERINAS_FIREFOX_START_URL", SCENE_URL)', guest)
        self.assertIn('"FIREFOX_DRM_START url=', guest)
        self.assertIn("def _spawn(start_url: str = START_URL)", guest)
        self.assertLess(guest.index('"WebDriver:GetWindowHandles"'), guest.index('"WebDriver:Navigate"'))
        # The scene navigation must be bounded by the scene phase, not by the
        # default 15-second command limit that a cold TCG first paint exceeds.
        self.assertIn('"WebDriver:Navigate",\n            {"url": SCENE_URL},\n            deadline,\n            limit=SCENE_TIMEOUT,', guest)
        self.assertIn('"support_source": "browser-log-deferred"', guest)
        self.assertIn("NEW_SESSION_TIMEOUT = 840.0", guest)
        self.assertIn('user_pref("javascript.enabled", true);', guest)
        self.assertIn('user_pref("webgl.force-enabled", true);', guest)
        self.assertIn('user_pref("network.captive-portal-service.enabled", false);', guest)
        self.assertIn('user_pref("network.connectivity-service.enabled", false);', guest)
        self.assertIn('user_pref("browser.region.network.url", "");', guest)
        self.assertIn('user_pref("app.update.enabled", false);', guest)
        self.assertIn('"MOZ_WEBGL_FORCE_EGL": "1"', guest)
        self.assertNotIn('"MOZ_AVOID_OPENGL_ALTOGETHER":', guest)
        self.assertIn('runtime_xauthority = "/run/asterinas.Xauthority"', guest)
        self.assertIn('"--offline",', guest)
        self.assertNotIn('"--remote-allow-system-access",', guest)
        self.assertIn("SCENE_SETTLE_SECONDS = 30.0", guest)
        self.assertIn("SCENE_POLL_INTERVAL = 2.0", guest)
        self.assertIn("script_bytes", guest)
        self.assertIn("dom_marker", guest)
        self.assertIn("dom_samples", guest)
        self.assertIn("dom_done", guest)
        self.assertIn("renderer_marker", guest)
        self.assertIn("renderer={renderer_marker}", guest)
        self.assertIn("webgl_context", guest)
        self.assertIn("webgl_debug", guest)
        self.assertIn("Firefox WebGL renderer was not reported", guest)
        self.assertIn('SCREENSHOT = Path("/tmp/asterinas-firefox-drm-scene.png")', guest)
        self.assertIn('FIREFOX_MOZ_LOG = Path("/tmp/asterinas-firefox-drm-scene-firefox.mozlog")', guest)
        self.assertIn('"timestamp,sync,Widget:4,Layers:4,WebRender:4"', guest)
        self.assertIn('"timestamp,sync,RemoteAgent:5,Marionette:5,IPC:5"', guest)
        self.assertIn("ASTERINAS_FIREFOX_MOZ_LOG_STDERR", guest)
        self.assertIn('"FIREFOX_DRM_MOZ_SINK sink=', guest)
        self.assertIn('"ipc_lines"', guest)
        self.assertIn('"session_lines"', guest)
        self.assertIn('"FIREFOX_DRM_PROFILE remote_log_level=Trace source=user.js"', guest)
        self.assertIn('user_pref("browser.dom.window.dump.enabled", true);', guest)
        self.assertIn('user_pref("browser.sessionstore.debug", true);', guest)
        self.assertIn('FIREFOX_DRM_BROWSER_EVIDENCE ', guest)
        self.assertIn('FIREFOX_DRM_BROWSER_DIAG ', guest)
        self.assertIn("def _probe_window_snapshot", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_WINDOWS "', guest)
        self.assertIn('"display": display', guest)
        self.assertIn("def _probe_process_environ", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_ENVIRON "', guest)
        self.assertIn('"environ": environ', guest)
        self.assertIn("def _probe_log_after_stop", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_LOG_AFTER_STOP "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_LOG_LINES "', guest)
        self.assertIn("def _probe_maps_region", guest)
        self.assertIn("def _probe_futex_address", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_FUTEX_MAP "', guest)
        self.assertIn("def _probe_futex_value", guest)
        self.assertIn("def _probe_startstack", guest)
        self.assertIn("def _probe_stack_strings", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_STACK_STRINGS "', guest)
        self.assertIn("def _probe_stack_words", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_STACK_WORDS "', guest)
        self.assertIn("def _probe_main_stack_words", guest)
        self.assertIn("def _probe_syscall_sp", guest)
        self.assertIn('"mode": mode', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_MAIN_STACK_WORDS "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_COMPOSITOR "', guest)
        self.assertIn("def _probe_thread_futexes", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_THREAD_FUTEX "', guest)
        self.assertIn("_WEBGL_CREATION_SCRIPT", guest)
        self.assertIn("webglcontextcreationerror", guest)
        self.assertIn('"FIREFOX_DRM_WEBGL_CREATION "', guest)
        self.assertIn("def _probe_pipe_fd", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_IPC_FD "', guest)
        self.assertIn("PROBE_POKE_SIGNAL = signal.SIGCONT", guest)
        self.assertIn("def _probe_poke_plan", guest)
        self.assertIn("ASTERINAS_FIREFOX_PROBE_POKE", guest)
        self.assertIn("def _probe_thread_tids", guest)
        self.assertIn("def _probe_tgkill", guest)
        self.assertIn("def _probe_poke", guest)
        self.assertIn("def _probe_main_block_age", guest)
        self.assertIn("def _probe_thread_stack_marker", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_THREAD_STACK "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_THREAD_STACK_WORD "', guest)
        self.assertIn("def _probe_syscall_fp", guest)
        self.assertIn("def _probe_frame_chain", guest)
        self.assertIn("def _probe_pollfds", guest)
        self.assertIn("def _probe_external_processes", guest)
        self.assertIn("def _probe_x_stimulus", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_XSTIM "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_EXTERNAL "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_POLLFD "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_FRAME_CHAIN "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_FRAME "', guest)
        self.assertIn("def _probe_new_session_with_pokes", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_BLOCK "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_POKE "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_POKE_EFFECT "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_POKE_SKIPPED "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_NEWSESSION_RESULT "', guest)
        self.assertIn("libc.syscall(", guest)
        self.assertIn('"FIREFOX_DRM_PROBE_FUTEX_VALUE "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_FUTEX_STACK "', guest)
        self.assertIn('"FIREFOX_DRM_PROBE_SAMPLE_FUTEX "', guest)
        self.assertIn('"futex_owner"', guest)
        self.assertIn("name={thread.get('Name', 'unknown')}", guest)
        self.assertIn('user_pref("webgl.disabled", true);', guest)
        self.assertIn("ASTERINAS_FIREFOX_WEBGL_DISABLED", guest)
        self.assertIn('user_pref("identity.fxaccounts.enabled", false);', guest)
        self.assertIn("ASTERINAS_FIREFOX_FXA_DISABLED", guest)
        self.assertIn("ASTERINAS_FIREFOX_WEBRENDER_DISABLED", guest)
        self.assertIn('"MOZ_WEBRENDER"] = "0"', guest)
        self.assertIn("ASTERINAS_FIREFOX_WEBRENDER_SOFTWARE", guest)
        self.assertIn('"LIBGL_ALWAYS_SOFTWARE"] = "1"', guest)
        self.assertIn('"FIREFOX_DRM_PROFILE webrender_software=1 source=probe"', guest)
        self.assertIn('"FIREFOX_DRM_PROFILE webrender_disabled=1 source=probe"', guest)
        self.assertIn('"FIREFOX_DRM_PROFILE webgl_disabled=1 source=probe"', guest)
        self.assertIn('FIREFOX_DRM_ACCESS ', guest)
        self.assertIn('FIREFOX_DRM_EGL_PROBE ', guest)
        self.assertIn('FIREFOX_DRM_GLXTEST ', guest)
        self.assertIn("software renderer", guest)
        self.assertNotIn('if "virgl" not in scene_renderer.lower()', guest)
        self.assertIn("BROWSER_DIAGNOSTIC_MAX_LINES = 24", guest)
        self.assertIn("_INSTALL_SCENE_ERROR_HOOK", guest)
        self.assertIn('"FIREFOX_DRM_SCENE_STATE "', guest)
        self.assertIn('"FIREFOX_DRM_TRANSPORT probe=pass"', guest)
        self.assertIn('os.environ["ASTERINAS_MARIONETTE_DIAGNOSTICS"] = "1"', guest)
        self.assertIn('"ASTERINAS_MARIONETTE_DIAGNOSTICS": "1"', guest)
        self.assertIn('"strictFileInteractability": True', guest)
        self.assertIn('"moz:windowless": True', guest)
        self.assertIn("def _new_session_capabilities", guest)
        self.assertIn("ASTERINAS_FIREFOX_NEWSESSION_CAPS", guest)
        self.assertIn("FIREFOX_DRM_PHASE_METRIC", guest)
        self.assertIn("FIREFOX_DRM_BOUNDARY", guest)
        self.assertIn("PROBE_ONLY", guest)
        self.assertIn("PROBE_HEADLESS", guest)
        self.assertIn("ASTERINAS_FIREFOX_PROBE_TIMEOUT", guest)
        self.assertIn("return min(600.0, max(10.0, value))", guest)
        self.assertIn("FIREFOX_DRM_PROBE_RECEIPT", guest)
        self.assertIn("FIREFOX_DRM_PROBE_THREAD", guest)
        self.assertIn("syscall={_probe_syscall_summary(thread.get('asterinas_syscall'))}", guest)
        self.assertIn("FIREFOX_DRM_PROBE_CHILD", guest)
        self.assertIn("_probe_syscall_summary(child.get('asterinas_syscall'))", guest)
        self.assertIn("FIREFOX_DRM_PROBE_SAMPLE", guest)
        self.assertIn("FIREFOX_DRM_PROBE_SAMPLE_CHILD", guest)
        self.assertIn("FIREFOX_DRM_PROBE_SAMPLE_THREAD", guest)
        self.assertIn("FIREFOX_DRM_PROBE_SAMPLE_LOG", guest)
        self.assertIn("sample_count", guest)
        self.assertIn("sample_index", guest)
        self.assertIn("_probe_sample(process, samples", guest)
        service = (TOOLS / "debian/rootfs/firefox_marionette_probe.service").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Environment=ASTERINAS_FIREFOX_PROBE_TIMEOUT=300", service)
        desktop = (TOOLS / "debian/rootfs/desktop_m4_gate.py").read_text(encoding="utf-8")
        self.assertIn("systemd.setenv", desktop)
        self.assertIn("FIREFOX_HEADLESS_PROBE_BOOTARGS", desktop)
        self.assertIn("ASTERINAS_FIREFOX_HEADLESS_PROBE", desktop)
        self.assertIn("ASTERINAS_DESKTOP_BROWSER_ENABLED=0", desktop)
        self.assertIn("asterinas.epoll_profile=1", desktop)
        self.assertIn("PROBE_CHILD_SCAN_LIMIT", guest)
        self.assertIn("PROBE_DESCENDANT_DEPTH", guest)
        self.assertIn('"relation"', guest)
        self.assertIn("def _wait_for_display", guest)
        self.assertIn('"FIREFOX_DRM_DISPLAY_WAIT "', guest)
        self.assertIn('("/usr/bin/xdpyinfo", "-display", DISPLAY)', guest)
        overlay = (TOOLS / "debian/rootfs/firefox_marionette_probe_overlay.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"browser_m5_marionette_gate.py"', overlay)
        self.assertIn('"/usr/lib/asterinas/browser_m5_marionette_gate.py"', overlay)
        self.assertIn("PROBE_SYSCALL_MAX_BYTES = 64 * 1024", guest)
        self.assertIn("asterinas_syscall", guest)
        self.assertIn("schedstat", guest)
        self.assertIn("def _run_probe", guest)

    def test_scene_uses_dom_backed_progress_markers(self) -> None:
        scene = (TOOLS / "debian/rootfs/firefox_drm_scene.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("dataset.asterinasScene", scene)
        self.assertIn("dataset.asterinasDone", scene)
        self.assertIn("dataset.asterinasSamples", scene)

    def test_scene_service_grants_render_node_group(self) -> None:
        service = (TOOLS / "debian/rootfs/firefox_drm_scene.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("User=asterinas", service)
        self.assertIn("SupplementaryGroups=render", service)

    def test_browser_log_accepts_webrender_and_virgl(self) -> None:
        result = classify_browser_log(
            "[GFX1]: RenderCompositorOGL initialized WebRender\n"
            "[GFX1]: renderer=virgl (Mesa 25.0)\n"
        )
        self.assertEqual(result, ("WebRender", "available", "virgl (Mesa 25.0)"))

    def test_browser_log_accepts_sanitized_generic_renderer(self) -> None:
        result = classify_browser_log(
            "[GFX1]: RenderCompositorOGL initialized WebRender\n"
            "[GFX1]: renderer=Generic Renderer\n"
        )
        self.assertEqual(result, ("WebRender", "available", "Generic Renderer"))

    def test_browser_log_rejects_swgl(self) -> None:
        with self.assertRaisesRegex(ValueError, "software"):
            classify_browser_log("RenderCompositorSWGL initialized\nrenderer=virgl\n")

    def test_browser_log_rejects_missing_webrender(self) -> None:
        with self.assertRaisesRegex(ValueError, "WebRender"):
            classify_browser_log("renderer=virgl (Mesa 25.0)\n")

    @staticmethod
    def receipt() -> str:
        return "\n".join(
            (
                "FIREFOX_DRM_ACCESS device=renderD128 read=1 write=1",
                "FIREFOX_DRM_VIEWPORT width=1280 height=696 dpr=1",
                "FIREFOX_DRM_SUPPORT compositing=WebRender webrender=available renderer=virgl",
                "FIREFOX_DRM_GL renderer=virgl",
                "FIREFOX_DRM_SCREENSHOT width=1280 height=696 sha256=" + "a" * 64,
                "FIREFOX_DRM_FRAMES samples=32 min_ms=8.1 p50_ms=16.2 p95_ms=24.5 max_ms=31.0",
                "FIREFOX_DRM_RECOVERY restart=pass",
            )
        )

    def test_accepts_complete_ordered_receipt(self) -> None:
        result = classify_scene(self.receipt())
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.renderer, "virgl")
        self.assertEqual(result.samples, 32)
        self.assertEqual(result.p95_ms, 24.5)

    def test_accepts_sanitized_generic_renderer_receipt(self) -> None:
        result = classify_scene(
            self.receipt()
            .replace("renderer=virgl", "renderer=Generic_Renderer")
            .replace("FIREFOX_DRM_GL renderer=virgl", "FIREFOX_DRM_GL renderer=Generic_Renderer")
        )
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.renderer, "Generic_Renderer")

    def test_rejects_scene_without_render_node_access_evidence(self) -> None:
        result = classify_scene(
            self.receipt().replace("FIREFOX_DRM_ACCESS device=renderD128 read=1 write=1\n", "")
        )
        self.assertFalse(result.passed)
        self.assertIn("renderD128", result.reason)

    def test_runtime_identity_is_bound_to_a_passing_scene(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = root / "source_patch_manifest.json"
            source_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": {
                            "package": "firefox-esr",
                            "version": "140.16.0esr-1~deb13u1",
                            "files": [{"path": "a.cpp", "sha256": "0" * 64}],
                        },
                        "patches": [
                            {"path": "0001.patch", "sha256": "1" * 64},
                            {"path": "0002.patch", "sha256": "2" * 64},
                        ],
                        "patch_options": {"strip": 1, "fuzz": 0},
                    }
                ),
                encoding="utf-8",
            )
            packages_lock = root / "packages.lock"
            packages_lock.write_text(
                "firefox-esr\triscv64\t140.16.0esr-1~deb13u1\n",
                encoding="utf-8",
            )
            result = classify_scene_with_runtime(
                self.receipt(), packages_lock, source_manifest
            )
            self.assertTrue(result.passed, result.reason)
            self.assertEqual(result.package_version, "140.16.0esr-1~deb13u1")
            self.assertTrue(result.packages_lock_sha256)

    def test_runtime_identity_rejects_an_unrelated_esr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_manifest = root / "source_patch_manifest.json"
            source_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": {
                            "package": "firefox-esr",
                            "version": "140.16.0esr-1~deb13u1",
                            "files": [{"path": "a.cpp", "sha256": "0" * 64}],
                        },
                        "patches": [
                            {"path": "0001.patch", "sha256": "1" * 64},
                            {"path": "0002.patch", "sha256": "2" * 64},
                        ],
                        "patch_options": {"strip": 1, "fuzz": 0},
                    }
                ),
                encoding="utf-8",
            )
            packages_lock = root / "packages.lock"
            packages_lock.write_text(
                "firefox-esr\triscv64\t153.4.0esr-1~deb13u1\n",
                encoding="utf-8",
            )
            result = classify_scene_with_runtime(
                self.receipt(), packages_lock, source_manifest
            )
            self.assertFalse(result.passed)
            self.assertIn("runtime package identity", result.reason)

    def test_accepts_prefixed_serial_records(self) -> None:
        serial = "\n".join(
            f"firefox-drm-scene-gate[107]: {line}" for line in self.receipt().splitlines()
        )
        result = classify_scene(serial)
        self.assertTrue(result.passed, result.reason)

    def test_large_transport_diagnostics_do_not_hide_records(self) -> None:
        serial = (
            "firefox-drm-scene-gate[107]: A_WEB_MARIONETTE_TRANSPORT "
            + "x" * 512
            + "\n"
        ) * 2000 + self.receipt()
        result = classify_scene(serial)
        self.assertTrue(result.passed, result.reason)

    def test_rejects_software_renderer(self) -> None:
        result = classify_scene(self.receipt().replace("renderer=virgl", "renderer=llvmpipe"))
        self.assertFalse(result.passed)
        self.assertIn("renderer", result.reason)

    def test_rejects_software_webgl_renderer(self) -> None:
        result = classify_scene(self.receipt().replace("FIREFOX_DRM_GL renderer=virgl", "FIREFOX_DRM_GL renderer=llvmpipe"))
        self.assertFalse(result.passed)
        self.assertIn("WebGL", result.reason)

    def test_rejects_unavailable_webrender(self) -> None:
        result = classify_scene(self.receipt().replace("webrender=available", "webrender=unavailable"))
        self.assertFalse(result.passed)

    def test_rejects_stale_or_duplicate_receipts(self) -> None:
        result = classify_scene(self.receipt() + "\n" + self.receipt().splitlines()[4])
        self.assertFalse(result.passed)
        self.assertIn("screenshot", result.reason)

    def test_rejects_bad_geometry_and_quantiles(self) -> None:
        mismatch = self.receipt().replace(
            "FIREFOX_DRM_SCREENSHOT width=1280 height=696",
            "FIREFOX_DRM_SCREENSHOT width=1278 height=690",
        )
        self.assertFalse(classify_scene(mismatch).passed)
        outside = self.receipt().replace(
            "FIREFOX_DRM_VIEWPORT width=1280 height=696",
            "FIREFOX_DRM_VIEWPORT width=1024 height=600",
        )
        result = classify_scene(outside)
        self.assertFalse(result.passed)
        self.assertIn("outside the fixed window", result.reason)
        unordered = self.receipt().replace("min_ms=8.1 p50_ms=16.2", "min_ms=20.0 p50_ms=16.2")
        self.assertFalse(classify_scene(unordered).passed)

    def test_rejects_short_frame_sample(self) -> None:
        result = classify_scene(self.receipt().replace("samples=32", "samples=8"))
        self.assertFalse(result.passed)
        self.assertIn("sample", result.reason)


if __name__ == "__main__":
    unittest.main()
