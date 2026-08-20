#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "qemu_uboot_booti.py"
PREPARE_SCRIPT = Path(__file__).resolve().parents[1] / "prepare_qemu_uboot_booti.sh"
REPO_ROOT = SCRIPT.parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
README = SCRIPT.parent / "README.md"
INITRAMFS_BUILDER = SCRIPT.parent / "make_qemu_uboot_initramfs.py"
INITRAMFS_SOURCE = SCRIPT.parent / "qemu_uboot_init.S"
sys.path.insert(0, str(SCRIPT.parent))
from megrez_contract import load_contract  # noqa: E402
import qemu_uboot_audit  # noqa: E402
from make_qemu_uboot_initramfs import (  # noqa: E402
    InitramfsEntry,
    make_newc_archive,
)
from qemu_uboot_artifacts import (  # noqa: E402
    payload_ranges,
    validate_bdinfo_memory_layout,
)
from qemu_uboot_gate import _issue_slow_run_permit  # noqa: E402
from qemu_uboot_devices import HEADLESS, MEGREZ_BASIC, QemuDeviceSet  # noqa: E402
from qemu_ppm import PpmAudit  # noqa: E402
from qemu_uboot_variants import (  # noqa: E402
    FIRST_PROCESS_CONSOLE_LOSS,
    QemuUbootVariant,
    effective_bootargs,
    validate_registered_variant,
    variant_by_name,
)

SCRIPT_SPEC = importlib.util.spec_from_file_location("qemu_uboot_booti", SCRIPT)
assert SCRIPT_SPEC is not None
assert SCRIPT_SPEC.loader is not None
qemu_uboot_booti = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = qemu_uboot_booti
SCRIPT_SPEC.loader.exec_module(qemu_uboot_booti)
SESSION_MODULE = sys.modules["qemu_uboot_session"]
EXECUTION_MODULE = sys.modules["qemu_uboot_execution"]
EXECUTION_IO_MODULE = sys.modules["qemu_uboot_execution_io"]
RX_READY_LINE = b"ASTERINAS_MEGREZ_RX_READY_20260721"
RX_INPUT_BYTES = b"ASTERINAS_MEGREZ_RX_20260721\n"
RX_ACK_LINE = b"ASTERINAS_MEGREZ_RX_ACK_20260721"


def valid_linux_image(size: int = 64) -> bytearray:
    image = bytearray(size)
    struct.pack_into("<I", image, 0x00, 0x0400006F)
    struct.pack_into("<Q", image, 0x08, 0x20_0000)
    struct.pack_into("<Q", image, 0x10, size)
    struct.pack_into("<I", image, 0x20, 2)
    image[0x30:0x38] = b"RISCV\0\0\0"
    struct.pack_into("<I", image, 0x38, 0x05435352)
    return image


CLEAN_SERIAL_LOG = """\
OpenSBI v1.7
Boot HART ISA Extensions    : sstc,zkr,svpbmt,svade
U-Boot 2026.07
=> version
U-Boot 2026.07
memory[0]       [0x80000000-0xffffffff], 0x80000000 bytes, flags: none
reserved[0]     [0x80000000-0x8004ffff], 0x50000 bytes, flags: no-map
reserved[1]     [0xfdea6000-0xfdeaafff], 0x5000 bytes, flags: no-notify
reserved[2]     [0xfdeab5d0-0xffffffff], 0x2154a30 bytes, flags: no-overwrite
11326096 bytes read
crc32 for 80200000 ... 80ccd28f ==> 57c40418
5048 bytes read
crc32 for 88000000 ... 880013b7 ==> 6e7844b8
=> setenv bootargs "console=ttyS0 loglevel=info init=/init"
=> printenv bootargs
bootargs=console=ttyS0 loglevel=info init=/init
=> fdt set /chosen bootargs "console=ttyS0 loglevel=info init=/init"
=> fdt print /chosen
    bootargs = "console=ttyS0 loglevel=info init=/init";
3411 bytes read
crc32 for 83000000 ... 83000d52 ==> 153879f1
=> echo ASTERINAS_PRE_BOOTI
ASTERINAS_PRE_BOOTI
=> booti 0x80200000 0x83000000:${initrd_size} 0x88000000
Starting kernel ...
Enter riscv_boot
>>> Hello from RISC-V userspace on Asterinas! <<<
"""

DIAGNOSTIC_PREFIX = "ASTERINAS_FIRST_PROCESS_DIAG"
REQUIRED_DIAGNOSTIC_STAGES = (
    "diagnostic_active",
    "process_components_ready",
    "device_init_ready",
    "stdio_init_ready",
    "user_enter",
    "user_first_return",
    "user_first_syscall",
    "user_first_write_returned",
)
ALL_DIAGNOSTIC_STAGES = (
    *REQUIRED_DIAGNOSTIC_STAGES[:6],
    "user_first_page_fault",
    "user_first_page_fault_handler",
    "user_page_fault_repeated",
    *REQUIRED_DIAGNOSTIC_STAGES[6:],
)
COMPLETE_DIAGNOSTIC_LINES = (
    f"{DIAGNOSTIC_PREFIX} stage=diagnostic_active console_registry=empty",
    f"{DIAGNOSTIC_PREFIX} stage=process_components_ready",
    f"{DIAGNOSTIC_PREFIX} stage=device_init_ready",
    f"{DIAGNOSTIC_PREFIX} stage=stdio_init_ready",
    f"{DIAGNOSTIC_PREFIX} stage=user_enter cpu=0 sepc=0x1000 sp=0x2000",
    f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_syscall sepc=0x1000",
    f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall id=64 sepc=0x1000",
    f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned fd=1 requested=50 result=50",
)
CONSOLE_LOSS_DIAGNOSTIC_LINES = (
    *COMPLETE_DIAGNOSTIC_LINES[:6],
    f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall id=56 sepc=0x1000",
    COMPLETE_DIAGNOSTIC_LINES[7],
)
PROCESS_STAGE_COMPLETE_LINE = "All components initialization in Process stage completed"
UART_REGISTRATION_LOG = (
    "\x1b[32m[     0.123]\x1b[0m "
    "\x1b[34mINFO  \x1b[0m: uart: Registered NS16550A as a console"
)

MEGREZ_BOOTARGS = "cpu_no_boost_1_6ghz loglevel=info init=/init"
MEGREZ_POSITIVE_SERIAL_LOG = f"""\
OpenSBI v1.7
Boot HART ISA Extensions    : sstc,svade
U-Boot 2026.07
=> version
U-Boot 2026.07
memory[0]       [0x80000000-0xffffffff], 0x80000000 bytes, flags: none
reserved[0]     [0x80000000-0x8004ffff], 0x50000 bytes, flags: no-map
reserved[1]     [0xfdea6000-0xfdeaafff], 0x5000 bytes, flags: no-notify
reserved[2]     [0xfdeab5d0-0xffffffff], 0x2154a30 bytes, flags: no-overwrite
11326096 bytes read
crc32 for 80200000 ... 80ccd28f ==> 57c40418
5048 bytes read
crc32 for 88000000 ... 880013b7 ==> 6e7844b8
=> setenv bootargs "{MEGREZ_BOOTARGS}"
=> printenv bootargs
bootargs={MEGREZ_BOOTARGS}
=> fdt set /chosen bootargs "{MEGREZ_BOOTARGS}"
=> if fdt get value aster_rng_seed /chosen rng-seed; then fdt rm /chosen rng-seed; fi
libfdt fdt_getprop(): FDT_ERR_NOTFOUND
=> fdt print /chosen
    bootargs = "{MEGREZ_BOOTARGS}";
3411 bytes read
crc32 for 83000000 ... 83000d52 ==> 153879f1
=> echo ASTERINAS_PRE_BOOTI
ASTERINAS_PRE_BOOTI
=> booti 0x80200000 0x83000000:${{initrd_size}} 0x88000000
Starting kernel ...
Enter riscv_boot
Booting 3 processors
Processor 2 started. Spinning for tasks.
Processor 3 started. Spinning for tasks.
Processor 1 started. Spinning for tasks.
All application processors started. The BSP continues to run.
OSTD initialized. Preparing components.
use randomness based on the timestamp, which is insecure
[kernel] rootfs is ready
>>> Hello from RISC-V userspace on Asterinas! <<<
"""
MEGREZ_STALE_SERIAL_LOG = (
    MEGREZ_POSITIVE_SERIAL_LOG.replace(
        f'=> setenv bootargs "{MEGREZ_BOOTARGS}"',
        '=> setenv bootargs "cpu_no_boost_1_6ghz"',
    )
    .replace(
        f"bootargs={MEGREZ_BOOTARGS}",
        "bootargs=cpu_no_boost_1_6ghz",
        1,
    )
    .replace(
        ">>> Hello from RISC-V userspace on Asterinas! <<<",
        "[     0.668] ERROR : Uncaught panic:\n"
        "\tFailed to run the init process: Error { errno: ENOENT, "
        'msg: Some("found a negative dentry") }',
    )
)


class _PreparedRunFixtures:
    @staticmethod
    def _run_artifacts() -> object:
        return qemu_uboot_booti.ArtifactExpectations(
            kernel_size=11326096,
            kernel_crc32="57c40418",
            dtb_size=5048,
            dtb_crc32="6e7844b8",
            initrd_size=3411,
            initrd_crc32="153879f1",
        )

    @staticmethod
    def _successful_session() -> object:
        return qemu_uboot_booti.SessionResult(
            marker_seen=True,
            booti_sent_count=1,
            timed_out=False,
            killed=False,
            cleanup_complete=True,
            returncode=0,
            failure=None,
            termination_action="SIGTERM",
        )

    @staticmethod
    def _materialize_run_inputs(directory: Path) -> dict[str, Path]:
        paths = {
            "uboot": directory / "u-boot",
            "boot_disk": directory / "boot.ext4",
            "manifest": directory / "manifest.json",
            "dtb_audit": directory / "qemu-dtb-audit.json",
            "source_dtb": directory / "qemu-virt.source.dtb",
            "variant_audit": directory / "qemu-dtb-variant-audit.json",
        }
        for name, path in paths.items():
            path.write_bytes(f"immutable {name}\n".encode())
        return paths

    @staticmethod
    def _passing_audit(
        profile: object,
        scenario: object,
    ) -> object:
        is_console_loss = (
            scenario is qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        )
        variant = FIRST_PROCESS_CONSOLE_LOSS if is_console_loss else None
        diagnostic_text = (
            "\n".join(CONSOLE_LOSS_DIAGNOSTIC_LINES) + "\n" if is_console_loss else ""
        )
        effective = (
            effective_bootargs(profile, FIRST_PROCESS_CONSOLE_LOSS)
            if scenario
            in {
                qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION,
                qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
            }
            else profile.bootargs
        )
        return qemu_uboot_booti.BootAudit(
            booti_command_count=1,
            userspace_marker_count=0 if is_console_loss else 1,
            effective_bootargs=effective,
            diagnostic=qemu_uboot_audit.audit_diagnostic_markers(
                diagnostic_text,
                variant=variant,
            ),
            application_processor_ids=(1, 2, 3),
            random_source="timestamp",
            classification=(
                FIRST_PROCESS_CONSOLE_LOSS.classification if is_console_loss else "PASS"
            ),
            passed=True,
            failures=(),
        )


class CommandLineTests(_PreparedRunFixtures, unittest.TestCase):
    def test_cli_validates_display_outputs_and_device_set(self) -> None:
        required = [
            "run", "--uboot", "u", "--boot-disk", "b", "--manifest", "m",
            "--serial-log", "s", "--marker-event", "e", "--result", "r",
        ]
        valid = [*required, "--device-set", "megrez-basic", "--screenshot", "shot", "--display-audit", "audit"]
        parsed = qemu_uboot_booti._parse_args(valid)
        self.assertIs(parsed.device_set, MEGREZ_BASIC)
        for arguments in (
            [*required, "--device-set", "megrez-basic", "--screenshot", "shot"],
            [*required, "--screenshot", "shot", "--display-audit", "audit"],
            [*valid, "--scenario", "stale-bootargs"],
            [*required, "--device-set", "unknown"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                qemu_uboot_booti._parse_args(arguments)

    def test_programmatic_display_validation_precedes_input_access(self) -> None:
        replaced = QemuDeviceSet("headless", ())
        with mock.patch.object(qemu_uboot_booti, "load_artifact_manifest") as manifest:
            for kwargs, message in (
                ({"device_set": replaced}, "not a registered"),
                ({"device_set": MEGREZ_BASIC}, "requires positive display outputs"),
                ({"screenshot": Path("shot")}, "provided together"),
            ):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                    qemu_uboot_booti.run_prepared(
                        uboot=Path("/missing/u"), boot_disk=Path("/missing/b"), manifest=Path("/missing/m"),
                        serial_log=Path("/missing/s"), marker_event=Path("/missing/e"), result_path=Path("/missing/r"),
                        startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                        **kwargs,
                    )
            manifest.assert_not_called()


class DisplayEvidenceTests(_PreparedRunFixtures, unittest.TestCase):
    def test_workspace_rejects_evidence_mutation_during_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result"}
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                def forge(evidence: object) -> None:
                    if evidence.label == "serial_log":
                        with outputs["serial_log"].open("r+b") as output:
                            output.write(b"FORGED")
                with mock.patch.object(EXECUTION_IO_MODULE.PinnedRegularInput, "verify_unchanged", autospec=True, side_effect=forge):
                    with self.assertRaisesRegex(RuntimeError, "changed during verification"):
                        workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)

    def test_workspace_rejects_output_directory_swap_during_evidence_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            output_directory = directory / "outputs"
            output_directory.mkdir()
            outputs = {name: output_directory / name for name in ("serial_log", "marker_event", "result_path")}
            detached = directory / "detached"
            real_verify = EXECUTION_IO_MODULE.PinnedRegularInput.verify_unchanged
            swapped = False

            def verify(evidence: object) -> None:
                nonlocal swapped
                real_verify(evidence)
                if not swapped and evidence.label == "serial_log":
                    swapped = True
                    output_directory.rename(detached)
                    output_directory.mkdir()
                    for path in outputs.values():
                        path.write_bytes(b"attacker replacement")

            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)
                with workspace.prepare_result(b"result") as prepared:
                    with prepared.retain() as result:
                        workspace.publish_result(prepared, b"result")
                        workspace.sync_result()
                        with mock.patch.object(EXECUTION_IO_MODULE.PinnedRegularInput, "verify_unchanged", autospec=True, side_effect=verify):
                            with self.assertRaisesRegex(RuntimeError, "output directory path changed"):
                                workspace.verify_after_result(serial_identity=serial, marker_identity=marker, result_identity=result.identity)

    def test_workspace_rejects_path_swap_during_evidence_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
            real_verify = EXECUTION_IO_MODULE.PinnedRegularInput.verify_unchanged
            swapped = False

            def verify(evidence: object) -> None:
                nonlocal swapped
                real_verify(evidence)
                if not swapped and evidence.label == "serial_log":
                    swapped = True
                    outputs["serial_log"].unlink()
                    outputs["serial_log"].write_bytes(b"replacement")

            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                with mock.patch.object(EXECUTION_IO_MODULE.PinnedRegularInput, "verify_unchanged", autospec=True, side_effect=verify):
                    with self.assertRaisesRegex(RuntimeError, "changed during verification"):
                        workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)

    def test_workspace_rejects_output_mutation_before_evidence_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {
                "serial_log": directory / "serial",
                "marker_event": directory / "marker",
                "result_path": directory / "result",
                "screenshot": directory / "shot",
                "display_audit": directory / "audit",
            }
            real_open = EXECUTION_IO_MODULE.PinnedRegularInput.open

            def open_evidence(path: Path, *, label: str):
                if label == "serial_log":
                    with path.open("r+b") as output:
                        output.truncate()
                        output.write(b"attacker mutation")
                return real_open(path, label=label)

            with EXECUTION_IO_MODULE.open_execution_workspace(
                **inputs, **outputs, progress_log=None
            ) as workspace:
                with mock.patch.object(
                    EXECUTION_IO_MODULE.PinnedRegularInput,
                    "open",
                    side_effect=open_evidence,
                ):
                    with self.assertRaisesRegex(RuntimeError, "published output changed"):
                        workspace.publish_evidence("serial_log", b"trusted serial")

    def test_workspace_rejects_display_output_pair_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            common = dict(
                **inputs, serial_log=directory / "serial", marker_event=directory / "marker",
                result_path=directory / "result", progress_log=None,
            )
            with self.assertRaisesRegex(ValueError, "provided together"):
                with EXECUTION_IO_MODULE.open_execution_workspace(
                    **common, screenshot=directory / "shot", display_audit=None
                ):
                    pass
            with self.assertRaisesRegex(ValueError, "overlap"):
                with EXECUTION_IO_MODULE.open_execution_workspace(
                    **common,
                    screenshot=directory / "shot",
                    display_audit=directory / "shot",
                ):
                    pass
            original_inputs = {name: path.read_bytes() for name, path in inputs.items()}
            shot = directory / "shot"
            audit = directory / "audit"
            shot.write_bytes(b"existing")
            os.link(shot, audit)
            cases = [(shot, audit, "overlap")]
            link_target = directory / "link-target"
            link_target.write_bytes(b"target")
            for name in ("screenshot", "display_audit"):
                link = directory / f"{name}-link"
                link.symlink_to(link_target)
                cases.append((link if name == "screenshot" else shot, link if name == "display_audit" else audit, "symbolic link"))
                target = directory / f"{name}-directory"
                target.mkdir()
                cases.append((target if name == "screenshot" else shot, target if name == "display_audit" else audit, "regular file"))
            for input_path in inputs.values():
                cases.append((input_path, audit, "read-only input"))
            linked_input = directory / "linked-input"
            os.link(inputs["uboot"], linked_input)
            cases.append((linked_input, audit, "read-only input"))
            for screenshot, display_audit, message in cases:
                with self.subTest(screenshot=screenshot, display_audit=display_audit):
                    with self.assertRaisesRegex(ValueError, message):
                        with EXECUTION_IO_MODULE.open_execution_workspace(
                            **common,
                            screenshot=screenshot,
                            display_audit=display_audit,
                        ):
                            self.fail("rejected workspace entered its body")
                    self.assertEqual(
                        {name: path.read_bytes() for name, path in inputs.items()},
                        original_inputs,
                    )
                    self.assertFalse((directory / "serial").exists())
                    self.assertFalse((directory / "marker").exists())
                    self.assertFalse((directory / "result").exists())

    def test_workspace_display_pair_rolls_back_on_audit_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {
                "serial_log": directory / "serial",
                "marker_event": directory / "marker",
                "result_path": directory / "result",
                "screenshot": directory / "shot",
                "display_audit": directory / "audit",
            }
            for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                path.write_bytes(b"old")
            original_inputs = {name: path.read_bytes() for name, path in inputs.items()}
            order: list[str] = []
            real_publish = EXECUTION_IO_MODULE.PreparedPublication.publish
            def publish(prepared: object) -> None:
                order.append(prepared._destination_name)
                if prepared._destination_name == "audit":
                    raise OSError("injected audit publish failure")
                real_publish(prepared)
            with EXECUTION_IO_MODULE.open_execution_workspace(
                **inputs, **outputs, progress_log=None,
            ) as workspace:
                staging = workspace._staging_directory
                with mock.patch.object(EXECUTION_IO_MODULE.PreparedPublication, "publish", autospec=True, side_effect=publish):
                    with self.assertRaisesRegex(OSError, "injected audit"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                self.assertFalse(outputs["result_path"].exists())
                self.assertFalse(outputs["screenshot"].exists())
                self.assertFalse(outputs["display_audit"].exists())
                self.assertEqual(order, ["shot", "audit"])
                self.assertEqual({name: path.read_bytes() for name, path in inputs.items()}, original_inputs)
            self.assertFalse(staging.exists())

    def test_workspace_display_removal_failure_clears_invalidated_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {
                "serial_log": directory / "serial",
                "marker_event": directory / "marker",
                "result_path": directory / "result",
                "screenshot": directory / "shot",
                "display_audit": directory / "audit",
            }
            for path in (
                outputs["result_path"],
                outputs["screenshot"],
                outputs["display_audit"],
            ):
                path.write_bytes(b"old")
            real_remove = EXECUTION_IO_MODULE.ExecutionWorkspace._remove_output
            remove_attempts: list[str] = []
            fail_screenshot_once = True

            def remove_output(workspace: object, name: str) -> None:
                nonlocal fail_screenshot_once
                remove_attempts.append(name)
                if name == "screenshot" and fail_screenshot_once:
                    fail_screenshot_once = False
                    raise OSError("injected screenshot removal failure")
                real_remove(workspace, name)

            with EXECUTION_IO_MODULE.open_execution_workspace(
                **inputs, **outputs, progress_log=None
            ) as workspace:
                with mock.patch.object(
                    EXECUTION_IO_MODULE.ExecutionWorkspace,
                    "_remove_output",
                    autospec=True,
                    side_effect=remove_output,
                ):
                    with self.assertRaisesRegex(OSError, "injected screenshot removal"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                self.assertFalse(outputs["result_path"].exists())
                self.assertFalse(outputs["screenshot"].exists())
                self.assertFalse(outputs["display_audit"].exists())
            self.assertEqual(
                remove_attempts,
                [
                    "result_path",
                    "screenshot",
                    "result_path",
                    "screenshot",
                    "display_audit",
                ],
            )

    def test_workspace_rejects_reconstructed_absent_display_output(self) -> None:
        for phase in ("staging", "result"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                outputs = {
                    "serial_log": directory / "serial",
                    "marker_event": directory / "marker",
                    "result_path": directory / "result",
                    "screenshot": directory / "shot",
                    "display_audit": directory / "audit",
                }
                with EXECUTION_IO_MODULE.open_execution_workspace(
                    **inputs, **outputs, progress_log=None
                ) as workspace:
                    serial_identity = workspace.publish_evidence("serial_log", b"serial")
                    marker_identity = workspace.publish_evidence("marker_event", b"marker")
                    self.assertEqual(workspace.publish_display_evidence(None, None), (None, None))
                    if phase == "staging":
                        outputs["screenshot"].write_bytes(b"attacker reconstruction")
                        with self.assertRaisesRegex(RuntimeError, "unexpected output entry"):
                            workspace.verify_and_cleanup_staging(
                                serial_identity=serial_identity,
                                marker_identity=marker_identity,
                            )
                        continue
                    workspace.verify_and_cleanup_staging(
                        serial_identity=serial_identity,
                        marker_identity=marker_identity,
                    )
                    with workspace.prepare_result(b"result") as prepared_result:
                        with prepared_result.retain() as result_publication:
                            workspace.publish_result(prepared_result, b"result")
                            workspace.sync_result()
                            outputs["screenshot"].write_bytes(b"attacker reconstruction")
                            with self.assertRaisesRegex(RuntimeError, "unexpected output entry"):
                                workspace.verify_after_result(
                                    serial_identity=serial_identity,
                                    marker_identity=marker_identity,
                                    result_identity=result_publication.identity,
                                )

    def test_workspace_display_prepare_failure_preserves_old_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
            for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                path.write_bytes(b"old")
            real_prepare = EXECUTION_IO_MODULE.PinnedOutputDirectory.prepare_atomic_write
            calls = 0
            def fail_second(directory_pin: object, name: str, payload: bytes):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second prepare failed")
                return real_prepare(directory_pin, name, payload)
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                with mock.patch.object(EXECUTION_IO_MODULE.PinnedOutputDirectory, "prepare_atomic_write", autospec=True, side_effect=fail_second):
                    with self.assertRaisesRegex(OSError, "second prepare"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                    self.assertEqual(path.read_bytes(), b"old")

    def test_workspace_display_retain_failure_preserves_old_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
            for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                path.write_bytes(b"old")
            real_retain = EXECUTION_IO_MODULE.PreparedPublication.retain
            calls = 0
            def fail_second(prepared: object) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second retain failed")
                return real_retain(prepared)
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                with mock.patch.object(EXECUTION_IO_MODULE.PreparedPublication, "retain", autospec=True, side_effect=fail_second):
                    with self.assertRaisesRegex(OSError, "second retain"):
                        workspace.publish_display_evidence(b"new shot", b"new audit")
                for path in (outputs["result_path"], outputs["screenshot"], outputs["display_audit"]):
                    self.assertEqual(path.read_bytes(), b"old")

    def test_megrez_basic_publishes_guarded_framebuffer_evidence(self) -> None:
        profile = qemu_uboot_booti.GENERIC_SV39
        scenario = qemu_uboot_booti.BootScenario.POSITIVE
        payload = b"P6\n1280 1024\n255\n" + b"\x01\x02\x03" * (1280 * 1024)
        ppm_audit = PpmAudit(1280, 1024, 255, 1280 * 1024, 3, (0, 0, 1279, 1023), True)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            seen: dict[str, object] = {}

            def qemu_arguments(**kwargs: object) -> list[str]:
                seen.update(kwargs)
                return ["qemu-system-riscv64"]

            def session(_argv: list[str], **kwargs: object) -> object:
                self.assertIsNotNone(kwargs["terminal_action"])
                kwargs["terminal_action"]()
                return self._successful_session()

            def capture(socket: Path, shot: Path, *, capture_root: Path) -> bytes:
                self.assertEqual(capture_root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(socket, capture_root / "qmp.sock")
                self.assertEqual(shot, capture_root / "shot.ppm")
                seen["capture_root"] = capture_root
                return payload

            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=qemu_arguments),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(profile, scenario)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=capture),
                mock.patch.object(qemu_uboot_booti, "audit_ppm", return_value=ppm_audit),
                mock.patch.object(EXECUTION_MODULE, "boot_commands", wraps=qemu_uboot_booti.boot_commands) as commands,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    screenshot=directory / "shot.ppm", display_audit=directory / "audit.json", device_set=MEGREZ_BASIC,
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertIs(seen["device_set"], MEGREZ_BASIC)
            self.assertIs(commands.call_args.kwargs["device_set"], MEGREZ_BASIC)
            self.assertEqual((directory / "shot.ppm").read_bytes(), payload)
            expected_audit = json.dumps(qemu_uboot_booti.asdict(ppm_audit), indent=2, sort_keys=True) + "\n"
            self.assertEqual((directory / "audit.json").read_text(), expected_audit)
            self.assertEqual(result.device_set, "megrez-basic")
            self.assertEqual(result.screenshot_sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(result.display_audit, qemu_uboot_booti.asdict(ppm_audit))
            self.assertFalse(Path(seen["capture_root"]).exists())

    def test_headless_execution_has_no_capture_side_effects(self) -> None:
        profile = qemu_uboot_booti.GENERIC_SV39
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            (directory / "result").write_bytes(b"stale result")
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]) as argv,
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", return_value=self._successful_session()) as session,
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(profile, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump") as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
                mock.patch.object(EXECUTION_MODULE, "boot_commands", wraps=qemu_uboot_booti.boot_commands) as commands,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertIs(argv.call_args.kwargs["device_set"], HEADLESS)
            self.assertIsNone(argv.call_args.kwargs["device_paths"])
            self.assertIs(commands.call_args.kwargs["device_set"], HEADLESS)
            self.assertIsNone(session.call_args.kwargs["terminal_action"])
            capture.assert_not_called()
            ppm.assert_not_called()
            self.assertEqual((result.device_set, result.screenshot_sha256, result.display_audit), ("headless", None, None))
            self.assertNotEqual((directory / "result").read_bytes(), b"stale result")

    def test_headless_workspace_replaces_a_preexisting_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result"}
            outputs["result_path"].write_bytes(b"stale result")
            with EXECUTION_IO_MODULE.open_execution_workspace(**inputs, **outputs, progress_log=None) as workspace:
                serial = workspace.publish_evidence("serial_log", b"serial")
                marker = workspace.publish_evidence("marker_event", b"marker")
                workspace.verify_and_cleanup_staging(serial_identity=serial, marker_identity=marker)
                with workspace.prepare_result(b"new result") as prepared:
                    with prepared.retain() as result:
                        workspace.publish_result(prepared, b"new result")
                        workspace.sync_result()
                        workspace.verify_after_result(serial_identity=serial, marker_identity=marker, result_identity=result.identity)
            self.assertEqual(outputs["result_path"].read_bytes(), b"new result")

    def test_framebuffer_failed_audit_retains_evidence_as_fail(self) -> None:
        payload = b"P6\n1280 1024\n255\n" + b"\0\0\0" * (1280 * 1024)
        audit = PpmAudit(1280, 1024, 255, 0, 1, None, False)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)

            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()

            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", return_value=payload),
                mock.patch.object(qemu_uboot_booti, "audit_ppm", return_value=audit),
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=inputs["boot_disk"],
                    manifest=inputs["manifest"],
                    serial_log=directory / "serial",
                    marker_event=directory / "marker",
                    result_path=directory / "result",
                    screenshot=directory / "shot",
                    display_audit=directory / "audit",
                    device_set=MEGREZ_BASIC,
                    startup_timeout=1,
                    command_timeout=1,
                    boot_timeout=1,
                    termination_grace=1,
                )
            self.assertFalse(result.passed)
            self.assertEqual(result.status, "FAIL")
            self.assertEqual((directory / "shot").read_bytes(), payload)
            self.assertEqual(result.screenshot_sha256, hashlib.sha256(payload).hexdigest())

    def test_framebuffer_capture_exception_cleans_staging_without_false_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            seen: dict[str, Path] = {}

            def qemu_arguments(**kwargs: object) -> list[str]:
                paths = kwargs["device_paths"]
                seen["capture_root"] = paths.capture_root
                return ["qemu-system-riscv64"]

            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()

            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=qemu_arguments),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=RuntimeError("capture failed")),
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as audit_ppm,
            ):
                result = qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                        serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                        screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                        startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertFalse(seen["capture_root"].exists())
            audit_ppm.assert_not_called()
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertEqual(result.status, "ERROR")
            self.assertTrue((directory / "result").exists())
            result_payload = json.loads((directory / "result").read_text())
            self.assertIsNone(result_payload["screenshot_sha256"])
            self.assertIsNone(result_payload["display_audit"])
            self.assertIn("capture-error:", (directory / "marker").read_text())

    def test_framebuffer_capture_timeout_replaces_stale_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            for name, payload in (("result", b"old result"), ("shot", b"old shot"), ("audit", b"old audit")):
                (directory / name).write_bytes(payload)
            seen: dict[str, Path] = {}
            def argv(**kwargs: object) -> list[str]:
                seen["root"] = kwargs["device_paths"].capture_root
                return ["qemu-system-riscv64"]
            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=TimeoutError("qmp timed out")),
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result", screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1)
            payload = json.loads((directory / "result").read_text())
            self.assertEqual((result.status, payload["terminal_classification"]), ("ERROR", "INCOMPLETE"))
            self.assertFalse(payload["passed"])
            self.assertIsNone(payload["screenshot_sha256"])
            self.assertIsNone(payload["display_audit"])
            self.assertIn("capture-error:", (directory / "marker").read_text())
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(seen["root"].exists())
            ppm.assert_not_called()

    def test_framebuffer_capture_value_error_replaces_stale_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            for name in ("result", "shot", "audit"):
                (directory / name).write_bytes(b"old")
            roots: list[Path] = []
            def argv(**kwargs: object) -> list[str]:
                roots.append(kwargs["device_paths"].capture_root)
                return ["qemu-system-riscv64"]
            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=ValueError("malformed QMP")),
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result", screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1)
            saved = json.loads((directory / "result").read_text())
            self.assertEqual((result.status, saved["terminal_classification"], saved["screenshot_sha256"], saved["display_audit"]), ("ERROR", "INCOMPLETE", None, None))
            self.assertFalse(saved["passed"])
            self.assertIn("capture-error:", (directory / "marker").read_text())
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(roots[0].exists())
            ppm.assert_not_called()

    def test_framebuffer_audit_value_error_withholds_capture_pair(self) -> None:
        ppm_payload = b"P6\n1280 1024\n255\n" + b"\1\2\3" * (1280 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            for name in ("result", "shot", "audit"):
                (directory / name).write_bytes(b"old")
            roots: list[Path] = []
            def argv(**kwargs: object) -> list[str]:
                roots.append(kwargs["device_paths"].capture_root)
                return ["qemu-system-riscv64"]
            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", return_value=ppm_payload) as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm", side_effect=ValueError("malformed PPM")) as audit,
            ):
                result = qemu_uboot_booti.run_prepared(uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"], serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result", screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1)
            capture.assert_called_once()
            audit.assert_called_once_with(ppm_payload, expected_width=1280, expected_height=1024)
            saved = json.loads((directory / "result").read_text())
            self.assertEqual((result.status, saved["terminal_classification"], saved["screenshot_sha256"], saved["display_audit"]), ("ERROR", "INCOMPLETE", None, None))
            self.assertFalse(saved["passed"])
            self.assertIn("capture-error:", (directory / "marker").read_text())
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(roots[0].exists())

    def test_framebuffer_boot_timeout_removes_stale_display_evidence(self) -> None:
        timeout = replace(self._successful_session(), marker_seen=False, timed_out=True, failure="boot-timeout")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            (directory / "result").write_bytes(b"old")
            (directory / "shot").write_bytes(b"old")
            (directory / "audit").write_bytes(b"old")
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", return_value=timeout),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump") as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                    serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertEqual((result.status, result.screenshot_sha256, result.display_audit), ("FAIL", None, None))
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(json.loads((directory / "result").read_text())["passed"])
            self.assertEqual(result.terminal_classification, "INCOMPLETE")
            self.assertTrue((directory / "serial").exists())
            self.assertTrue((directory / "marker").exists())
            capture.assert_not_called()
            ppm.assert_not_called()

    def test_in_place_display_evidence_mutation_revokes_result(self) -> None:
        payload = b"P6\n1280 1024\n255\n" + b"\x01\x02\x03" * (1280 * 1024)
        audit = PpmAudit(1280, 1024, 255, 1280 * 1024, 3, (0, 0, 1279, 1023), True)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            real_verify = EXECUTION_IO_MODULE.ExecutionWorkspace.verify_after_result

            def session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return self._successful_session()

            def verify_after_result(workspace: object, **kwargs: object) -> None:
                for name in ("shot", "audit"):
                    with (directory / name).open("r+b") as evidence:
                        evidence.truncate()
                        evidence.write(b"attacker mutation")
                        evidence.flush()
                        os.fsync(evidence.fileno())
                real_verify(workspace, **kwargs)

            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", return_value=payload),
                mock.patch.object(qemu_uboot_booti, "audit_ppm", return_value=audit),
                mock.patch.object(EXECUTION_IO_MODULE.ExecutionWorkspace, "verify_after_result", autospec=True, side_effect=verify_after_result),
            ):
                with self.assertRaisesRegex(RuntimeError, "screenshot changed during the run"):
                    qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                        serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                        screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                        startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                    )
            self.assertFalse(json.loads((directory / "result").read_text())["passed"])

    def test_framebuffer_process_error_withholds_display_evidence(self) -> None:
        session = replace(
            self._successful_session(),
            failure="process-error:booti",
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            seen: dict[str, Path] = {}
            def argv(**kwargs: object) -> list[str]:
                seen["capture_root"] = kwargs["device_paths"].capture_root
                return ["qemu-system-riscv64"]
            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["terminal_action"]()
                return session
            with (
                mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                mock.patch.object(qemu_uboot_booti, "qemu_argv", side_effect=argv),
                mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=run_session),
                mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                mock.patch.object(qemu_uboot_booti, "capture_screendump", side_effect=TimeoutError("capture timed out")) as capture,
                mock.patch.object(qemu_uboot_booti, "audit_ppm") as ppm,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                    serial_log=directory / "serial", marker_event=directory / "marker", result_path=directory / "result",
                    screenshot=directory / "shot", display_audit=directory / "audit", device_set=MEGREZ_BASIC,
                    startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                )
            self.assertEqual((result.status, result.terminal_classification), ("ERROR", "INCOMPLETE"))
            payload = json.loads((directory / "result").read_text())
            self.assertEqual((payload["device_set"], payload["screenshot_sha256"], payload["display_audit"]), ("megrez-basic", None, None))
            self.assertFalse(payload["passed"])
            self.assertFalse((directory / "shot").exists())
            self.assertFalse((directory / "audit").exists())
            self.assertFalse(seen["capture_root"].exists())
            capture.assert_called_once()
            ppm.assert_not_called()
            self.assertIn("process-error:booti", (directory / "marker").read_text())


class PreparedRunTests(_PreparedRunFixtures, unittest.TestCase):
    def test_progress_log_is_visible_while_serial_evidence_is_staged(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        artifacts = self._run_artifacts()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            progress = directory / "progress.log"
            serial = directory / "serial.log"

            def run_session(_argv: list[str], **kwargs: object) -> object:
                raw_log = kwargs["raw_log_file"]
                raw_log.write(b"live serial progress\n")
                raw_log.flush()
                self.assertEqual(progress.read_bytes(), b"live serial progress\n")
                self.assertFalse(serial.exists())
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
            ):
                result = qemu_uboot_booti.run_prepared(
                    **inputs,
                    serial_log=serial,
                    progress_log=progress,
                    marker_event=directory / "marker-event.txt",
                    result_path=directory / "result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    variant=FIRST_PROCESS_CONSOLE_LOSS,
                )

            self.assertTrue(result.passed)
            self.assertEqual(progress.read_bytes(), serial.read_bytes())

    def test_console_loss_commands_bind_exact_ram_and_dtb_bootargs(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        diagnostic_bootargs = (
            "cpu_no_boost_1_6ghz loglevel=info init=/init "
            "asterinas.first_process_diag=1"
        )

        commands = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
            variant=FIRST_PROCESS_CONSOLE_LOSS,
        )
        commands_by_name = {command.name: command for command in commands}

        self.assertEqual(
            commands_by_name["bootargs-env"].text,
            f'setenv bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={diagnostic_bootargs}",
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb-proof"].expected_output,
            diagnostic_bootargs,
        )
        texts = [command.text for command in commands]
        self.assertEqual(sum(text.startswith("booti ") for text in texts), 1)
        self.assertFalse(any("saveenv" in text for text in texts))

        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                profile.name,
                "--scenario",
                "first-process-console-loss",
                "--variant",
                "first-process-console-loss",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        cli_lines = cli.stdout.splitlines()
        self.assertIn(f'setenv bootargs "{diagnostic_bootargs}"', cli_lines)
        self.assertIn(
            f'fdt set /chosen bootargs "{diagnostic_bootargs}"',
            cli_lines,
        )
        self.assertNotIn(f'setenv bootargs "{profile.bootargs}"', cli_lines)

        invalid_variants = (
            None,
            replace(FIRST_PROCESS_CONSOLE_LOSS, bootarg_suffix="debug=1"),
            replace(FIRST_PROCESS_CONSOLE_LOSS),
        )
        for variant in invalid_variants:
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(
                        profile=profile,
                        scenario=(
                            qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                        ),
                        variant=variant,
                    )

    def test_positive_scenario_policy_accepts_a_bootargs_override(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        override = (
            "cpu_no_boost_1_6ghz console=ttyS0 loglevel=info init=/init"
        )

        policy = qemu_uboot_audit._scenario_policy(
            profile,
            qemu_uboot_booti.BootScenario.POSITIVE,
            None,
            bootargs_override=override,
        )

        self.assertEqual(policy.effective_bootargs, override)
        self.assertEqual(policy.environment_bootargs, override)
        with self.assertRaises(ValueError):
            qemu_uboot_audit._scenario_policy(
                profile,
                qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                None,
                bootargs_override=override,
            )

    def test_registered_console_suppression_is_fixed_and_uses_standard_payload(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        diagnostic_bootargs = (
            "cpu_no_boost_1_6ghz loglevel=info init=/init "
            "asterinas.first_process_diag=1"
        )

        commands = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=(qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION),
        )
        commands_by_name = {command.name: command for command in commands}

        self.assertEqual(
            commands_by_name["bootargs-env"].text,
            f'setenv bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={diagnostic_bootargs}",
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{diagnostic_bootargs}"',
        )
        self.assertEqual(
            commands_by_name["bootargs-dtb-proof"].expected_output,
            diagnostic_bootargs,
        )
        texts = [command.text for command in commands]
        self.assertEqual(sum(text.startswith("booti ") for text in texts), 1)
        self.assertFalse(any("saveenv" in text for text in texts))
        self.assertFalse(any(" compatible " in text for text in texts))
        self.assertFalse(any("snps,dw-apb-uart" in text for text in texts))

        positive = qemu_uboot_booti.boot_commands(profile=profile)
        self.assertEqual(
            [command.name for command in commands],
            [command.name for command in positive],
        )
        bootargs_names = {
            "bootargs-env",
            "bootargs-env-proof",
            "bootargs-dtb",
            "bootargs-dtb-proof",
        }
        for command, positive_command in zip(commands, positive):
            if command.name not in bootargs_names:
                self.assertEqual(command, positive_command)

        with self.assertRaises(ValueError):
            qemu_uboot_booti.boot_commands(
                profile=profile,
                scenario=(qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION),
                variant=FIRST_PROCESS_CONSOLE_LOSS,
            )
        for wrong_profile_name in ("generic-sv39", "megrez-sv48-svadu-fast"):
            with self.subTest(profile=wrong_profile_name):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(
                        profile=qemu_uboot_booti.profile_by_name(wrong_profile_name),
                        scenario=(
                            qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                        ),
                    )

    def test_positive_and_stale_commands_remain_unchanged(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        positive = qemu_uboot_booti.boot_commands(profile=profile)
        positive_explicit_none = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
            variant=None,
        )
        stale = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )
        stale_explicit_none = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
            variant=None,
        )

        self.assertEqual(positive_explicit_none, positive)
        self.assertEqual(stale_explicit_none, stale)
        positive_by_name = {command.name: command for command in positive}
        stale_by_name = {command.name: command for command in stale}
        self.assertEqual(
            positive_by_name["bootargs-env"].text,
            f'setenv bootargs "{profile.bootargs}"',
        )
        self.assertEqual(
            positive_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={profile.bootargs}",
        )
        self.assertEqual(
            positive_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{profile.bootargs}"',
        )
        self.assertEqual(
            positive_by_name["bootargs-dtb-proof"].expected_output,
            profile.bootargs,
        )
        self.assertEqual(
            stale_by_name["bootargs-env"].text,
            'setenv bootargs "cpu_no_boost_1_6ghz"',
        )
        self.assertEqual(
            stale_by_name["bootargs-env-proof"].expected_output,
            "bootargs=cpu_no_boost_1_6ghz",
        )
        self.assertEqual(
            stale_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{profile.bootargs}"',
        )
        self.assertEqual(
            stale_by_name["bootargs-dtb-proof"].expected_output,
            profile.bootargs,
        )
        for commands in (positive, stale):
            command_text = "\n".join(command.text for command in commands)
            expected_text = "\n".join(command.expected_output for command in commands)
            self.assertNotIn("asterinas.first_process_diag", command_text)
            self.assertNotIn("asterinas.first_process_diag", expected_text)
            self.assertEqual(
                sum(command.text.startswith("booti ") for command in commands),
                1,
            )
            self.assertFalse(any("saveenv" in command.text for command in commands))

        for scenario in (
            qemu_uboot_booti.BootScenario.POSITIVE,
            qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        ):
            with self.subTest(scenario=scenario):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

        override = "console=ttyS0 loglevel=debug init=/init"
        overridden = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
            bootargs_override=override,
        )
        overridden_by_name = {command.name: command for command in overridden}
        self.assertEqual(
            overridden_by_name["bootargs-env"].text,
            f'setenv bootargs "{override}"',
        )
        self.assertEqual(
            overridden_by_name["bootargs-env-proof"].expected_output,
            f"bootargs={override}",
        )
        self.assertEqual(
            overridden_by_name["bootargs-dtb"].text,
            f'fdt set /chosen bootargs "{override}"',
        )
        self.assertEqual(
            overridden_by_name["bootargs-dtb-proof"].expected_output,
            override,
        )

    def test_invalid_scenario_variant_pairs_fail_before_artifact_access(self) -> None:
        class UnreadableArtifacts:
            def __getattribute__(self, name: str) -> object:
                raise AssertionError(f"artifact field was accessed: {name}")

        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        wrong_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")
        drifted_profile = replace(
            profile,
            validation=replace(profile.validation, bootargs="init=/wrong"),
        )
        invalid_pairs = (
            (
                "positive-with-variant",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.POSITIVE,
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "stale-with-variant",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "console-loss-without-variant",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": None,
                },
            ),
            (
                "suppression-with-variant",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "console-loss-with-wrong-profile",
                {
                    "profile": wrong_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "suppression-with-wrong-profile",
                {
                    "profile": wrong_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                },
            ),
            (
                "console-loss-with-drifted-profile",
                {
                    "profile": drifted_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                },
            ),
            (
                "suppression-with-drifted-profile",
                {
                    "profile": drifted_profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                },
            ),
            (
                "console-loss-with-override",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
                    ),
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                    "bootargs_override": "debug=1",
                },
            ),
            (
                "suppression-with-override",
                {
                    "profile": profile,
                    "scenario": (
                        qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
                    ),
                    "bootargs_override": "debug=1",
                },
            ),
            (
                "stale-with-override",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                    "bootargs_override": "debug=1",
                },
            ),
            (
                "positive-with-variant-and-override",
                {
                    "profile": profile,
                    "scenario": qemu_uboot_booti.BootScenario.POSITIVE,
                    "variant": FIRST_PROCESS_CONSOLE_LOSS,
                    "bootargs_override": "debug=1",
                },
            ),
        )
        artifacts = UnreadableArtifacts()
        for label, arguments in invalid_pairs:
            with self.subTest(pair=label):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.boot_commands(artifacts, **arguments)

        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                profile.name,
                "--scenario",
                "first-process-console-loss",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn("requires its fixed variant", cli.stderr)

    def test_console_loss_run_binds_registered_values_and_all_identities(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        artifacts = self._run_artifacts()
        session = self._successful_session()
        audit = self._passing_audit(profile, scenario)
        payload_sha256 = "b" * 64

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            boot_disk = inputs["boot_disk"]
            source_dtb = inputs["source_dtb"]
            serial_log = directory / "serial.log"
            marker_event = directory / "marker-event.txt"
            result_path = directory / "result.json"
            dtb_audit = inputs["dtb_audit"]
            variant_audit = inputs["variant_audit"]
            serial_log.write_text("captured transcript\n")
            source_sha256 = hashlib.sha256(source_dtb.read_bytes()).hexdigest()
            disk_sha256 = hashlib.sha256(boot_disk.read_bytes()).hexdigest()
            real_qemu_argv = qemu_uboot_booti.qemu_argv

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256=payload_sha256),
                ) as verify_dtb,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    side_effect=real_qemu_argv,
                ) as build_argv,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    return_value=session,
                ) as run_session,
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=audit,
                ) as audit_log,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=boot_disk,
                    manifest=inputs["manifest"],
                    serial_log=serial_log,
                    marker_event=marker_event,
                    result_path=result_path,
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    dtb_audit=dtb_audit,
                    variant=FIRST_PROCESS_CONSOLE_LOSS,
                    source_dtb=source_dtb,
                    variant_audit=variant_audit,
                )

            expected_bootargs = effective_bootargs(
                profile,
                FIRST_PROCESS_CONSOLE_LOSS,
            )
            self.assertTrue(result.passed)
            self.assertEqual(result.variant, FIRST_PROCESS_CONSOLE_LOSS.name)
            self.assertEqual(result.effective_bootargs, expected_bootargs)
            self.assertEqual(
                (result.source_dtb_sha256_before, result.source_dtb_sha256_after),
                (source_sha256, source_sha256),
            )
            self.assertEqual(
                (result.payload_dtb_sha256_before, result.payload_dtb_sha256_after),
                (payload_sha256, payload_sha256),
            )
            self.assertEqual(
                (result.boot_disk_sha256_before, result.boot_disk_sha256_after),
                (disk_sha256, disk_sha256),
            )
            self.assertEqual(
                json.loads(result_path.read_text())["variant"], result.variant
            )
            build_call = build_argv.call_args.kwargs
            self.assertNotEqual(build_call["uboot"], inputs["uboot"])
            self.assertNotEqual(build_call["boot_disk"], inputs["boot_disk"])
            self.assertEqual(build_call["uboot"].name, "u-boot")
            self.assertEqual(build_call["boot_disk"].name, "boot.ext4")
            self.assertIs(build_call["profile"], profile)
            self.assertTrue(build_call["snapshot_disk"])
            self.assertEqual(
                run_session.call_args.kwargs["completion_line"],
                FIRST_PROCESS_CONSOLE_LOSS.completion_line,
            )
            commands = run_session.call_args.kwargs["commands"]
            self.assertIn(
                f'setenv bootargs "{expected_bootargs}"',
                [command.text for command in commands],
            )
            self.assertEqual(len(verify_dtb.call_args_list), 2)
            for call in verify_dtb.call_args_list:
                arguments = call.kwargs
                for key, original in (
                    ("boot_disk", boot_disk),
                    ("audit_path", dtb_audit),
                    ("variant_audit_path", variant_audit),
                    ("source_dtb", source_dtb),
                ):
                    self.assertNotEqual(arguments[key], original)
                    self.assertEqual(arguments[key].name, original.name)
                self.assertIs(arguments["profile"], profile)
                self.assertEqual(arguments["expected_size"], artifacts.dtb_size)
                self.assertEqual(arguments["expected_crc32"], artifacts.dtb_crc32)
                self.assertIs(arguments["variant"], FIRST_PROCESS_CONSOLE_LOSS)
            self.assertIs(
                audit_log.call_args.kwargs["variant"],
                FIRST_PROCESS_CONSOLE_LOSS,
            )
            self.assertIs(audit_log.call_args.kwargs["scenario"], scenario)

    def test_run_rejects_result_path_equal_to_source_dtb_before_side_effects(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = {
                "uboot": directory / "u-boot",
                "boot_disk": directory / "boot.ext4",
                "manifest": directory / "manifest.json",
                "dtb_audit": directory / "qemu-dtb-audit.json",
                "source_dtb": directory / "qemu-virt.source.dtb",
                "variant_audit": directory / "qemu-dtb-variant-audit.json",
            }
            for name, path in inputs.items():
                path.write_bytes(f"immutable {name}\n".encode())
            original_source = inputs["source_dtb"].read_bytes()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    side_effect=AssertionError("manifest was accessed"),
                ) as load_manifest,
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=AssertionError("QEMU session was started"),
                ) as run_session,
            ):
                with self.assertRaisesRegex(ValueError, "overlap"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=inputs["source_dtb"],
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            load_manifest.assert_not_called()
            run_session.assert_not_called()
            self.assertEqual(inputs["source_dtb"].read_bytes(), original_source)

    def test_registered_milestones_reject_overrides_before_artifact_access(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("sifive-u-asterinas-smoke")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            materialized = self._materialize_run_inputs(directory)
            inputs = {
                name: materialized[name]
                for name in ("uboot", "boot_disk", "manifest", "dtb_audit")
            }
            with mock.patch.object(
                qemu_uboot_booti,
                "load_artifact_manifest",
                side_effect=AssertionError("manifest was accessed"),
            ) as load_manifest:
                with self.assertRaisesRegex(ValueError, "registered-milestone"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker.txt",
                        result_path=directory / "result.json",
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        bootargs_override="console=ttyS0",
                    )
                load_manifest.assert_not_called()

    def test_run_rejects_complete_input_and_output_overlap_matrix(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        output_names = (
            "serial_log",
            "marker_event",
            "result_path",
            "progress_log",
        )

        def invoke(
            inputs: dict[str, Path],
            outputs: dict[str, Path],
        ) -> None:
            qemu_uboot_booti.run_prepared(
                **inputs,
                **outputs,
                startup_timeout=1.0,
                command_timeout=1.0,
                boot_timeout=1.0,
                termination_grace=1.0,
                profile=profile,
                scenario=scenario,
                variant=FIRST_PROCESS_CONSOLE_LOSS,
            )

        with mock.patch.object(
            qemu_uboot_booti,
            "load_artifact_manifest",
            side_effect=AssertionError("manifest was accessed"),
        ) as load_manifest:
            for input_name in (
                "uboot",
                "boot_disk",
                "manifest",
                "dtb_audit",
                "source_dtb",
                "variant_audit",
            ):
                for output_name in output_names:
                    for alias_kind in ("path", "symlink", "hardlink"):
                        label = (input_name, output_name, alias_kind)
                        with (
                            self.subTest(input_output=label),
                            tempfile.TemporaryDirectory() as tmp,
                        ):
                            directory = Path(tmp)
                            inputs = self._materialize_run_inputs(directory)
                            originals = {
                                name: path.read_bytes() for name, path in inputs.items()
                            }
                            outputs = {
                                "serial_log": directory / "serial.log",
                                "marker_event": directory / "marker-event.txt",
                                "result_path": directory / "result.json",
                                "progress_log": directory / "progress.log",
                            }
                            if alias_kind == "path":
                                outputs[output_name] = inputs[input_name]
                            elif alias_kind == "symlink":
                                outputs[output_name].symlink_to(inputs[input_name])
                            else:
                                os.link(inputs[input_name], outputs[output_name])

                            with self.assertRaisesRegex(
                                (ValueError, RuntimeError),
                                "overlap|symbolic link",
                            ):
                                invoke(inputs, outputs)
                            self.assertEqual(
                                {
                                    name: path.read_bytes()
                                    for name, path in inputs.items()
                                },
                                originals,
                            )

            for left_index, left_name in enumerate(output_names):
                for right_name in output_names[left_index + 1 :]:
                    for alias_kind in ("path", "symlink", "hardlink"):
                        label = (left_name, right_name, alias_kind)
                        with (
                            self.subTest(output_output=label),
                            tempfile.TemporaryDirectory() as tmp,
                        ):
                            directory = Path(tmp)
                            inputs = self._materialize_run_inputs(directory)
                            originals = {
                                name: path.read_bytes() for name, path in inputs.items()
                            }
                            outputs = {
                                "serial_log": directory / "serial.log",
                                "marker_event": directory / "marker-event.txt",
                                "result_path": directory / "result.json",
                                "progress_log": directory / "progress.log",
                            }
                            if alias_kind == "path":
                                outputs[right_name] = outputs[left_name]
                            else:
                                outputs[left_name].write_bytes(b"stale output\n")
                                if alias_kind == "symlink":
                                    outputs[right_name].symlink_to(outputs[left_name])
                                else:
                                    os.link(outputs[left_name], outputs[right_name])

                            with self.assertRaisesRegex(
                                (ValueError, RuntimeError),
                                "overlap|symbolic link",
                            ):
                                invoke(inputs, outputs)
                            self.assertEqual(
                                {
                                    name: path.read_bytes()
                                    for name, path in inputs.items()
                                },
                                originals,
                            )

        load_manifest.assert_not_called()

    def test_run_stages_every_input_and_launches_only_staged_materials(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        artifacts = self._run_artifacts()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            originals = {name: path.read_bytes() for name, path in inputs.items()}
            staged: dict[str, Path] = {}

            def load_manifest(path: Path) -> object:
                self.assertNotEqual(path, inputs["manifest"])
                self.assertEqual(path.read_bytes(), originals["manifest"])
                staged["manifest"] = path
                return artifacts

            def verify_dtb(**kwargs: object) -> object:
                bindings = {
                    "boot_disk": "boot_disk",
                    "audit_path": "dtb_audit",
                    "source_dtb": "source_dtb",
                    "variant_audit_path": "variant_audit",
                }
                for argument, name in bindings.items():
                    path = kwargs[argument]
                    self.assertIsInstance(path, Path)
                    assert isinstance(path, Path)
                    self.assertNotEqual(path, inputs[name])
                    self.assertEqual(path.read_bytes(), originals[name])
                    staged[name] = path
                return mock.Mock(sha256="a" * 64)

            real_qemu_argv = qemu_uboot_booti.qemu_argv

            def build_argv(**kwargs: object) -> list[str]:
                for name in ("uboot", "boot_disk"):
                    path = kwargs[name]
                    self.assertIsInstance(path, Path)
                    assert isinstance(path, Path)
                    self.assertNotEqual(path, inputs[name])
                    self.assertEqual(path.read_bytes(), originals[name])
                    staged[name] = path
                for name, path in inputs.items():
                    held = directory / f".{name}.held"
                    os.replace(path, held)
                    path.write_bytes(f"attacker {name}\n".encode())
                    os.replace(held, path)
                return real_qemu_argv(**kwargs)

            def run_session(argv: list[str], **kwargs: object) -> object:
                kernel = Path(argv[argv.index("-kernel") + 1])
                drive = next(item for item in argv if "file=" in item)
                disk = Path(
                    next(
                        field[5:]
                        for field in drive.split(",")
                        if field.startswith("file=")
                    )
                )
                self.assertEqual(kernel, staged["uboot"])
                self.assertEqual(disk, staged["boot_disk"])
                self.assertEqual(kernel.read_bytes(), originals["uboot"])
                self.assertEqual(disk.read_bytes(), originals["boot_disk"])
                raw_log = kwargs["raw_log_file"]
                raw_log.write(b"retained serial transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    side_effect=load_manifest,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    side_effect=verify_dtb,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    side_effect=build_argv,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ) as audit_log,
            ):
                result = qemu_uboot_booti.run_prepared(
                    **inputs,
                    serial_log=directory / "serial.log",
                    marker_event=directory / "marker-event.txt",
                    result_path=directory / "result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    variant=FIRST_PROCESS_CONSOLE_LOSS,
                )

            self.assertTrue(result.passed)
            self.assertEqual(
                audit_log.call_args.args[0],
                "retained serial transcript\n",
            )
            self.assertEqual(
                {name: path.read_bytes() for name, path in inputs.items()},
                originals,
            )

    def test_replaced_serial_or_marker_cannot_change_audit_or_be_deleted(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        transcript = b"retained serial transcript\n"
        replacement = b"attacker replacement\n"

        for target_name in ("serial_log", "marker_event"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                originals = {name: path.read_bytes() for name, path in inputs.items()}
                outputs = {
                    "serial_log": directory / "serial.log",
                    "marker_event": directory / "marker-event.txt",
                    "result_path": directory / "result.json",
                }

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(transcript)
                    return self._successful_session()

                def replace_during_audit(serial_text: str, **kwargs: object) -> object:
                    self.assertEqual(serial_text, transcript.decode())
                    self.assertIn("marker_seen=yes", kwargs["marker_event"])
                    target = outputs[target_name]
                    target.unlink()
                    target.write_bytes(replacement)
                    return self._passing_audit(profile, scenario)

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        side_effect=replace_during_audit,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "output entry changed"):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            **outputs,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertEqual(outputs[target_name].read_bytes(), replacement)
                self.assertFalse(outputs["result_path"].exists())
                self.assertEqual(
                    {name: path.read_bytes() for name, path in inputs.items()},
                    originals,
                )

    def test_each_staged_input_mutation_prevents_result_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for mutation in (
            "uboot",
            "boot_disk",
            "manifest",
            "dtb_audit",
            "source_dtb",
            "variant_audit",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                originals = {name: path.read_bytes() for name, path in inputs.items()}
                staged: dict[str, Path] = {}
                result_path = directory / "result.json"

                def load_manifest(path: Path) -> object:
                    staged["manifest"] = path
                    return self._run_artifacts()

                def verify_dtb(**kwargs: object) -> object:
                    staged["boot_disk"] = kwargs["boot_disk"]
                    staged["dtb_audit"] = kwargs["audit_path"]
                    staged["source_dtb"] = kwargs["source_dtb"]
                    staged["variant_audit"] = kwargs["variant_audit_path"]
                    return mock.Mock(sha256="a" * 64)

                def build_argv(**kwargs: object) -> list[str]:
                    staged["uboot"] = kwargs["uboot"]
                    return ["qemu-system-riscv64"]

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                def mutate_during_audit(*_args: object, **_kwargs: object) -> object:
                    staged[mutation].write_bytes(b"mutated staged input\n")
                    return self._passing_audit(profile, scenario)

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        side_effect=load_manifest,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        side_effect=verify_dtb,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        side_effect=build_argv,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        side_effect=mutate_during_audit,
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            serial_log=directory / "serial.log",
                            marker_event=directory / "marker-event.txt",
                            result_path=result_path,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertFalse(result_path.exists())
                self.assertEqual(
                    {name: path.read_bytes() for name, path in inputs.items()},
                    originals,
                )

    def test_staging_cleanup_failure_precedes_result_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            result_path = directory / "result.json"
            real_cleanup = EXECUTION_IO_MODULE.tempfile.TemporaryDirectory.cleanup
            cleanup_attempts = 0

            def fail_first_cleanup(temporary: object) -> None:
                nonlocal cleanup_attempts
                cleanup_attempts += 1
                if cleanup_attempts == 1:
                    real_cleanup(temporary)
                    raise OSError("injected staging cleanup failure")
                real_cleanup(temporary)

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.tempfile.TemporaryDirectory,
                    "cleanup",
                    autospec=True,
                    side_effect=fail_first_cleanup,
                ),
            ):
                with self.assertRaisesRegex(OSError, "staging cleanup failure"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertGreaterEqual(cleanup_attempts, 1)
            self.assertFalse(result_path.exists())

    def test_post_replace_result_fsync_failure_revokes_owned_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for should_replace_result in (False, True):
            with (
                self.subTest(should_replace_result=should_replace_result),
                tempfile.TemporaryDirectory() as tmp,
            ):
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                result_path = directory / "result.json"
                replacement = b"attacker-owned result replacement\n"
                parent_metadata = result_path.parent.stat()
                parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)
                descriptor_count = len(os.listdir("/proc/self/fd"))
                real_fsync = os.fsync
                fsync_failed = False

                def fail_result_parent_fsync(fd: int) -> None:
                    nonlocal fsync_failed
                    metadata = os.fstat(fd)
                    identity = (metadata.st_dev, metadata.st_ino)
                    if (
                        not fsync_failed
                        and result_path.exists()
                        and identity == parent_identity
                    ):
                        fsync_failed = True
                        if should_replace_result:
                            result_path.unlink()
                            result_path.write_bytes(replacement)
                        raise OSError("injected post-replace result fsync failure")
                    real_fsync(fd)

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        return_value=self._passing_audit(profile, scenario),
                    ),
                    mock.patch.object(
                        EXECUTION_IO_MODULE.os,
                        "fsync",
                        side_effect=fail_result_parent_fsync,
                    ),
                ):
                    with self.assertRaisesRegex(
                        OSError, "post-replace result fsync failure"
                    ):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            serial_log=directory / "serial.log",
                            marker_event=directory / "marker-event.txt",
                            result_path=result_path,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertTrue(fsync_failed)
                self.assertEqual(len(os.listdir("/proc/self/fd")), descriptor_count)
                if should_replace_result:
                    self.assertEqual(result_path.read_bytes(), replacement)
                else:
                    revoked = json.loads(result_path.read_text())
                    self.assertFalse(revoked["passed"])
                    self.assertEqual(revoked["status"], "ERROR")
                    self.assertEqual(
                        revoked["terminal_classification"], "INCOMPLETE"
                    )

    def test_post_replace_failure_revokes_preowned_result_publication(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for error_type in (KeyboardInterrupt, OSError):
            for should_replace_result in (False, True):
                with (
                    self.subTest(
                        error_type=error_type.__name__,
                        should_replace_result=should_replace_result,
                    ),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    directory = Path(tmp)
                    inputs = self._materialize_run_inputs(directory)
                    result_path = directory / "result.json"
                    replacement = b"attacker-owned result replacement\n"
                    descriptor_count = len(os.listdir("/proc/self/fd"))
                    real_replace = os.replace
                    result_replaced = False

                    def fail_after_result_replace(
                        source: object,
                        destination: object,
                        **kwargs: object,
                    ) -> None:
                        nonlocal result_replaced
                        real_replace(source, destination, **kwargs)
                        if destination == result_path.name:
                            result_replaced = True
                            if should_replace_result:
                                result_path.unlink()
                                result_path.write_bytes(replacement)
                            raise error_type("injected failure after replacing result")

                    def run_session(_argv: list[str], **kwargs: object) -> object:
                        kwargs["raw_log_file"].write(b"retained transcript\n")
                        return self._successful_session()

                    with (
                        mock.patch.object(
                            qemu_uboot_booti,
                            "load_artifact_manifest",
                            return_value=self._run_artifacts(),
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "verify_prepared_dtb",
                            return_value=mock.Mock(sha256="a" * 64),
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "qemu_argv",
                            return_value=["qemu-system-riscv64"],
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "qemu_version",
                            return_value="QEMU emulator version test",
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "run_serial_session",
                            side_effect=run_session,
                        ),
                        mock.patch.object(
                            qemu_uboot_booti,
                            "audit_serial_log",
                            return_value=self._passing_audit(profile, scenario),
                        ),
                        mock.patch.object(
                            EXECUTION_IO_MODULE.os,
                            "replace",
                            side_effect=fail_after_result_replace,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            error_type, "failure after replacing result"
                        ):
                            qemu_uboot_booti.run_prepared(
                                **inputs,
                                serial_log=directory / "serial.log",
                                marker_event=directory / "marker-event.txt",
                                result_path=result_path,
                                startup_timeout=1.0,
                                command_timeout=1.0,
                                boot_timeout=1.0,
                                termination_grace=1.0,
                                profile=profile,
                                scenario=scenario,
                                variant=FIRST_PROCESS_CONSOLE_LOSS,
                            )

                    self.assertTrue(result_replaced)
                    self.assertEqual(len(os.listdir("/proc/self/fd")), descriptor_count)
                    if should_replace_result:
                        self.assertEqual(result_path.read_bytes(), replacement)
                    else:
                        self.assertFalse(json.loads(result_path.read_text())["passed"])

    def test_result_publication_cannot_hide_retained_evidence_mutation(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for target_name in (
            "serial_log",
            "marker_event",
            "uboot",
            "boot_disk",
            "manifest",
            "dtb_audit",
            "source_dtb",
            "variant_audit",
        ):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                outputs = {
                    "serial_log": directory / "serial.log",
                    "marker_event": directory / "marker-event.txt",
                    "result_path": directory / "result.json",
                }
                replacement = f"attacker {target_name} replacement\n".encode()
                real_publish_result = (
                    EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
                )

                def replace_evidence(
                    workspace: object,
                    prepared: object,
                    payload: bytes,
                ) -> None:
                    real_publish_result(workspace, prepared, payload)
                    target = (outputs | inputs)[target_name]
                    if target_name in outputs:
                        target.unlink()
                    target.write_bytes(replacement)

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        return_value=self._passing_audit(profile, scenario),
                    ),
                    mock.patch.object(
                        EXECUTION_IO_MODULE.ExecutionWorkspace,
                        "publish_result",
                        autospec=True,
                        side_effect=replace_evidence,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            **outputs,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertEqual(
                    (outputs | inputs)[target_name].read_bytes(), replacement
                )
                self.assertFalse(
                    json.loads(outputs["result_path"].read_text())["passed"]
                )

    def test_framebuffer_result_rechecks_retained_display_evidence(self) -> None:
        payload = b"P6\n1280 1024\n255\n" + b"\x01\x02\x03" * (1280 * 1024)
        audit = PpmAudit(1280, 1024, 255, 1280 * 1024, 3, (0, 0, 1279, 1023), True)
        for target_name in ("screenshot", "display_audit"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                outputs = {"serial_log": directory / "serial", "marker_event": directory / "marker", "result_path": directory / "result", "screenshot": directory / "shot", "display_audit": directory / "audit"}
                real_publish = EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
                def replace(workspace: object, prepared: object, payload: bytes) -> None:
                    real_publish(workspace, prepared, payload)
                    target = outputs[target_name]
                    target.unlink()
                    target.write_bytes(b"attacker replacement\n")
                def session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["terminal_action"]()
                    return self._successful_session()
                with (
                    mock.patch.object(qemu_uboot_booti, "load_artifact_manifest", return_value=self._run_artifacts()),
                    mock.patch.object(qemu_uboot_booti, "verify_prepared_dtb"),
                    mock.patch.object(qemu_uboot_booti, "qemu_argv", return_value=["qemu-system-riscv64"]),
                    mock.patch.object(qemu_uboot_booti, "qemu_version", return_value="qemu test"),
                    mock.patch.object(qemu_uboot_booti, "run_serial_session", side_effect=session),
                    mock.patch.object(qemu_uboot_booti, "audit_serial_log", return_value=self._passing_audit(qemu_uboot_booti.GENERIC_SV39, qemu_uboot_booti.BootScenario.POSITIVE)),
                    mock.patch.object(qemu_uboot_booti, "capture_screendump", return_value=payload),
                    mock.patch.object(qemu_uboot_booti, "audit_ppm", return_value=audit),
                    mock.patch.object(EXECUTION_IO_MODULE.ExecutionWorkspace, "publish_result", autospec=True, side_effect=replace),
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            uboot=inputs["uboot"], boot_disk=inputs["boot_disk"], manifest=inputs["manifest"],
                            **outputs, device_set=MEGREZ_BASIC, startup_timeout=1, command_timeout=1, boot_timeout=1, termination_grace=1,
                        )
                self.assertEqual(outputs[target_name].read_bytes(), b"attacker replacement\n")
                self.assertFalse(json.loads(outputs["result_path"].read_text())["passed"])

    def test_post_result_resource_cleanup_failure_revokes_pass_result(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            result_path = directory / "result.json"
            real_close = EXECUTION_IO_MODULE.PinnedRegularInput.close
            cleanup_failed = False

            def fail_input_cleanup(input_pin: object) -> None:
                nonlocal cleanup_failed
                real_close(input_pin)
                if input_pin.label == "uboot" and not cleanup_failed:
                    cleanup_failed = True
                    raise OSError("injected retained-input cleanup failure")

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.PinnedRegularInput,
                    "close",
                    autospec=True,
                    side_effect=fail_input_cleanup,
                ),
            ):
                with self.assertRaisesRegex(OSError, "retained-input cleanup failure"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertTrue(cleanup_failed)
            self.assertFalse(json.loads(result_path.read_text())["passed"])

    def test_result_publication_close_failure_revokes_pass_result(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            result_path = directory / "result.json"
            real_publish_result = EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
            real_close = EXECUTION_IO_MODULE.PinnedPublication.close
            result_identity: tuple[int, int] | None = None
            close_failed = False

            def track_result_publication(
                workspace: object,
                prepared: object,
                payload: bytes,
            ) -> None:
                nonlocal result_identity
                result_identity = prepared.identity
                real_publish_result(workspace, prepared, payload)

            def fail_result_close(publication: object) -> None:
                nonlocal close_failed
                real_close(publication)
                if publication.identity == result_identity and not close_failed:
                    close_failed = True
                    raise OSError("injected result publication close failure")

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.ExecutionWorkspace,
                    "publish_result",
                    autospec=True,
                    side_effect=track_result_publication,
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.PinnedPublication,
                    "close",
                    autospec=True,
                    side_effect=fail_result_close,
                ),
            ):
                with self.assertRaisesRegex(OSError, "publication close failure"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertTrue(close_failed)
            self.assertFalse(json.loads(result_path.read_text())["passed"])

    def test_result_publication_rechecks_every_output_directory(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for target_name in ("serial_log", "marker_event", "result_path"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                input_directory = root / "inputs"
                input_directory.mkdir()
                inputs = self._materialize_run_inputs(input_directory)
                outputs = {
                    "serial_log": root / "serial-output" / "serial.log",
                    "marker_event": root / "marker-output" / "marker-event.txt",
                    "result_path": root / "result-output" / "result.json",
                }
                for output in outputs.values():
                    output.parent.mkdir()
                detached_directory = root / f"detached-{target_name}"
                replacement = f"attacker {target_name} replacement\n".encode()
                real_publish_result = (
                    EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result
                )

                def replace_output_directory(
                    workspace: object,
                    prepared: object,
                    payload: bytes,
                ) -> None:
                    real_publish_result(workspace, prepared, payload)
                    target = outputs[target_name]
                    target.parent.rename(detached_directory)
                    target.parent.mkdir()
                    target.write_bytes(replacement)

                def run_session(_argv: list[str], **kwargs: object) -> object:
                    kwargs["raw_log_file"].write(b"retained transcript\n")
                    return self._successful_session()

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        return_value=mock.Mock(sha256="a" * 64),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_argv",
                        return_value=["qemu-system-riscv64"],
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=run_session,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "audit_serial_log",
                        return_value=self._passing_audit(profile, scenario),
                    ),
                    mock.patch.object(
                        EXECUTION_IO_MODULE.ExecutionWorkspace,
                        "publish_result",
                        autospec=True,
                        side_effect=replace_output_directory,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "output directory path changed"
                    ):
                        qemu_uboot_booti.run_prepared(
                            **inputs,
                            **outputs,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                        )

                self.assertEqual(outputs[target_name].read_bytes(), replacement)
                owned_result = (
                    detached_directory / outputs["result_path"].name
                    if target_name == "result_path"
                    else outputs["result_path"]
                )
                self.assertFalse(json.loads(owned_result.read_text())["passed"])

    def test_replaced_result_publication_is_rejected_without_cleanup(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS
        replacement = b"attacker-owned result replacement\n"

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            originals = {name: path.read_bytes() for name, path in inputs.items()}
            result_path = directory / "result.json"
            real_publish_result = EXECUTION_IO_MODULE.ExecutionWorkspace.publish_result

            def replace_result(workspace: object, prepared: object, payload: bytes) -> None:
                real_publish_result(workspace, prepared, payload)
                result_path.unlink()
                result_path.write_bytes(replacement)

            def run_session(_argv: list[str], **kwargs: object) -> object:
                kwargs["raw_log_file"].write(b"retained transcript\n")
                return self._successful_session()

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=self._run_artifacts(),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="a" * 64),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=run_session,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ),
                mock.patch.object(
                    EXECUTION_IO_MODULE.ExecutionWorkspace,
                    "publish_result",
                    autospec=True,
                    side_effect=replace_result,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "output entry changed"):
                    qemu_uboot_booti.run_prepared(
                        **inputs,
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker-event.txt",
                        result_path=result_path,
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=scenario,
                        variant=FIRST_PROCESS_CONSOLE_LOSS,
                    )

            self.assertEqual(result_path.read_bytes(), replacement)
            self.assertEqual(
                {name: path.read_bytes() for name, path in inputs.items()},
                originals,
            )

    def test_console_loss_run_rejects_source_payload_or_disk_mutation(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS

        for mutation in ("source", "payload", "disk"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                inputs = self._materialize_run_inputs(directory)
                boot_disk = inputs["boot_disk"]
                source_dtb = inputs["source_dtb"]
                serial_log = directory / "serial.log"
                result_path = directory / "result.json"
                serial_log.write_text("captured transcript\n")
                staged: dict[str, Path] = {}
                verify_count = 0

                def mutate_after_session(*_args: object, **_kwargs: object) -> object:
                    if mutation == "source":
                        staged["source"].write_bytes(b"source DTB after")
                    if mutation == "disk":
                        staged["disk"].write_bytes(b"boot disk after")
                    return self._successful_session()

                def verify_dtb(**kwargs: object) -> object:
                    nonlocal verify_count
                    staged["source"] = kwargs["source_dtb"]
                    staged["disk"] = kwargs["boot_disk"]
                    digest = "b" if mutation == "payload" and verify_count else "a"
                    verify_count += 1
                    return mock.Mock(sha256=digest * 64)

                with (
                    mock.patch.object(
                        qemu_uboot_booti,
                        "load_artifact_manifest",
                        return_value=self._run_artifacts(),
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "verify_prepared_dtb",
                        side_effect=verify_dtb,
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "qemu_version",
                        return_value="QEMU emulator version test",
                    ),
                    mock.patch.object(
                        qemu_uboot_booti,
                        "run_serial_session",
                        side_effect=mutate_after_session,
                    ),
                    mock.patch.object(qemu_uboot_booti, "audit_serial_log"),
                ):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        qemu_uboot_booti.run_prepared(
                            uboot=inputs["uboot"],
                            boot_disk=boot_disk,
                            manifest=inputs["manifest"],
                            serial_log=serial_log,
                            marker_event=directory / "marker-event.txt",
                            result_path=result_path,
                            startup_timeout=1.0,
                            command_timeout=1.0,
                            boot_timeout=1.0,
                            termination_grace=1.0,
                            profile=profile,
                            scenario=scenario,
                            dtb_audit=inputs["dtb_audit"],
                            variant=FIRST_PROCESS_CONSOLE_LOSS,
                            source_dtb=source_dtb,
                            variant_audit=inputs["variant_audit"],
                        )
                self.assertFalse(result_path.exists())

    def test_console_loss_argv_keeps_firmware_machine_dtb_implicit(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        argv = qemu_uboot_booti.qemu_argv(
            uboot=Path("/tmp/u-boot"),
            boot_disk=Path("/tmp/boot.ext4"),
            profile=profile,
            snapshot_disk=True,
        )

        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertNotIn("-dtb", argv)
        self.assertFalse(any("dtb=" in item or "dumpdtb=" in item for item in argv))

        commands = qemu_uboot_booti.boot_commands(
            self._run_artifacts(),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
            variant=FIRST_PROCESS_CONSOLE_LOSS,
        )
        payload_path_commands = [
            command.text for command in commands if "/qemu-virt.dtb" in command.text
        ]
        self.assertEqual(
            payload_path_commands,
            ["ext4load virtio 0:0 0x88000000 /qemu-virt.dtb"],
        )
        booti_commands = [
            command.text for command in commands if command.text.startswith("booti ")
        ]
        self.assertEqual(
            booti_commands,
            ["booti 0x80200000 0x83000000:${initrd_size} 0x88000000"],
        )

    def test_suppression_probe_uses_standard_dtb_and_normal_completion(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenario = qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION
        artifacts = self._run_artifacts()
        payload_sha256 = "c" * 64
        expected_bootargs = effective_bootargs(profile, FIRST_PROCESS_CONSOLE_LOSS)
        serial_interaction = SESSION_MODULE.SerialInteraction(
            ready_line=RX_READY_LINE,
            input_steps=(
                SESSION_MODULE.SerialInputStep(input_bytes=RX_INPUT_BYTES),
            ),
            completion_line=RX_ACK_LINE,
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            boot_disk = inputs["boot_disk"]
            serial_log = directory / "serial.log"
            serial_log.write_text("captured transcript\n")
            disk_sha256 = hashlib.sha256(boot_disk.read_bytes()).hexdigest()
            real_qemu_argv = qemu_uboot_booti.qemu_argv

            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256=payload_sha256),
                ) as verify_dtb,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    side_effect=real_qemu_argv,
                ) as build_argv,
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    return_value=self._successful_session(),
                ) as run_session,
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=self._passing_audit(profile, scenario),
                ) as audit_log,
            ):
                result = qemu_uboot_booti.run_prepared(
                    uboot=inputs["uboot"],
                    boot_disk=boot_disk,
                    manifest=inputs["manifest"],
                    serial_log=serial_log,
                    marker_event=directory / "marker-event.txt",
                    result_path=directory / "result.json",
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                    scenario=scenario,
                    dtb_audit=inputs["dtb_audit"],
                    serial_interaction=serial_interaction,
                )

            self.assertTrue(result.passed)
            self.assertIsNone(result.variant)
            self.assertEqual(result.effective_bootargs, expected_bootargs)
            self.assertIsNone(result.source_dtb_sha256_before)
            self.assertIsNone(result.source_dtb_sha256_after)
            self.assertEqual(
                (result.payload_dtb_sha256_before, result.payload_dtb_sha256_after),
                (payload_sha256, payload_sha256),
            )
            self.assertEqual(
                (result.boot_disk_sha256_before, result.boot_disk_sha256_after),
                (disk_sha256, disk_sha256),
            )
            build_call = build_argv.call_args.kwargs
            self.assertNotEqual(build_call["uboot"], inputs["uboot"])
            self.assertNotEqual(build_call["boot_disk"], inputs["boot_disk"])
            self.assertEqual(build_call["uboot"].name, "u-boot")
            self.assertEqual(build_call["boot_disk"].name, "boot.ext4")
            self.assertIs(build_call["profile"], profile)
            self.assertFalse(build_call["snapshot_disk"])
            self.assertEqual(
                run_session.call_args.kwargs["completion_line"],
                SESSION_MODULE.USERSPACE_MARKER,
            )
            self.assertIs(
                run_session.call_args.kwargs["serial_interaction"],
                serial_interaction,
            )
            command_text = "\n".join(
                command.text for command in run_session.call_args.kwargs["commands"]
            )
            self.assertIn(f'setenv bootargs "{expected_bootargs}"', command_text)
            self.assertNotIn("snps,dw-apb-uart", command_text)
            for call in verify_dtb.call_args_list:
                self.assertIsNone(call.kwargs["variant"])
                self.assertIsNone(call.kwargs["variant_audit_path"])
                self.assertIsNone(call.kwargs["source_dtb"])
            self.assertIsNone(audit_log.call_args.kwargs["variant"])
            self.assertIs(audit_log.call_args.kwargs["scenario"], scenario)
            self.assertEqual(
                audit_log.call_args.kwargs["userspace_marker"],
                serial_interaction.completion_line.decode(),
            )
            self.assertEqual(
                audit_log.call_args.kwargs["readiness_marker"],
                serial_interaction.ready_line.decode(),
            )

    def test_positive_stale_and_suppression_reject_variant_materials(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        scenarios = (
            qemu_uboot_booti.BootScenario.POSITIVE,
            qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
            qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION,
        )
        paths = {
            "source_dtb": Path("/unreadable/source.dtb"),
            "variant_audit": Path("/unreadable/variant-audit.json"),
        }
        material_sets = (
            {"variant": FIRST_PROCESS_CONSOLE_LOSS},
            {"source_dtb": paths["source_dtb"]},
            {"variant_audit": paths["variant_audit"]},
            {"variant": FIRST_PROCESS_CONSOLE_LOSS, **paths},
        )
        base_run = {
            "uboot": Path("/unreadable/u-boot"),
            "boot_disk": Path("/unreadable/boot.ext4"),
            "manifest": Path("/unreadable/manifest.json"),
            "serial_log": Path("/unreadable/serial.log"),
            "marker_event": Path("/unreadable/marker-event.txt"),
            "result_path": Path("/unreadable/result.json"),
            "startup_timeout": 1.0,
            "command_timeout": 1.0,
            "boot_timeout": 1.0,
            "termination_grace": 1.0,
            "profile": profile,
            "dtb_audit": Path("/unreadable/dtb-audit.json"),
        }
        with mock.patch.object(
            qemu_uboot_booti,
            "load_artifact_manifest",
            side_effect=AssertionError("manifest was accessed"),
        ) as load_manifest:
            for scenario in scenarios:
                for materials in material_sets:
                    with self.subTest(scenario=scenario, materials=materials):
                        with self.assertRaises(ValueError):
                            qemu_uboot_booti.run_prepared(
                                **base_run,
                                scenario=scenario,
                                **materials,
                            )
            load_manifest.assert_not_called()

        cli_base = [
            "run",
            "--uboot",
            "/tmp/u-boot",
            "--boot-disk",
            "/tmp/boot.ext4",
            "--manifest",
            "/tmp/manifest.json",
            "--serial-log",
            "/tmp/serial.log",
            "--marker-event",
            "/tmp/marker-event.txt",
            "--result",
            "/tmp/result.json",
            "--dtb-audit",
            "/tmp/dtb-audit.json",
            "--profile",
            profile.name,
        ]
        material_cli = [
            "--variant",
            FIRST_PROCESS_CONSOLE_LOSS.name,
            "--source-dtb",
            "/tmp/source.dtb",
            "--variant-audit",
            "/tmp/variant-audit.json",
        ]
        accepted = qemu_uboot_booti._parse_args(
            cli_base
            + ["--scenario", qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS]
            + material_cli
        )
        self.assertIs(accepted.variant, FIRST_PROCESS_CONSOLE_LOSS)
        self.assertEqual(accepted.source_dtb, Path("/tmp/source.dtb"))
        self.assertEqual(
            accepted.variant_audit,
            Path("/tmp/variant-audit.json"),
        )
        for scenario in scenarios:
            with self.subTest(cli_scenario=scenario), self.assertRaises(SystemExit):
                qemu_uboot_booti._parse_args(
                    cli_base + ["--scenario", scenario.value] + material_cli
                )
        for missing_option in ("--variant", "--source-dtb", "--variant-audit"):
            missing_material = list(material_cli)
            option_index = missing_material.index(missing_option)
            del missing_material[option_index : option_index + 2]
            with self.subTest(missing=missing_option), self.assertRaises(SystemExit):
                qemu_uboot_booti._parse_args(
                    cli_base
                    + [
                        "--scenario",
                        qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS.value,
                    ]
                    + missing_material
                )

    def test_resolves_only_the_registered_console_loss_variant(self) -> None:
        variant = variant_by_name("first-process-console-loss")

        self.assertIs(variant, FIRST_PROCESS_CONSOLE_LOSS)
        self.assertEqual(variant.name, "first-process-console-loss")
        self.assertEqual(variant.base_profile_name, "megrez-sv48-svade-fast")
        self.assertEqual(
            variant.bootarg_suffix,
            "asterinas.first_process_diag=1",
        )
        self.assertEqual(variant.payload_uart_compatible, "snps,dw-apb-uart")
        self.assertEqual(variant.terminal_stage, "user_first_write_returned")
        self.assertEqual(
            variant.completion_line,
            b"ASTERINAS_FIRST_PROCESS_DIAG "
            b"stage=user_first_write_returned "
            b"fd=1 requested=50 result=50",
        )
        self.assertEqual(variant.expected_userspace_marker_count, 0)
        self.assertEqual(variant.classification, "EXPECTED_CONSOLE_ROUTE_LOSS")
        self.assertEqual(variant.transport, "U-Boot booti only")
        validate_registered_variant(variant)

        for unknown_name in (
            "",
            "console-loss",
            "first-process-console-loss-copy",
            "first-process-console-loss ",
        ):
            with self.subTest(unknown_name=unknown_name):
                with self.assertRaisesRegex(ValueError, "unknown QEMU U-Boot variant"):
                    variant_by_name(unknown_name)

        with self.assertRaises(FrozenInstanceError):
            variant.name = "modified"  # type: ignore[misc]

        base_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        self.assertEqual(effective_bootargs(base_profile, None), base_profile.bootargs)
        self.assertEqual(
            effective_bootargs(base_profile, variant),
            base_profile.bootargs + " asterinas.first_process_diag=1",
        )

        wrong_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")
        with self.assertRaisesRegex(ValueError, "requires base profile"):
            effective_bootargs(wrong_profile, variant)

    def test_rejects_modified_variant_identity(self) -> None:
        field_variants = (
            replace(FIRST_PROCESS_CONSOLE_LOSS, name="modified"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, base_profile_name="generic-sv39"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, bootarg_suffix="debug=1"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, payload_uart_compatible="ns16550a"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, terminal_stage="user_enter"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, completion_line=b"modified"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, expected_userspace_marker_count=1),
            replace(FIRST_PROCESS_CONSOLE_LOSS, classification="PASS"),
            replace(FIRST_PROCESS_CONSOLE_LOSS, transport="direct kernel boot"),
        )
        for field_variant in field_variants:
            with self.subTest(field_variant=field_variant):
                with self.assertRaisesRegex(ValueError, "registered singleton"):
                    validate_registered_variant(field_variant)

        equal_copy = QemuUbootVariant(
            name="first-process-console-loss",
            base_profile_name="megrez-sv48-svade-fast",
            bootarg_suffix="asterinas.first_process_diag=1",
            payload_uart_compatible="snps,dw-apb-uart",
            terminal_stage="user_first_write_returned",
            completion_line=(
                b"ASTERINAS_FIRST_PROCESS_DIAG "
                b"stage=user_first_write_returned "
                b"fd=1 requested=50 result=50"
            ),
            expected_userspace_marker_count=0,
            classification="EXPECTED_CONSOLE_ROUTE_LOSS",
            transport="U-Boot booti only",
        )
        self.assertEqual(equal_copy, FIRST_PROCESS_CONSOLE_LOSS)
        self.assertIsNot(equal_copy, FIRST_PROCESS_CONSOLE_LOSS)
        with self.assertRaisesRegex(ValueError, "registered singleton"):
            validate_registered_variant(equal_copy)

    def test_resolves_immutable_generic_and_fast_megrez_profiles(self) -> None:
        svade = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        svadu = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")

        self.assertEqual((svade.memory, svade.hart_count), ("2G", 4))
        self.assertIn("sv57=false", svade.cpu)
        self.assertIn("svade=true", svade.cpu)
        self.assertIn("svadu=false", svade.cpu)
        self.assertIn("svadu=true", svadu.cpu)
        self.assertIn("svade=false", svadu.cpu)
        self.assertNotIn("sv48=false", svade.cpu)
        self.assertIn("zkr=false", svade.cpu)
        self.assertIn("svpbmt=false", svade.cpu)
        self.assertEqual(
            svade.bootargs,
            "cpu_no_boost_1_6ghz loglevel=info init=/init",
        )
        self.assertEqual(
            qemu_uboot_booti.profile_by_name("generic-sv39"),
            qemu_uboot_booti.GENERIC_SV39,
        )

    def test_fast_megrez_profiles_match_the_board_contract(self) -> None:
        contract = load_contract()

        for name in (
            "megrez-sv48-svade-fast",
            "megrez-sv48-svadu-fast",
        ):
            with self.subTest(profile=name):
                qemu_uboot_booti.validate_profile_policy(
                    contract,
                    qemu_uboot_booti.profile_by_name(name),
                )

    def test_slow_profile_is_registered_and_matches_the_board_contract(self) -> None:
        slow = qemu_uboot_booti.profile_by_name("megrez-sv48-slow")

        self.assertEqual(slow.memory, "16G")
        self.assertEqual(slow.memory_bytes, 0x4_0000_0000)
        self.assertEqual(slow.hart_count, 4)
        self.assertEqual(slow.mmu_type, "riscv,sv48")
        self.assertEqual(slow.ad_extension, "svade")
        self.assertEqual(
            slow.bootargs,
            "cpu_no_boost_1_6ghz loglevel=info init=/init",
        )
        self.assertTrue(slow.requires_resource_gate)
        qemu_uboot_booti.validate_profile_policy(load_contract(), slow)

    def test_slow_qemu_argv_requires_a_valid_resource_permit(self) -> None:
        paths = {
            "uboot": Path("/tmp/u-boot"),
            "boot_disk": Path("/tmp/boot.ext4"),
        }
        slow = qemu_uboot_booti.profile_by_name("megrez-sv48-slow")

        invalid_permits = (
            None,
            True,
            object(),
        )
        for permit in invalid_permits:
            with self.subTest(permit=permit):
                kwargs = {} if permit is None else {"slow_permit": permit}
                with self.assertRaisesRegex(
                    ValueError,
                    "resource gate permit",
                ):
                    qemu_uboot_booti.qemu_argv(
                        **paths,
                        profile=slow,
                        **kwargs,
                    )

        permit = _issue_slow_run_permit()
        argv = qemu_uboot_booti.qemu_argv(
            **paths,
            profile=slow,
            slow_permit=permit,
        )
        self.assertEqual(argv[argv.index("-m") + 1], "16G")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")

    def test_slow_gate_cannot_be_bypassed_by_profile_drift(self) -> None:
        slow = qemu_uboot_booti.profile_by_name("megrez-sv48-slow")
        drifted = replace(
            slow,
            machine=replace(slow.machine, requires_resource_gate=False),
        )

        with self.assertRaisesRegex(ValueError, "registered value"):
            qemu_uboot_booti.qemu_argv(
                uboot=Path("/tmp/u-boot"),
                boot_disk=Path("/tmp/boot.ext4"),
                profile=drifted,
            )

    def test_megrez_qemu_argv_uses_four_harts_without_changing_the_default(
        self,
    ) -> None:
        paths = {
            "uboot": Path("/tmp/u-boot"),
            "boot_disk": Path("/tmp/boot.ext4"),
        }
        implicit = qemu_uboot_booti.qemu_argv(**paths)
        explicit = qemu_uboot_booti.qemu_argv(
            **paths,
            profile=qemu_uboot_booti.GENERIC_SV39,
        )
        svade = qemu_uboot_booti.qemu_argv(
            **paths,
            profile=qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast"),
        )

        self.assertEqual(implicit, explicit)
        self.assertEqual(
            implicit,
            [
                "qemu-system-riscv64",
                "-machine",
                "virt",
                "-cpu",
                "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
                "-m",
                "2G",
                "-smp",
                "1",
                "-display",
                "none",
                "-monitor",
                "none",
                "-serial",
                "stdio",
                "-no-reboot",
                "-kernel",
                "/tmp/u-boot",
                "-drive",
                "if=none,format=raw,file=/tmp/boot.ext4,id=bootdisk",
                "-device",
                "virtio-blk-device,drive=bootdisk",
            ],
        )
        self.assertEqual(svade[svade.index("-smp") + 1], "4")

    def test_snapshot_disk_preserves_guest_reboot_policy(self) -> None:
        paths = {
            "uboot": Path("/tmp/u-boot"),
            "boot_disk": Path("/tmp/boot.ext4"),
        }

        snapshot_only = qemu_uboot_booti.qemu_argv(
            **paths,
            snapshot_disk=True,
        )
        guest_reboot = qemu_uboot_booti.qemu_argv(
            **paths,
            guest_reboot=True,
        )

        self.assertIn("-no-reboot", snapshot_only)
        self.assertTrue(any("snapshot=on" in arg for arg in snapshot_only))
        self.assertNotIn("-no-reboot", guest_reboot)
        self.assertTrue(any("snapshot=on" in arg for arg in guest_reboot))

    def test_profile_policy_rejects_duplicate_or_drifting_cpu_selectors(self) -> None:
        contract = load_contract()
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        mutations = (
            replace(
                profile,
                machine=replace(profile.machine, cpu=f"{profile.cpu},svade=true"),
            ),
            replace(
                profile,
                machine=replace(profile.machine, cpu=f"{profile.cpu},svinval=true"),
            ),
            replace(
                profile,
                machine=replace(
                    profile.machine,
                    cpu=profile.cpu.replace("zkr=false", "zkr=true"),
                ),
            ),
            replace(profile, machine=replace(profile.machine, memory="1G")),
            replace(
                profile,
                validation=replace(profile.validation, bootargs="init=/wrong"),
            ),
        )

        for mutation in mutations:
            with self.subTest(profile=mutation):
                with self.assertRaises(ValueError):
                    qemu_uboot_booti.validate_profile_policy(contract, mutation)

    def test_megrez_cli_prints_profile_bootargs(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                "megrez-sv48-svade-fast",
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'setenv bootargs "cpu_no_boost_1_6ghz loglevel=info init=/init"',
            result.stdout,
        )
        self.assertNotIn("console=ttyS0", result.stdout)

    def test_megrez_command_scenarios_sanitize_rng_and_keep_stale_env_local(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        positive = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
        )
        stale = qemu_uboot_booti.boot_commands(
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )

        rng_guard = (
            "if fdt get value aster_rng_seed /chosen rng-seed; "
            "then fdt rm /chosen rng-seed; fi"
        )
        for commands in (positive, stale):
            texts = [command.text for command in commands]
            self.assertIn(rng_guard, texts)
            self.assertLess(texts.index(rng_guard), texts.index("fdt print /chosen"))
            self.assertEqual(sum(text.startswith("booti ") for text in texts), 1)
            self.assertNotIn("saveenv", texts)
        positive_texts = [command.text for command in positive]
        stale_texts = [command.text for command in stale]
        self.assertIn(f'setenv bootargs "{MEGREZ_BOOTARGS}"', positive_texts)
        self.assertIn('setenv bootargs "cpu_no_boost_1_6ghz"', stale_texts)
        self.assertNotIn(f'setenv bootargs "{MEGREZ_BOOTARGS}"', stale_texts)
        self.assertIn(
            f'fdt set /chosen bootargs "{MEGREZ_BOOTARGS}"',
            stale_texts,
        )
        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "print-commands",
                "--profile",
                profile.name,
                "--scenario",
                "stale-bootargs",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertIn('setenv bootargs "cpu_no_boost_1_6ghz"', cli.stdout)
        self.assertIn(f'fdt set /chosen bootargs "{MEGREZ_BOOTARGS}"', cli.stdout)

    def test_stale_run_requires_expected_timeout_and_complete_cleanup(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=11326096,
            kernel_crc32="57c40418",
            dtb_size=5048,
            dtb_crc32="6e7844b8",
            initrd_size=3411,
            initrd_crc32="153879f1",
        )
        expected = qemu_uboot_booti.SessionResult(
            marker_seen=False,
            booti_sent_count=1,
            timed_out=True,
            killed=False,
            cleanup_complete=True,
            returncode=0,
            failure="boot-timeout",
            termination_action="SIGTERM",
        )
        sessions = (
            expected,
            replace(expected, cleanup_complete=False),
            replace(expected, timed_out=False),
        )
        audit = qemu_uboot_booti.BootAudit(
            booti_command_count=1,
            userspace_marker_count=0,
            effective_bootargs=MEGREZ_BOOTARGS,
            diagnostic=qemu_uboot_audit.audit_diagnostic_markers(
                "",
                variant=None,
            ),
            application_processor_ids=(1, 2, 3),
            random_source="timestamp",
            classification="EXPECTED_INIT_ENOENT",
            passed=True,
            failures=(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            serial_log = directory / "serial.log"
            serial_log.write_text(MEGREZ_STALE_SERIAL_LOG)
            marker_event = directory / "marker.txt"
            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    return_value=mock.Mock(sha256="abc"),
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_argv",
                    return_value=["qemu-system-riscv64"],
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "qemu_version",
                    return_value="QEMU emulator version test",
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "run_serial_session",
                    side_effect=sessions,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "audit_serial_log",
                    return_value=audit,
                ) as audit_log,
            ):
                results = [
                    qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"],
                        boot_disk=inputs["boot_disk"],
                        manifest=inputs["manifest"],
                        serial_log=serial_log,
                        marker_event=marker_event,
                        result_path=directory / f"result-{index}.json",
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                        dtb_audit=inputs["dtb_audit"],
                    )
                    for index in range(len(sessions))
                ]

        self.assertEqual([result.passed for result in results], [True, False, False])
        self.assertEqual(results[0].scenario, "stale-bootargs")
        self.assertIn(
            "cleanup_complete=no", audit_log.call_args_list[1].kwargs["marker_event"]
        )
        for call in audit_log.call_args_list:
            self.assertEqual(
                call.kwargs["scenario"],
                qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
            )

    def test_run_rejects_profile_drift_before_manifest_or_qemu_access(self) -> None:
        base_profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        profile = replace(
            base_profile,
            validation=replace(
                base_profile.validation,
                bootargs="init=/wrong",
            ),
        )

        with mock.patch.object(qemu_uboot_booti, "qemu_version") as qemu_version:
            with self.assertRaisesRegex(ValueError, "registered value"):
                qemu_uboot_booti.run_prepared(
                    uboot=Path("/does/not/exist/u-boot"),
                    boot_disk=Path("/does/not/exist/boot.ext4"),
                    manifest=Path("/does/not/exist/manifest.json"),
                    serial_log=Path("/does/not/exist/serial.log"),
                    marker_event=Path("/does/not/exist/marker.txt"),
                    result_path=Path("/does/not/exist/result.json"),
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                )
            qemu_version.assert_not_called()

    def test_megrez_run_requires_a_profile_bound_dtb_audit_before_qemu(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")

        with mock.patch.object(qemu_uboot_booti, "qemu_version") as qemu_version:
            with self.assertRaisesRegex(ValueError, "generated DTB audit is required"):
                qemu_uboot_booti.run_prepared(
                    uboot=Path("/does/not/exist/u-boot"),
                    boot_disk=Path("/does/not/exist/boot.ext4"),
                    manifest=Path("/does/not/exist/manifest.json"),
                    serial_log=Path("/does/not/exist/serial.log"),
                    marker_event=Path("/does/not/exist/marker.txt"),
                    result_path=Path("/does/not/exist/result.json"),
                    startup_timeout=1.0,
                    command_timeout=1.0,
                    boot_timeout=1.0,
                    termination_grace=1.0,
                    profile=profile,
                )
            qemu_version.assert_not_called()

    def test_megrez_run_verifies_the_boot_disk_dtb_before_qemu(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=1,
            kernel_crc32="11111111",
            dtb_size=2,
            dtb_crc32="22222222",
            initrd_size=3,
            initrd_crc32="33333333",
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inputs = self._materialize_run_inputs(directory)
            with (
                mock.patch.object(
                    qemu_uboot_booti,
                    "load_artifact_manifest",
                    return_value=artifacts,
                ),
                mock.patch.object(
                    qemu_uboot_booti,
                    "verify_prepared_dtb",
                    side_effect=ValueError("forged DTB audit"),
                ) as verify_dtb,
                mock.patch.object(qemu_uboot_booti, "qemu_version") as qemu_version,
            ):
                with self.assertRaisesRegex(ValueError, "forged DTB audit"):
                    qemu_uboot_booti.run_prepared(
                        uboot=inputs["uboot"],
                        boot_disk=inputs["boot_disk"],
                        manifest=inputs["manifest"],
                        serial_log=directory / "serial.log",
                        marker_event=directory / "marker.txt",
                        result_path=directory / "result.json",
                        startup_timeout=1.0,
                        command_timeout=1.0,
                        boot_timeout=1.0,
                        termination_grace=1.0,
                        profile=profile,
                        dtb_audit=inputs["dtb_audit"],
                    )

            arguments = verify_dtb.call_args.kwargs
            self.assertNotEqual(arguments["boot_disk"], inputs["boot_disk"])
            self.assertNotEqual(arguments["audit_path"], inputs["dtb_audit"])
            self.assertEqual(arguments["boot_disk"].name, "boot.ext4")
            self.assertEqual(arguments["audit_path"].name, "qemu-dtb-audit.json")
            self.assertIs(arguments["profile"], profile)
            self.assertEqual(arguments["expected_size"], artifacts.dtb_size)
            self.assertEqual(arguments["expected_crc32"], artifacts.dtb_crc32)
            self.assertIsNone(arguments["variant"])
            self.assertIsNone(arguments["variant_audit_path"])
            self.assertIsNone(arguments["source_dtb"])
            qemu_version.assert_not_called()

    def test_cli_rejects_unknown_profile_before_querying_qemu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            queried = directory / "qemu-queried"
            fake_qemu = directory / "qemu-system-riscv64"
            fake_qemu.write_text(f"#!/bin/sh\ntouch {queried}\n")
            fake_qemu.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{directory}{os.pathsep}{env['PATH']}"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "run",
                    "--profile",
                    "bad",
                    "--uboot",
                    str(directory / "u-boot"),
                    "--boot-disk",
                    str(directory / "boot.ext4"),
                    "--manifest",
                    str(directory / "manifest.json"),
                    "--serial-log",
                    str(directory / "serial.log"),
                    "--marker-event",
                    str(directory / "marker.txt"),
                    "--result",
                    str(directory / "result.json"),
                ],
                capture_output=True,
                env=env,
                text=True,
            )
            self.assertFalse(queried.exists())

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown QEMU U-Boot profile: bad", result.stderr)

    def test_print_commands_proves_bootargs_before_one_booti(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "print-commands"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        booti_lines = [line for line in lines if line.startswith("booti ")]
        self.assertEqual(
            booti_lines,
            ["booti 0x80200000 0x83000000:${initrd_size} 0x88000000"],
        )
        bootargs_commands = (
            'setenv bootargs "console=ttyS0 loglevel=info init=/init"',
            "printenv bootargs",
            'fdt set /chosen bootargs "console=ttyS0 loglevel=info init=/init"',
            "fdt print /chosen",
        )
        for command in bootargs_commands:
            self.assertIn(command, lines)
        command_indices = [lines.index(command) for command in bootargs_commands]
        self.assertEqual(command_indices, sorted(command_indices))
        self.assertLess(command_indices[-1], lines.index(booti_lines[0]))
        self.assertNotIn("saveenv", result.stdout)
        self.assertNotIn("ostd.log_level", result.stdout)

    def test_command_plan_uses_runtime_artifact_sizes_and_crcs(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "ArtifactExpectations"))
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x123,
            kernel_crc32="11111111",
            dtb_size=0x234,
            dtb_crc32="22222222",
            initrd_size=0x345,
            initrd_crc32="33333333",
        )

        commands = qemu_uboot_booti.boot_commands(artifacts)

        expected_outputs = {
            command.name: command.expected_output for command in commands
        }
        self.assertEqual(expected_outputs["kernel-load"], "291 bytes read")
        self.assertEqual(expected_outputs["kernel-size"], "123")
        self.assertEqual(expected_outputs["kernel-crc"], "11111111")
        self.assertEqual(expected_outputs["dtb-load"], "564 bytes read")
        self.assertEqual(expected_outputs["dtb-crc"], "22222222")
        self.assertEqual(expected_outputs["initrd-load"], "837 bytes read")
        self.assertEqual(expected_outputs["initrd-crc"], "33333333")

    def test_derives_artifact_expectations_from_file_contents(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "artifact_expectations_from_paths"))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            kernel_bytes = valid_linux_image()
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            artifacts = qemu_uboot_booti.artifact_expectations_from_paths(
                kernel=kernel,
                dtb=dtb,
                initrd=initrd,
            )

        self.assertEqual(artifacts.kernel_size, 64)
        self.assertEqual(artifacts.kernel_crc32, "4c567565")
        self.assertEqual(artifacts.dtb_size, 3)
        self.assertEqual(artifacts.dtb_crc32, "58700668")
        self.assertEqual(artifacts.initrd_size, 6)
        self.assertEqual(artifacts.initrd_crc32, "fa3bf02d")

    def test_write_manifest_records_host_derived_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            manifest = directory / "artifacts.json"
            kernel_bytes = valid_linux_image()
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "write-manifest",
                    "--kernel",
                    str(kernel),
                    "--dtb",
                    str(dtb),
                    "--initrd",
                    str(initrd),
                    "--output",
                    str(manifest),
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(manifest.read_text()),
                {
                    "dtb_crc32": "58700668",
                    "dtb_sha256": (
                        "e7ce15bb0946707f3145d94c16c6946c"
                        "ef9b92b157527c1a64c2c8903fdbe86f"
                    ),
                    "dtb_size": 3,
                    "initrd_crc32": "fa3bf02d",
                    "initrd_sha256": (
                        "09e6c018d2c8c4903308613dd1b72484"
                        "d57eadf12ec50ddc8f52e5accce470f2"
                    ),
                    "initrd_size": 6,
                    "kernel_crc32": "4c567565",
                    "kernel_sha256": (
                        "948af20de762380321d412ebdb8a2fc9"
                        "e44c21b1602ca8e5b5b3dd47d3746c89"
                    ),
                    "kernel_size": 64,
                },
            )

    def test_rejects_a_linux_image_whose_header_size_mismatches_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            kernel_bytes = valid_linux_image()
            # A declared size smaller than the on-disk file must be
            # rejected; a declared size larger than the file is valid
            # (Linux declares the uncompressed size including BSS).
            struct.pack_into("<Q", kernel_bytes, 0x10, 63)
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            with self.assertRaisesRegex(ValueError, "Linux Image size"):
                qemu_uboot_booti.artifact_expectations_from_paths(
                    kernel=kernel,
                    dtb=dtb,
                    initrd=initrd,
                )

    def test_rejects_a_linux_image_with_an_unsafe_text_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "kernel"
            dtb = directory / "dtb"
            initrd = directory / "initrd"
            kernel_bytes = valid_linux_image()
            struct.pack_into("<Q", kernel_bytes, 0x08, 0)
            kernel.write_bytes(kernel_bytes)
            dtb.write_bytes(b"dtb")
            initrd.write_bytes(b"initrd")

            with self.assertRaisesRegex(ValueError, "text offset"):
                qemu_uboot_booti.artifact_expectations_from_paths(
                    kernel=kernel,
                    dtb=dtb,
                    initrd=initrd,
                )

    def test_rejects_payload_ranges_that_overlap_before_qemu_starts(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "validate_fixed_payload_layout"))
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x32_00000,
            kernel_crc32="11111111",
            dtb_size=0x1000,
            dtb_crc32="22222222",
            initrd_size=0x1000,
            initrd_crc32="33333333",
        )

        with self.assertRaisesRegex(ValueError, "kernel overlaps initrd"):
            qemu_uboot_booti.validate_fixed_payload_layout(artifacts)

    def test_payload_ranges_reserve_aligned_maximum_dtb_resize_space(self) -> None:
        for dtb_size in (0x1001, 0x2000):
            with self.subTest(dtb_size=dtb_size):
                artifacts = qemu_uboot_booti.ArtifactExpectations(
                    kernel_size=0x1000,
                    kernel_crc32="11111111",
                    dtb_size=dtb_size,
                    dtb_crc32="22222222",
                    initrd_size=0x1000,
                    initrd_crc32="33333333",
                )

                self.assertEqual(
                    payload_ranges(artifacts)["dtb"],
                    range(0x8800_0000, 0x8800_4000),
                )

    def test_bdinfo_rejects_reserved_dtb_maximum_resize_space(self) -> None:
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x1000,
            kernel_crc32="11111111",
            dtb_size=0x1001,
            dtb_crc32="22222222",
            initrd_size=0x1000,
            initrd_crc32="33333333",
        )
        log = """\
memory[0] [0x80000000-0xffffffff]
reserved[0] [0x88003000-0x88003fff]
"""

        with self.assertRaisesRegex(ValueError, "dtb overlaps"):
            validate_bdinfo_memory_layout(log, artifacts)

    def test_rejects_reversed_bdinfo_ranges(self) -> None:
        artifacts = qemu_uboot_booti.ArtifactExpectations(
            kernel_size=0x1000,
            kernel_crc32="11111111",
            dtb_size=0x1000,
            dtb_crc32="22222222",
            initrd_size=0x1000,
            initrd_crc32="33333333",
        )
        log = """\
memory[0] [0x80000000-0xffffffff]
reserved[0] [0x90000000-0x80000000]
"""

        with self.assertRaisesRegex(ValueError, "reversed reserved range"):
            validate_bdinfo_memory_layout(log, artifacts)

    def test_qemu_version_rejects_empty_output(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch(
            "qemu_uboot_commands.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "empty version output"):
                qemu_uboot_booti.qemu_version()

    def test_initramfs_rejects_the_newc_trailer_name(self) -> None:
        entry = InitramfsEntry("TRAILER!!!", b"hidden", 0o100644)

        with self.assertRaisesRegex(ValueError, "reserved initramfs entry"):
            make_newc_archive(b"elf", extra_entries=(entry,))

    def test_load_manifest_restores_typed_artifact_gates(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "load_artifact_manifest"))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "artifacts.json"
            manifest.write_text(
                json.dumps(
                    {
                        "kernel_size": 1,
                        "kernel_crc32": "11111111",
                        "kernel_sha256": "1" * 64,
                        "dtb_size": 2,
                        "dtb_crc32": "22222222",
                        "dtb_sha256": "2" * 64,
                        "initrd_size": 3,
                        "initrd_crc32": "33333333",
                        "initrd_sha256": "3" * 64,
                    }
                )
            )

            artifacts = qemu_uboot_booti.load_artifact_manifest(manifest)

        self.assertEqual(
            artifacts,
            qemu_uboot_booti.ArtifactExpectations(
                kernel_size=1,
                kernel_crc32="11111111",
                dtb_size=2,
                dtb_crc32="22222222",
                initrd_size=3,
                initrd_crc32="33333333",
                kernel_sha256="1" * 64,
                dtb_sha256="2" * 64,
                initrd_sha256="3" * 64,
            ),
        )

    def test_qemu_command_models_sv39_svade_and_uses_only_virtio(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "qemu_argv"))

        argv = qemu_uboot_booti.qemu_argv(
            uboot=Path("/tmp/u-boot"),
            boot_disk=Path("/tmp/boot.ext4"),
        )

        self.assertIn(
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
            argv,
        )
        self.assertIn("/tmp/u-boot", argv)
        self.assertIn(
            "if=none,format=raw,file=/tmp/boot.ext4,id=bootdisk",
            argv,
        )
        self.assertNotIn("/dev/ttyUSB0", " ".join(argv))


class AuditSerialLogTests(unittest.TestCase):
    @staticmethod
    def successful_marker_event() -> str:
        return "marker_seen=yes\naction=SIGTERM\ncleanup_complete=yes\n"

    @staticmethod
    def diagnostic_bootargs() -> str:
        return f"{MEGREZ_BOOTARGS} {FIRST_PROCESS_CONSOLE_LOSS.bootarg_suffix}"

    @staticmethod
    def registered_profile():
        return qemu_uboot_booti.profile_by_name("sifive-u-asterinas-smoke")

    def registered_serial_log(self) -> str:
        return MEGREZ_POSITIVE_SERIAL_LOG.replace(
            MEGREZ_BOOTARGS,
            self.registered_profile().bootargs,
        )

    def audit_registered_serial(self, serial_log: str):
        return qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            profile=self.registered_profile(),
        )

    def assert_registered_failure(self, serial_log: str, failure: str) -> None:
        audit = self.audit_registered_serial(serial_log)
        self.assertFalse(audit.passed)
        self.assertIn(failure, audit.failures)

    def console_loss_serial_log(
        self,
        diagnostic_lines: tuple[str, ...] | list[str] = CONSOLE_LOSS_DIAGNOSTIC_LINES,
    ) -> str:
        diagnostic_output = "\n".join(diagnostic_lines)
        return MEGREZ_POSITIVE_SERIAL_LOG.replace(
            MEGREZ_BOOTARGS,
            self.diagnostic_bootargs(),
        ).replace(
            qemu_uboot_audit.USERSPACE_MARKER_TEXT,
            f"{PROCESS_STAGE_COMPLETE_LINE}\n{diagnostic_output}",
        )

    def suppression_serial_log(self) -> str:
        return MEGREZ_POSITIVE_SERIAL_LOG.replace(
            MEGREZ_BOOTARGS,
            self.diagnostic_bootargs(),
        ).replace(
            "OSTD initialized. Preparing components.\n",
            f"OSTD initialized. Preparing components.\n{UART_REGISTRATION_LOG}\n",
        )

    def audit_console_loss(
        self,
        diagnostic_lines: tuple[str, ...] | list[str] = CONSOLE_LOSS_DIAGNOSTIC_LINES,
        *,
        marker_event: str | None = None,
    ):
        return self.audit_console_loss_serial(
            self.console_loss_serial_log(diagnostic_lines),
            marker_event=marker_event,
        )

    def audit_console_loss_serial(
        self,
        serial_log: str,
        *,
        marker_event: str | None = None,
    ):
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        return qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=marker_event or self.successful_marker_event(),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
            variant=FIRST_PROCESS_CONSOLE_LOSS,
        )

    def diagnostic_audit(
        self,
        lines: tuple[str, ...] | list[str],
        *,
        newline: str = "\n",
        terminated: bool = True,
        variant: QemuUbootVariant | None = FIRST_PROCESS_CONSOLE_LOSS,
        leading_line: str | None = None,
    ):
        transcript_lines = ([] if leading_line is None else [leading_line]) + list(
            lines
        )
        serial_log = newline.join(transcript_lines)
        if terminated:
            serial_log += newline
        return qemu_uboot_audit.audit_diagnostic_markers(
            serial_log,
            variant=variant,
        )

    @staticmethod
    def replace_diagnostic_stage(
        lines: tuple[str, ...], stage: str, replacement: str
    ) -> tuple[str, ...]:
        replaced = list(lines)
        index = REQUIRED_DIAGNOSTIC_STAGES.index(stage)
        replaced[index] = replacement
        return tuple(replaced)

    def test_accepts_complete_ordered_console_loss_diagnostics(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "DiagnosticAudit"))
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                colored_lines = tuple(
                    f"\x1b[32m{line}\x1b[0m" for line in COMPLETE_DIAGNOSTIC_LINES
                )
                audit = self.diagnostic_audit(
                    colored_lines,
                    newline=newline,
                    leading_line="ordinary boot output",
                )

                self.assertTrue(audit.passed, audit.failures)
                self.assertEqual(audit.total_count, 8)
                self.assertEqual(audit.activation_count, 1)
                self.assertEqual(audit.ordered_stages, REQUIRED_DIAGNOSTIC_STAGES)
                self.assertEqual(audit.last_stage, "user_first_write_returned")
                self.assertEqual(audit.first_return_reason, "user_syscall")
                self.assertEqual(audit.first_syscall, 64)
                self.assertEqual(audit.first_syscall_sepc, 0x1000)
                self.assertEqual(audit.write_fd, 1)
                self.assertEqual(audit.write_requested, 50)
                self.assertEqual(audit.write_result, 50)
                self.assertIsNone(audit.page_fault_outcome)
                self.assertFalse(audit.repeated_page_fault)
                evidence = {item.stage: item for item in audit.stage_evidence}
                self.assertEqual(tuple(evidence), ALL_DIAGNOSTIC_STAGES)
                for index, stage in enumerate(REQUIRED_DIAGNOSTIC_STAGES, start=1):
                    self.assertEqual(evidence[stage].count, 1)
                    self.assertEqual(evidence[stage].indices, (index,))
                for stage in (
                    "user_first_page_fault",
                    "user_first_page_fault_handler",
                    "user_page_fault_repeated",
                ):
                    self.assertEqual(evidence[stage].count, 0)
                    self.assertEqual(evidence[stage].indices, ())

        equal_copy = replace(FIRST_PROCESS_CONSOLE_LOSS)
        with self.assertRaisesRegex(ValueError, "registered singleton"):
            self.diagnostic_audit(COMPLETE_DIAGNOSTIC_LINES, variant=equal_copy)

    def test_each_legal_prefix_reports_its_last_valid_stage(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "DiagnosticAudit"))
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        for length in range(len(REQUIRED_DIAGNOSTIC_STAGES) + 1):
            with self.subTest(length=length):
                prefix_lines = COMPLETE_DIAGNOSTIC_LINES[:length]
                audit = self.diagnostic_audit(
                    prefix_lines,
                    newline="\r\n",
                    leading_line="ordinary boot output",
                )
                expected_stages = REQUIRED_DIAGNOSTIC_STAGES[:length]
                expected_last = expected_stages[-1] if expected_stages else None
                evidence = {item.stage: item for item in audit.stage_evidence}

                self.assertEqual(audit.ordered_stages, expected_stages)
                self.assertEqual(audit.last_stage, expected_last)
                self.assertEqual(
                    audit.passed, length == len(REQUIRED_DIAGNOSTIC_STAGES)
                )
                for index, stage in enumerate(REQUIRED_DIAGNOSTIC_STAGES):
                    expected_indices = (index + 1,) if index < length else ()
                    self.assertEqual(evidence[stage].indices, expected_indices)
                    self.assertEqual(evidence[stage].count, int(index < length))

                if length <= 6:
                    disruption = COMPLETE_DIAGNOSTIC_LINES[length + 1]
                else:
                    disruption = COMPLETE_DIAGNOSTIC_LINES[length - 1]
                disrupted = self.diagnostic_audit(
                    (*prefix_lines, disruption),
                    leading_line="ordinary boot output",
                )
                self.assertFalse(disrupted.passed)
                self.assertEqual(disrupted.ordered_stages, expected_stages)
                self.assertEqual(disrupted.last_stage, expected_last)

    def test_every_marker_schema_requires_exact_complete_fields(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        exception_details = {
            "instruction_misaligned": "detail_kind=unavailable",
            "instruction_fault": "detail_kind=unavailable",
            "illegal_instruction": "detail_kind=instruction detail=0x13",
            "breakpoint": "detail_kind=unavailable",
            "load_misaligned": "detail_kind=stval detail=0x2000",
            "load_fault": "detail_kind=stval detail=0x3000",
            "store_misaligned": "detail_kind=stval detail=0x4000",
            "store_fault": "detail_kind=stval detail=0x5000",
            "user_env_call": "detail_kind=unavailable",
            "supervisor_env_call": "detail_kind=unavailable",
            "instruction_page_fault": "detail_kind=stval detail=0x6000",
            "load_page_fault": "detail_kind=stval detail=0x7000",
            "store_page_fault": "detail_kind=stval detail=0x8000",
            "unknown": "detail_kind=unavailable",
        }
        return_lines = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_syscall sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=kernel_event sepc=0x1000",
            *(
                f"{DIAGNOSTIC_PREFIX} stage=user_first_return "
                f"reason=user_exception sepc=0x1000 cause={cause} {detail}"
                for cause, detail in exception_details.items()
            ),
        )
        for line in return_lines:
            with self.subTest(valid_return=line):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    "user_first_return",
                    line,
                )
                audit = self.diagnostic_audit(lines)
                self.assertTrue(audit.passed, audit.failures)
                self.assertEqual(
                    audit.first_return_reason,
                    line.split(" reason=", 1)[1].split(" ", 1)[0],
                )

        for cause in (
            "instruction_page_fault",
            "load_page_fault",
            "store_page_fault",
        ):
            for outcome in ("resolved", "fault_signal_queued"):
                with self.subTest(page_fault_cause=cause, outcome=outcome):
                    lines = list(COMPLETE_DIAGNOSTIC_LINES)
                    lines[6:6] = [
                        f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                        f"cause={cause} stval=0x4000 sepc=0x1000",
                        f"{DIAGNOSTIC_PREFIX} "
                        f"stage=user_first_page_fault_handler outcome={outcome}",
                        f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
                        f"cause={cause} stval=0x4000 sepc=0x1000",
                    ]
                    audit = self.diagnostic_audit(lines)
                    self.assertTrue(audit.passed, audit.failures)
                    self.assertEqual(audit.page_fault_outcome, outcome)
                    self.assertTrue(audit.repeated_page_fault)

        malformed_required = {
            "diagnostic_active": (f"{DIAGNOSTIC_PREFIX} stage=diagnostic_active"),
            "process_components_ready": (
                f"{DIAGNOSTIC_PREFIX} stage=process_components_ready extra=1"
            ),
            "device_init_ready": (
                f"{DIAGNOSTIC_PREFIX} stage=device_init_ready extra=1"
            ),
            "stdio_init_ready": (f"{DIAGNOSTIC_PREFIX} stage=stdio_init_ready extra=1"),
            "user_enter": (
                f"{DIAGNOSTIC_PREFIX} stage=user_enter cpu=+0 sepc=0x1000 sp=0x2000"
            ),
            "user_first_return": (
                f"{DIAGNOSTIC_PREFIX} stage=user_first_return "
                "reason=user_syscall sepc=0x1000 cause=breakpoint"
            ),
            "user_first_syscall": (
                f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall id=0x40 sepc=0x1000"
            ),
            "user_first_write_returned": (
                f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned "
                "fd=1 requested=50 result=+50"
            ),
        }
        for stage, malformed in malformed_required.items():
            with self.subTest(malformed_stage=stage):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                evidence = {item.stage: item for item in audit.stage_evidence}
                self.assertFalse(audit.passed)
                self.assertEqual(evidence[stage].count, 0)

        invalid_exception_lines = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=unknown sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_exception "
            "sepc=0x1000 cause=illegal_instruction detail_kind=stval detail=0x13",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_exception "
            "sepc=0x1000 cause=breakpoint detail_kind=unavailable detail=0x0",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_exception "
            "sepc=0x1000 cause=load_fault detail_kind=stval detail=0X3000",
        )
        for malformed in invalid_exception_lines:
            with self.subTest(invalid_exception=malformed):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    "user_first_return",
                    malformed,
                )
                self.assertFalse(self.diagnostic_audit(lines).passed)

        malformed_optional = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=unknown stval=0x4000 sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler "
            "outcome=resolved extra=1",
            f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
            "cause=load_page_fault sepc=0x1000 stval=0x4000",
            f"{DIAGNOSTIC_PREFIX} stage=unknown_stage value=1",
        )
        for malformed in malformed_optional:
            with self.subTest(malformed_optional=malformed):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines.insert(6, malformed)
                self.assertFalse(self.diagnostic_audit(lines).passed)

        unterminated = self.diagnostic_audit(
            COMPLETE_DIAGNOSTIC_LINES,
            terminated=False,
        )
        self.assertFalse(unterminated.passed)
        self.assertEqual(unterminated.last_stage, "user_first_syscall")
        evidence = {item.stage: item for item in unterminated.stage_evidence}
        self.assertEqual(evidence["user_first_write_returned"].count, 0)

    def test_marker_free_normal_log_reports_no_last_stage(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        clean = qemu_uboot_audit.audit_diagnostic_markers(
            CLEAN_SERIAL_LOG,
            variant=None,
        )
        self.assertTrue(clean.passed, clean.failures)
        self.assertEqual(clean.total_count, 0)
        self.assertEqual(clean.activation_count, 0)
        self.assertEqual(clean.ordered_stages, ())
        self.assertIsNone(clean.last_stage)

        with_marker = CLEAN_SERIAL_LOG.replace(
            "Enter riscv_boot\n",
            "Enter riscv_boot\n" + COMPLETE_DIAGNOSTIC_LINES[0] + "\n",
        )
        rejected = qemu_uboot_audit.audit_diagnostic_markers(
            with_marker,
            variant=None,
        )
        self.assertFalse(rejected.passed)
        self.assertEqual(rejected.total_count, 1)
        self.assertEqual(rejected.activation_count, 1)
        self.assertIsNone(rejected.last_stage)

    def test_rejects_malformed_diagnostic_prefix_candidates(self) -> None:
        malformed_candidates = (
            f"{DIAGNOSTIC_PREFIX}X stage=diagnostic_active console_registry=empty",
            f"{DIAGNOSTIC_PREFIX}\tstage=diagnostic_active console_registry=empty",
            f"{DIAGNOSTIC_PREFIX}stage=diagnostic_active console_registry=empty",
        )

        for candidate in malformed_candidates:
            with self.subTest(candidate=candidate, variant="none"):
                audit = self.diagnostic_audit((candidate,), variant=None)
                self.assertEqual(audit.total_count, 1)
                self.assertFalse(audit.passed)
                self.assertIsNone(audit.last_stage)

            with self.subTest(candidate=candidate, variant="registered"):
                audit = self.diagnostic_audit((*COMPLETE_DIAGNOSTIC_LINES, candidate))
                self.assertEqual(audit.total_count, 9)
                self.assertFalse(audit.passed)
                self.assertEqual(audit.last_stage, "user_first_write_returned")

    def test_rejects_oversized_diagnostic_numbers(self) -> None:
        oversized = "9" * 5000
        cases = (
            (
                "user_first_syscall",
                "id=64",
                f"id={oversized}",
                "user_first_return",
            ),
            (
                "user_first_write_returned",
                "fd=1",
                f"fd={oversized}",
                "user_first_syscall",
            ),
            (
                "user_first_write_returned",
                "requested=50",
                f"requested={oversized}",
                "user_first_syscall",
            ),
            (
                "user_first_write_returned",
                "result=50",
                f"result={oversized}",
                "user_first_syscall",
            ),
            (
                "user_first_write_returned",
                "result=50",
                f"result=-{oversized}",
                "user_first_syscall",
            ),
        )

        for stage, original, replacement, expected_last in cases:
            with self.subTest(stage=stage, replacement=replacement[:24]):
                valid_line = COMPLETE_DIAGNOSTIC_LINES[
                    REQUIRED_DIAGNOSTIC_STAGES.index(stage)
                ]
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    valid_line.replace(original, replacement),
                )
                audit = self.diagnostic_audit(lines)
                evidence = {item.stage: item for item in audit.stage_evidence}

                self.assertFalse(audit.passed)
                self.assertEqual(evidence[stage].count, 0)
                self.assertEqual(audit.last_stage, expected_last)

    def test_rejects_missing_duplicate_reordered_and_truncated_markers(self) -> None:
        for index, stage in enumerate(REQUIRED_DIAGNOSTIC_STAGES):
            with self.subTest(disruption="missing", stage=stage):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines.pop(index)
                audit = self.diagnostic_audit(lines)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[:index]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(
                    audit.last_stage,
                    expected_prefix[-1] if expected_prefix else None,
                )

            with self.subTest(disruption="duplicate", stage=stage):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines.insert(index + 1, lines[index])
                audit = self.diagnostic_audit(lines)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[: index + 1]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(audit.last_stage, expected_prefix[-1])

        for index in range(len(REQUIRED_DIAGNOSTIC_STAGES) - 1):
            with self.subTest(disruption="reordered", index=index):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines[index], lines[index + 1] = lines[index + 1], lines[index]
                audit = self.diagnostic_audit(lines)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[:index]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(
                    audit.last_stage,
                    expected_prefix[-1] if expected_prefix else None,
                )

        truncated = self.diagnostic_audit(
            COMPLETE_DIAGNOSTIC_LINES,
            terminated=False,
        )
        self.assertFalse(truncated.passed)
        self.assertEqual(
            truncated.ordered_stages,
            REQUIRED_DIAGNOSTIC_STAGES[:-1],
        )
        self.assertEqual(truncated.last_stage, "user_first_syscall")

    def test_rejects_malformed_fields_for_every_marker_kind(self) -> None:
        required_cases = (
            (
                "diagnostic_active",
                "stage=diagnostic_active console_registry=occupied",
            ),
            ("process_components_ready", "stage=process_components_ready extra=1"),
            ("device_init_ready", "stage=device_init_ready extra=1"),
            ("stdio_init_ready", "stage=stdio_init_ready extra=1"),
            ("user_enter", "stage=user_enter cpu=-1 sepc=0x1000 sp=0x2000"),
            ("user_enter", "stage=user_enter cpu=0 sepc=1000 sp=0x2000"),
            ("user_enter", "stage=user_enter cpu=0 sepc=0x1000 sp=0X2000"),
            (
                "user_first_return",
                "stage=user_first_return reason=user_syscall sepc=0X1000",
            ),
            (
                "user_first_return",
                "stage=user_first_return reason=kernel_event sepc=1000",
            ),
            (
                "user_first_return",
                "stage=user_first_return reason=user_exception sepc=0x1000 "
                "cause=not_a_cause detail_kind=unavailable",
            ),
            ("user_first_syscall", "stage=user_first_syscall id=+56 sepc=0x1000"),
            ("user_first_syscall", "stage=user_first_syscall id=56 sepc=0X1000"),
            (
                "user_first_write_returned",
                "stage=user_first_write_returned fd=-1 requested=50 result=50",
            ),
            (
                "user_first_write_returned",
                "stage=user_first_write_returned fd=1 requested=+50 result=50",
            ),
            (
                "user_first_write_returned",
                "stage=user_first_write_returned fd=1 requested=50 result=0x32",
            ),
        )
        for stage, fields in required_cases:
            with self.subTest(stage=stage, fields=fields):
                malformed = f"{DIAGNOSTIC_PREFIX} {fields}"
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                stage_index = REQUIRED_DIAGNOSTIC_STAGES.index(stage)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[:stage_index]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(
                    audit.last_stage,
                    expected_prefix[-1] if expected_prefix else None,
                )

        valid_fault = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=load_page_fault stval=0x4000 sepc=0x1000"
        )
        valid_handler = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler outcome=resolved"
        )
        optional_cases = (
            (
                (),
                f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                "cause=unknown stval=0x4000 sepc=0x1000",
            ),
            (
                (),
                f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                "cause=load_page_fault stval=0X4000 sepc=0x1000",
            ),
            (
                (),
                f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                "cause=load_page_fault stval=0x4000 sepc=1000",
            ),
            (
                (valid_fault,),
                f"{DIAGNOSTIC_PREFIX} "
                "stage=user_first_page_fault_handler outcome=ignored",
            ),
            (
                (valid_fault, valid_handler),
                f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
                "cause=load_page_fault stval=4000 sepc=0x1000",
            ),
        )
        for prefix, malformed in optional_cases:
            with self.subTest(optional=malformed):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines[6:6] = [*prefix, malformed]
                audit = self.diagnostic_audit(lines)
                self.assertFalse(audit.passed)
                self.assertEqual(
                    audit.ordered_stages,
                    REQUIRED_DIAGNOSTIC_STAGES[:6],
                )
                self.assertEqual(audit.last_stage, "user_first_return")

    def test_rejects_unknown_extra_or_misordered_fields(self) -> None:
        cases = (
            (
                "diagnostic_active",
                f"{DIAGNOSTIC_PREFIX} stage=diagnostic_active "
                "console_registry=empty unknown=1",
            ),
            (
                "user_enter",
                f"{DIAGNOSTIC_PREFIX} stage=user_enter cpu=0 sp=0x2000 sepc=0x1000",
            ),
            (
                "user_first_return",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_return "
                "sepc=0x1000 reason=user_syscall",
            ),
            (
                "user_first_syscall",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall sepc=0x1000 id=56",
            ),
            (
                "user_first_write_returned",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned "
                "fd=1 result=50 requested=50",
            ),
            (
                "user_first_write_returned",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned "
                "fd=1 requested=50 result=50 extra=1",
            ),
        )
        for stage, malformed in cases:
            with self.subTest(stage=stage, malformed=malformed):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                stage_index = REQUIRED_DIAGNOSTIC_STAGES.index(stage)
                self.assertFalse(audit.passed)
                self.assertEqual(
                    audit.ordered_stages,
                    REQUIRED_DIAGNOSTIC_STAGES[:stage_index],
                )

        unknown_stage = list(COMPLETE_DIAGNOSTIC_LINES)
        unknown_stage.insert(6, f"{DIAGNOSTIC_PREFIX} stage=unknown_stage value=1")
        audit = self.diagnostic_audit(unknown_stage)
        self.assertFalse(audit.passed)
        self.assertEqual(
            audit.ordered_stages,
            REQUIRED_DIAGNOSTIC_STAGES[:6],
        )

    def test_rejects_invalid_return_reason_detail_coupling(self) -> None:
        invalid_return_fields = (
            "reason=unknown sepc=0x1000",
            "reason=user_syscall sepc=0x1000 cause=breakpoint",
            "reason=kernel_event sepc=0x1000 detail_kind=unavailable",
            "reason=user_exception sepc=0x1000 cause=illegal_instruction "
            "detail_kind=instruction",
            "reason=user_exception sepc=0x1000 cause=illegal_instruction "
            "detail_kind=stval detail=0x13",
            "reason=user_exception sepc=0x1000 cause=breakpoint "
            "detail_kind=unavailable detail=0x0",
            "reason=user_exception sepc=0x1000 cause=load_fault "
            "detail_kind=unavailable",
            "reason=user_exception sepc=0x1000 cause=load_fault "
            "detail_kind=stval detail=0X3000",
        )
        for fields in invalid_return_fields:
            with self.subTest(fields=fields):
                malformed = f"{DIAGNOSTIC_PREFIX} stage=user_first_return {fields}"
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    "user_first_return",
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                self.assertFalse(audit.passed)
                self.assertEqual(
                    audit.ordered_stages,
                    REQUIRED_DIAGNOSTIC_STAGES[:5],
                )
                self.assertEqual(audit.last_stage, "user_enter")
                self.assertIsNone(audit.first_return_reason)

    def test_accepts_only_a_complete_resolved_page_fault_pair(self) -> None:
        no_fault = self.audit_console_loss()
        self.assertTrue(no_fault.passed, no_fault.failures)
        self.assertEqual(
            no_fault.classification,
            FIRST_PROCESS_CONSOLE_LOSS.classification,
        )
        self.assertEqual(no_fault.effective_bootargs, self.diagnostic_bootargs())
        self.assertTrue(no_fault.diagnostic.passed, no_fault.diagnostic.failures)
        self.assertIsNone(no_fault.diagnostic.page_fault_outcome)

        resolved_lines = list(CONSOLE_LOSS_DIAGNOSTIC_LINES)
        resolved_lines[6:6] = [
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=load_page_fault stval=0x4000 sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler outcome=resolved",
        ]
        resolved = self.audit_console_loss(resolved_lines)
        self.assertTrue(resolved.passed, resolved.failures)
        self.assertEqual(resolved.diagnostic.page_fault_outcome, "resolved")
        self.assertFalse(resolved.diagnostic.repeated_page_fault)

        process_after_activation = self.console_loss_serial_log().replace(
            f"{PROCESS_STAGE_COMPLETE_LINE}\n{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}",
            f"{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}\n{PROCESS_STAGE_COMPLETE_LINE}",
        )
        rejected = self.audit_console_loss_serial(process_after_activation)
        self.assertFalse(rejected.passed)

    def test_rejects_queued_repeated_or_incomplete_page_fault_evidence(self) -> None:
        fault = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=load_page_fault stval=0x4000 sepc=0x1000"
        )
        resolved = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler outcome=resolved"
        )
        queued = (
            f"{DIAGNOSTIC_PREFIX} "
            "stage=user_first_page_fault_handler outcome=fault_signal_queued"
        )
        repeated = (
            f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
            "cause=load_page_fault stval=0x4000 sepc=0x1000"
        )
        cases = (
            (fault, queued),
            (fault, resolved, repeated),
            (fault,),
            (resolved,),
            (fault, repeated),
        )
        for optional_lines in cases:
            with self.subTest(optional_lines=optional_lines):
                lines = list(CONSOLE_LOSS_DIAGNOSTIC_LINES)
                lines[6:6] = optional_lines
                audit = self.audit_console_loss(lines)
                self.assertFalse(audit.passed)

    def test_rejects_wrong_first_syscall_or_write_result(self) -> None:
        mutations = (
            ("id=56", "id=64"),
            ("fd=1", "fd=2"),
            ("requested=50", "requested=49"),
            ("result=50", "result=49"),
            ("result=50", "result=-5"),
        )
        for original, replacement in mutations:
            with self.subTest(replacement=replacement):
                lines = tuple(
                    line.replace(original, replacement)
                    for line in CONSOLE_LOSS_DIAGNOSTIC_LINES
                )
                audit = self.audit_console_loss(lines)
                self.assertFalse(audit.passed)

        bad_marker_events = (
            "marker_seen=no\naction=SIGTERM\ncleanup_complete=yes\n",
            "marker_seen=yes\naction=already-exited\ncleanup_complete=yes\n",
            "marker_seen=yes\naction=SIGTERM\ncleanup_complete=no\n",
            "marker_seen=yes\naction=SIGTERM\ncleanup_complete=yes\n"
            "failure=boot-timeout\n",
            "marker_seen=yes\naction=SIGKILL\ncleanup_complete=yes\n"
            "failure=session-error\n",
        )
        for marker_event in bad_marker_events:
            with self.subTest(marker_event=marker_event):
                audit = self.audit_console_loss(marker_event=marker_event)
                self.assertFalse(audit.passed)

    def test_normal_and_stale_reject_any_diagnostic_prefix(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        injected_positive = MEGREZ_POSITIVE_SERIAL_LOG.replace(
            "Enter riscv_boot\n",
            f"Enter riscv_boot\n{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}\n",
        )
        positive = qemu_uboot_booti.audit_serial_log(
            injected_positive,
            marker_event=self.successful_marker_event(),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
        )
        self.assertFalse(positive.passed)
        self.assertGreater(positive.diagnostic.total_count, 0)

        injected_stale = MEGREZ_STALE_SERIAL_LOG.replace(
            "Enter riscv_boot\n",
            f"Enter riscv_boot\n{DIAGNOSTIC_PREFIX} malformed\n",
        )
        stale = qemu_uboot_booti.audit_serial_log(
            injected_stale,
            marker_event=(
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n"
            ),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )
        self.assertFalse(stale.passed)
        self.assertGreater(stale.diagnostic.total_count, 0)

    def test_registered_console_suppression_requires_flag_ns16550_and_zero_diagnostics(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")

        def audit(serial_log: str):
            return qemu_uboot_booti.audit_serial_log(
                serial_log,
                marker_event=self.successful_marker_event(),
                profile=profile,
                scenario=(qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION),
            )

        valid = audit(self.suppression_serial_log())
        self.assertTrue(valid.passed, valid.failures)
        self.assertEqual(valid.classification, "PASS")
        self.assertEqual(valid.effective_bootargs, self.diagnostic_bootargs())
        self.assertEqual(valid.userspace_marker_count, 1)
        self.assertEqual(valid.diagnostic.total_count, 0)

        mutations = (
            self.suppression_serial_log().replace(
                f" {FIRST_PROCESS_CONSOLE_LOSS.bootarg_suffix}",
                "",
            ),
            self.suppression_serial_log().replace(
                f"{UART_REGISTRATION_LOG}\n",
                "",
            ),
            self.suppression_serial_log().replace(
                f"{UART_REGISTRATION_LOG}\n",
                f"{UART_REGISTRATION_LOG}\n" * 2,
            ),
            self.suppression_serial_log().replace(
                qemu_uboot_audit.USERSPACE_MARKER_TEXT,
                "",
            ),
            self.suppression_serial_log().replace(
                "Enter riscv_boot\n",
                f"Enter riscv_boot\n{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}\n",
            ),
        )
        for serial_log in mutations:
            with self.subTest(serial_log=serial_log[-300:]):
                self.assertFalse(audit(serial_log).passed)

    def test_console_loss_rejects_real_uart_registration_log(self) -> None:
        serial_log = self.console_loss_serial_log().replace(
            "OSTD initialized. Preparing components.\n",
            f"OSTD initialized. Preparing components.\n{UART_REGISTRATION_LOG}\n",
        )

        audit = self.audit_console_loss_serial(serial_log)

        self.assertFalse(audit.passed)
        self.assertIn(
            "console-loss unexpectedly registered an NS16550 console",
            audit.failures,
        )

    def test_accepts_one_complete_marker_terminated_boot(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "audit_serial_log"))

        audit = qemu_uboot_booti.audit_serial_log(
            CLEAN_SERIAL_LOG,
            marker_event=self.successful_marker_event(),
        )

        self.assertEqual(audit.booti_command_count, 1)
        self.assertEqual(audit.userspace_marker_count, 1)
        self.assertEqual(audit.application_processor_ids, ())
        self.assertIsNone(audit.random_source)
        self.assertEqual(audit.classification, "PASS")
        self.assertTrue(audit.passed)

    def test_accepts_the_serial_interaction_ack_as_the_userspace_marker(self) -> None:
        ready = RX_READY_LINE.decode()
        ack = RX_ACK_LINE.decode()
        serial_log = CLEAN_SERIAL_LOG.replace(
            qemu_uboot_audit.USERSPACE_MARKER_TEXT,
            f"{ready}\n{ack}",
        )

        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            userspace_marker=ack,
            readiness_marker=ready,
        )

        self.assertEqual(audit.userspace_marker_count, 1)
        self.assertTrue(audit.passed, audit.failures)

        duplicate_ready = qemu_uboot_booti.audit_serial_log(
            serial_log.replace(ready, f"{ready}\n{ready}"),
            marker_event=self.successful_marker_event(),
            userspace_marker=ack,
            readiness_marker=ready,
        )
        self.assertFalse(duplicate_ready.passed)

    def test_accepts_each_megrez_ad_envelope_and_normalizes_ap_order(self) -> None:
        for profile_name, extensions in (
            ("megrez-sv48-svade-fast", "sstc,svade"),
            ("megrez-sv48-svadu-fast", "sstc,svadu"),
        ):
            with self.subTest(profile=profile_name):
                profile = qemu_uboot_booti.profile_by_name(profile_name)
                serial_log = MEGREZ_POSITIVE_SERIAL_LOG.replace(
                    "sstc,svade", extensions
                )

                audit = qemu_uboot_booti.audit_serial_log(
                    serial_log,
                    marker_event=self.successful_marker_event(),
                    profile=profile,
                    scenario=qemu_uboot_booti.BootScenario.POSITIVE,
                )

                self.assertTrue(audit.passed, audit.failures)
                self.assertEqual(audit.application_processor_ids, (1, 2, 3))
                self.assertEqual(audit.random_source, "timestamp")
                self.assertEqual(audit.classification, "PASS")

    def test_rejects_megrez_smp_rng_bootargs_and_seed_regressions(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        mutations = (
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                "Processor 3 started. Spinning for tasks.\n", ""
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                "use randomness based on the timestamp, which is insecure",
                "use randomness generated by hardware",
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                "use randomness based on the timestamp, which is insecure",
                "use randomness provided by the device tree",
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace("sstc,svade", "sstc,zkr,svade"),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                f'    bootargs = "{MEGREZ_BOOTARGS}";',
                f'    bootargs = "{MEGREZ_BOOTARGS}";\n    rng-seed = [01 02];',
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                MEGREZ_BOOTARGS,
                "console=ttyS0 loglevel=info init=/init",
            ),
        )

        for serial_log in mutations:
            with self.subTest(serial_log=serial_log):
                audit = qemu_uboot_booti.audit_serial_log(
                    serial_log,
                    marker_event=self.successful_marker_event(),
                    profile=profile,
                )
                self.assertFalse(audit.passed)

    def test_classifies_the_exact_stale_bootargs_failure_as_expected(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        audit = qemu_uboot_booti.audit_serial_log(
            MEGREZ_STALE_SERIAL_LOG,
            marker_event=(
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n"
            ),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )

        self.assertTrue(audit.passed, audit.failures)
        self.assertEqual(audit.userspace_marker_count, 0)
        self.assertEqual(audit.classification, "EXPECTED_INIT_ENOENT")

    def test_stale_bootargs_requires_exact_panic_and_timeout_cleanup(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        cases = (
            (
                MEGREZ_STALE_SERIAL_LOG.replace("found a negative dentry", "missing"),
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n",
            ),
            (
                MEGREZ_STALE_SERIAL_LOG,
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=no\n",
            ),
            (
                MEGREZ_STALE_SERIAL_LOG
                + ">>> Hello from RISC-V userspace on Asterinas! <<<\n",
                "marker_seen=yes\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n",
            ),
            (
                MEGREZ_STALE_SERIAL_LOG + "secondary panic after init failure\n",
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n",
            ),
        )
        for serial_log, marker_event in cases:
            with self.subTest(marker_event=marker_event):
                audit = qemu_uboot_booti.audit_serial_log(
                    serial_log,
                    marker_event=marker_event,
                    profile=profile,
                    scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                )
                self.assertFalse(audit.passed)

    def test_positive_scenario_still_rejects_the_stale_panic(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        audit = qemu_uboot_booti.audit_serial_log(
            MEGREZ_STALE_SERIAL_LOG,
            marker_event="marker_seen=no\naction=SIGTERM\n",
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
        )

        self.assertFalse(audit.passed)

    def test_registered_milestones_accept_expected_command_echoes(self) -> None:
        audit = self.audit_registered_serial(self.registered_serial_log())

        self.assertTrue(audit.passed, audit.failures)

    def test_registered_milestones_reject_a_reboot_after_the_terminal(self) -> None:
        self.assert_registered_failure(
            self.registered_serial_log() + "OpenSBI v1.7\n",
            "milestone repeated after terminal: FirmwareReady",
        )

    def test_registered_milestones_reject_a_reboot_before_the_successful_chain(
        self,
    ) -> None:
        serial_log = "OpenSBI v1.7\nU-Boot 2026.07\n" + self.registered_serial_log()

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: FirmwareReady expected 1, got 2",
        )

    def test_registered_milestones_reject_repeated_firmware_before_bootloader(
        self,
    ) -> None:
        serial_log = self.registered_serial_log().replace(
            "OpenSBI v1.7\n",
            "OpenSBI v1.7\nOpenSBI v1.7\n",
            1,
        )

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: FirmwareReady expected 1, got 2",
        )

    def test_registered_milestones_reject_a_replayed_pre_booti_marker(
        self,
    ) -> None:
        marker_exchange = "=> echo ASTERINAS_PRE_BOOTI\nASTERINAS_PRE_BOOTI\n"
        serial_log = self.registered_serial_log().replace(
            marker_exchange,
            marker_exchange * 2,
            1,
        )

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: ArtifactsLoaded expected 2, got 4",
        )

    def test_registered_milestones_reject_an_early_duplicate(self) -> None:
        profile = self.registered_profile()
        kernel_ready = next(
            milestone.line.decode()
            for milestone in profile.validation.milestones
            if milestone.stage.value == "KernelReady"
        )
        rootfs_ready = next(
            milestone.line.decode()
            for milestone in profile.validation.milestones
            if milestone.stage.value == "RootfsReady"
        )
        serial_log = self.registered_serial_log().replace(
            kernel_ready,
            f"{rootfs_ready}\n{kernel_ready}",
            1,
        )

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: RootfsReady expected 1, got 2",
        )

    def test_rejects_the_wrong_ad_extension_for_the_selected_profile(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")
        serial_log = CLEAN_SERIAL_LOG.replace(
            "console=ttyS0 loglevel=info init=/init",
            profile.bootargs,
        )

        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            profile=profile,
        )

        self.assertFalse(audit.passed)
        self.assertIn("OpenSBI did not prove forced svadu mode", audit.failures)

    def assert_rejected(self, serial_log: str) -> None:
        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
        )
        self.assertFalse(audit.passed)

    def test_rejects_a_kernel_crc_mismatch(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("57c40418", "deadbeef"))

    def test_rejects_a_missing_console_bootarg(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("console=ttyS0 ", ""))

    def test_rejects_bootargs_from_the_persistent_environment(self) -> None:
        self.assert_rejected(
            CLEAN_SERIAL_LOG.replace(
                "bootargs=console=ttyS0 loglevel=info init=/init",
                "bootargs=cpu_no_boost_1_6ghz",
            )
        )

    def test_rejects_a_missing_fdt_bootargs_print(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("=> fdt print /chosen\n", ""))

    def test_rejects_a_mismatched_dtb_bootargs_proof(self) -> None:
        self.assert_rejected(
            CLEAN_SERIAL_LOG.replace(
                'bootargs = "console=ttyS0 loglevel=info init=/init";',
                'bootargs = "cpu_no_boost_1_6ghz";',
            )
        )

    def test_rejects_bootargs_transaction_after_userspace_marker(self) -> None:
        transaction = """\
=> setenv bootargs "console=ttyS0 loglevel=info init=/init"
=> printenv bootargs
bootargs=console=ttyS0 loglevel=info init=/init
=> fdt set /chosen bootargs "console=ttyS0 loglevel=info init=/init"
=> fdt print /chosen
    bootargs = "console=ttyS0 loglevel=info init=/init";
"""
        serial_log = CLEAN_SERIAL_LOG.replace(transaction, "") + transaction

        self.assert_rejected(serial_log)

    def test_rejects_a_persistent_environment_write(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG + "=> saveenv\nSaving Environment\n")

    def test_does_not_treat_a_longer_command_as_saveenv(self) -> None:
        audit = qemu_uboot_booti.audit_serial_log(
            CLEAN_SERIAL_LOG + "=> saveenvironment\nUnknown command\n",
            marker_event=self.successful_marker_event(),
        )

        self.assertTrue(audit.passed, audit.failures)

    def test_rejects_a_panic_even_after_the_userspace_marker(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG + "Uncaught panic:\n")

    def test_rejects_svadu_when_the_run_requires_svade(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("svade", "svadu"))

    def test_rejects_a_missing_pre_booti_marker(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("ASTERINAS_PRE_BOOTI\n", "", 1))

    def test_rejects_a_missing_kernel_entry_marker(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("Enter riscv_boot\n", ""))

    def test_rejects_a_payload_that_overlaps_a_bdinfo_reserved_range(self) -> None:
        self.assert_rejected(
            CLEAN_SERIAL_LOG.replace(
                "[0x80000000-0x8004ffff]",
                "[0x80200000-0x80200fff]",
            )
        )

    def test_rejects_crcs_reported_for_the_wrong_artifact_addresses(self) -> None:
        swapped = CLEAN_SERIAL_LOG.replace("crc32 for 80200000", "crc32 for dead0000")
        swapped = swapped.replace("crc32 for 83000000", "crc32 for 80200000")
        swapped = swapped.replace("crc32 for dead0000", "crc32 for 83000000")

        self.assert_rejected(swapped)


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
                **kwargs,
            )

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


class PreparationContractTests(unittest.TestCase):
    def test_variant_preparation_packages_only_payload_dtb(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        self.assertIn('variant="${QEMU_UBOOT_VARIANT:-}"', script)
        self.assertIn('qemu_uboot_variants.py" validate', script)
        self.assertIn('qemu_uboot_dtb.py" derive-variant', script)
        self.assertIn('qemu_uboot_dtb.py" audit-existing', script)
        variant_offset = script.index('variant="${QEMU_UBOOT_VARIANT:-}"')
        validation_offset = script.index('qemu_uboot_variants.py" validate')
        mkdir_offset = script.index('mkdir -p "${out_dir}" "${cache_dir}"')
        derive_offset = script.index('qemu_uboot_dtb.py" derive-variant')
        audit_offset = script.index('qemu_uboot_dtb.py" audit-existing')
        stage_copy = (
            'cp "${out_dir}/${dtb_filename}" "${stage_dir}/${dtb_filename}"'
        )

        self.assertLess(variant_offset, validation_offset)
        self.assertLess(validation_offset, mkdir_offset)
        self.assertLess(derive_offset, audit_offset)
        self.assertIn('--source-dtb "${out_dir}/qemu-virt.source.dtb"', script)
        self.assertIn('--payload-dtb "${out_dir}/${dtb_filename}"', script)
        self.assertIn(
            '--audit-output "${out_dir}/qemu-dtb-variant-audit.json"',
            script,
        )
        self.assertIn(stage_copy, script)
        self.assertNotIn('qemu-virt.source.dtb" "${stage_dir}', script)
        self.assertNotIn('qemu-dtb-audit.json" "${stage_dir}', script)
        self.assertNotIn('qemu-dtb-variant-audit.json" "${stage_dir}', script)
        for evidence in (
            '"${out_dir}/qemu-virt.source.dtb"',
            '"${out_dir}/${dtb_filename}"',
            '"${out_dir}/qemu-dtb-variant-audit.json"',
            '"${out_dir}/qemu-dtb-audit.json"',
            '"${out_dir}/boot.ext4"',
        ):
            self.assertIn(evidence, script)

    def test_preparation_rejects_unknown_variant_before_side_effects(self) -> None:
        output_root = REPO_ROOT / "target/qemu-uboot"
        output_root.mkdir(parents=True, exist_ok=True)
        cases = (
            ("unknown variant", "not-registered", None, "unknown QEMU U-Boot variant"),
            (
                "registered variant with default profile",
                FIRST_PROCESS_CONSOLE_LOSS.name,
                None,
                f"requires profile {FIRST_PROCESS_CONSOLE_LOSS.base_profile_name}",
            ),
            (
                "registered variant with explicit generic profile",
                FIRST_PROCESS_CONSOLE_LOSS.name,
                "generic-sv39",
                f"requires profile {FIRST_PROCESS_CONSOLE_LOSS.base_profile_name}",
            ),
        )
        for label, variant, profile, expected_error in cases:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory(dir=output_root) as tmp,
            ):
                root = Path(tmp)
                out_dir = root / "out"
                cache_dir = root / "cache"
                environment = {
                    "PATH": os.environ["PATH"],
                    "ASTERINAS_RISCV_BOOTI": str(root / "missing-kernel"),
                    "ASTERINAS_INITRAMFS": str(root / "missing-initramfs"),
                    "QEMU_UBOOT_OUT_DIR": str(out_dir),
                    "QEMU_UBOOT_CACHE_DIR": str(cache_dir),
                    "QEMU_UBOOT_VARIANT": variant,
                }
                if profile is not None:
                    environment["QEMU_UBOOT_PROFILE"] = profile
                result = subprocess.run(
                    ["bash", str(PREPARE_SCRIPT), "prepare"],
                    capture_output=True,
                    env=environment,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(out_dir.exists())
                self.assertFalse(cache_dir.exists())

    def test_resolves_relative_managed_paths_from_the_repository_root(self) -> None:
        relative = "target/qemu-uboot/relative-contract"
        expected = REPO_ROOT / relative

        output = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--canonical-output-dir", relative],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )
        build = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--canonical-build-dir", relative],
            cwd="/tmp",
            capture_output=True,
            text=True,
        )

        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout.strip(), str(expected))
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(build.stdout.strip(), str(expected))

    def test_accepts_only_nested_private_ltp_qemu_output_paths(self) -> None:
        accepted = REPO_ROOT / "target/ltp/qemu/smp1"

        result = subprocess.run(
            [
                "bash",
                str(PREPARE_SCRIPT),
                "--canonical-output-dir",
                str(accepted),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), accepted)

    def test_rejects_ltp_output_paths_outside_private_qemu_children(self) -> None:
        rejected_paths = (
            REPO_ROOT / "target/ltp",
            REPO_ROOT / "target/ltp/qemu",
            REPO_ROOT / "target/ltp/not-qemu",
            REPO_ROOT / "target/qemu-uboot/../ltp/escape",
        )

        for rejected in rejected_paths:
            with self.subTest(rejected=rejected):
                result = subprocess.run(
                    [
                        "bash",
                        str(PREPARE_SCRIPT),
                        "--canonical-output-dir",
                        str(rejected),
                    ],
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("must resolve below", result.stderr)

    def test_shared_qemu_current_output_path_remains_accepted(self) -> None:
        current = REPO_ROOT / "target/qemu-uboot/current"

        result = subprocess.run(
            [
                "bash",
                str(PREPARE_SCRIPT),
                "--canonical-output-dir",
                str(current),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), current)

    def test_rejects_an_output_directory_with_parent_traversal(self) -> None:
        candidate = REPO_ROOT / "target/qemu-uboot/../../../outside"

        result = subprocess.run(
            [
                "bash",
                str(PREPARE_SCRIPT),
                "--canonical-output-dir",
                str(candidate),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must resolve below", result.stderr)

    def test_rejects_an_output_directory_through_a_symlink(self) -> None:
        output_root = REPO_ROOT / "target/qemu-uboot"
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as tmp:
            link = Path(tmp) / "outside"
            link.symlink_to(Path(tmp).parent.parent.parent)

            result = subprocess.run(
                [
                    "bash",
                    str(PREPARE_SCRIPT),
                    "--canonical-output-dir",
                    str(link / "run"),
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must resolve below", result.stderr)

    def test_prints_the_pinned_uboot_commit(self) -> None:
        result = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--print-uboot-commit"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "ece349ade2973e220f524ce59e59711cc919263f\n",
        )

    def test_check_tools_reports_every_missing_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as empty_path:
            python = Path(empty_path) / "python3"
            python.write_text("#!/bin/sh\nexit 1\n")
            python.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(PREPARE_SCRIPT), "--check-tools"],
                capture_output=True,
                env={"PATH": empty_path},
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        for tool in (
            "dtc",
            "fdtget",
            "fdtput",
            "git",
            "make",
            "mkfs.ext4",
            "qemu-system-riscv64",
            "riscv64-linux-gnu-gcc",
        ):
            self.assertIn(f"missing tool: {tool}", result.stderr)
        self.assertIn("missing Python module: setuptools", result.stderr)
        self.assertIn("missing Python development headers", result.stderr)

    def test_preparation_delegates_profile_matched_dtb_before_artifacts(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        self.assertIn("QEMU_UBOOT_PROFILE:-generic-sv39", script)
        self.assertNotIn(
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
            script,
        )
        validate_offset = script.index('qemu_uboot_dtb.py" validate-profile')
        clone_offset = script.index("git clone --filter=blob:none")
        generate_offset = script.index('qemu_uboot_dtb.py" generate')
        copy_offset = script.index(
            'cp "${out_dir}/${dtb_filename}" "${stage_dir}/${dtb_filename}"'
        )
        manifest_offset = script.index('qemu_uboot_booti.py" write-manifest')
        checksum_offset = script.index("\n    sha256sum \\\n")
        self.assertLess(validate_offset, clone_offset)
        self.assertLess(generate_offset, copy_offset)
        self.assertLess(copy_offset, manifest_offset)
        self.assertLess(manifest_offset, checksum_offset)

    def test_payload_manifest_contains_only_the_three_boot_artifacts(self) -> None:
        result = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "--print-payload-manifest"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["asterinas.booti", "initramfs.cpio.gz", "qemu-virt.dtb"],
        )

    def test_rejects_a_mismatched_uboot_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            (checkout / "README").write_text("not the pinned revision\n")
            subprocess.run(["git", "-C", str(checkout), "add", "README"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )

            result = subprocess.run(
                [
                    "bash",
                    str(PREPARE_SCRIPT),
                    "--verify-uboot-source",
                    str(checkout),
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("U-Boot commit mismatch", result.stderr)

    def test_rejects_a_dirty_uboot_checkout_at_the_expected_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            tracked = checkout / "tracked"
            tracked.write_text("clean\n")
            subprocess.run(["git", "-C", str(checkout), "add", "tracked"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            expected_commit = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked.write_text("dirty\n")

            result = subprocess.run(
                [
                    "bash",
                    str(PREPARE_SCRIPT),
                    "--verify-uboot-source",
                    str(checkout),
                    expected_commit,
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("U-Boot checkout is not clean", result.stderr)

    def test_prepare_requires_explicit_kernel_and_initramfs(self) -> None:
        result = subprocess.run(
            ["bash", str(PREPARE_SCRIPT), "prepare"],
            capture_output=True,
            env={"PATH": os.environ["PATH"]},
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ASTERINAS_RISCV_BOOTI", result.stderr)
        self.assertIn("ASTERINAS_INITRAMFS", result.stderr)

    def test_preparation_preserves_firmware_and_qemu_provenance(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        self.assertIn("u-boot-commit.txt", script)
        self.assertIn("u-boot.config", script)
        self.assertIn("qemu-version.txt", script)
        self.assertIn('"${out_dir}/u-boot.config"', script)

    def test_preparation_clears_stale_run_evidence_before_fetching_uboot(self) -> None:
        script = PREPARE_SCRIPT.read_text()

        clear_offset = script.index('rm -f "${out_dir}/result.json"')
        clone_offset = script.index("git clone --filter=blob:none")
        self.assertLess(clear_offset, clone_offset)
        self.assertIn('"${out_dir}/serial.log"', script[clear_offset:clone_offset])
        self.assertIn(
            '"${out_dir}/marker-event.txt"', script[clear_offset:clone_offset]
        )
        self.assertIn('"${out_dir}/qemu-virt.dtb"', script[clear_offset:clone_offset])


class RepositoryIntegrationTests(unittest.TestCase):
    def test_marker_init_writes_to_serial_and_remains_alive(self) -> None:
        source = INITRAMFS_SOURCE.read_text()

        self.assertIn("/dev/ttyS0", source)
        self.assertIn("li a7, 56", source)
        self.assertNotIn("li a7, 93", source)

    def test_marker_init_retries_short_writes_in_order(self) -> None:
        source = INITRAMFS_SOURCE.read_text()
        write_loop = source[source.index("1:") : source.index("2:")]

        required_operations = (
            "mv s0, a0",
            "mv a0, s0",
            "li a7, 64",
            "ecall",
            "blez a0, 3b",
            "sub a2, a2, a0",
            "add a1, a1, a0",
            "bnez a2, 3b",
        )
        offsets = [write_loop.index(operation) for operation in required_operations]
        self.assertEqual(offsets, sorted(offsets))

    def test_tracked_marker_initramfs_builder_is_reproducible(self) -> None:
        builder = INITRAMFS_BUILDER.read_text()
        source = INITRAMFS_SOURCE.read_text()

        self.assertIn("gzip.compress", builder)
        self.assertIn("mtime=0", builder)
        self.assertIn("riscv64-linux-gnu-gcc", builder)
        self.assertIn(">>> Hello from RISC-V userspace on Asterinas! <<<", source)
        self.assertIn("li a7, 64", source)

    @unittest.skipUnless(
        shutil.which("riscv64-linux-gnu-gcc"),
        "RISC-V cross compiler is only required in the development container",
    )
    def test_marker_initramfs_builder_is_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.cpio.gz"
            second = Path(tmp) / "second.cpio.gz"
            subprocess.run(
                [sys.executable, str(INITRAMFS_BUILDER), str(first)], check=True
            )
            subprocess.run(
                [sys.executable, str(INITRAMFS_BUILDER), str(second)], check=True
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_controller_is_split_into_single_responsibility_modules(self) -> None:
        module_names = (
            "qemu_uboot_artifacts.py",
            "qemu_uboot_commands.py",
            "qemu_uboot_execution.py",
            "qemu_uboot_session.py",
            "qemu_uboot_audit.py",
        )

        self.assertEqual(
            [name for name in module_names if not (SCRIPT.parent / name).is_file()],
            [],
        )
        # The prepare script grew past 250 lines with the Linux reference and
        # third-board flows; keep a generous bound that still flags a
        # future uncontrolled growth rather than a single added command.
        self.assertLessEqual(len(SCRIPT.read_text().splitlines()), 400)

    def test_make_exposes_unit_and_full_uboot_booti_targets(self) -> None:
        makefile = MAKEFILE.read_text()

        self.assertIn("test_riscv_uboot_booti_unit:", makefile)
        self.assertIn("test_riscv_uboot_booti:", makefile)
        self.assertIn("prepare_qemu_uboot_booti.sh prepare", makefile)
        self.assertIn("qemu_uboot_booti.py run", makefile)
        self.assertIn(
            'QEMU_UBOOT_BUILD_DIR="$(QEMU_UBOOT_BUILD_DIR_EFFECTIVE)"',
            makefile,
        )
        generic_target = makefile[
            makefile.index("test_riscv_uboot_booti:") :
            makefile.index(".PHONY: test_riscv_sifive_u")
        ]
        self.assertIn('QEMU_UBOOT_PROFILE="generic-sv39"', generic_target)
        self.assertIn('--profile "generic-sv39"', generic_target)
        self.assertIn("flock", PREPARE_SCRIPT.read_text())

    def test_make_exposes_opt_in_sifive_smoke_and_linux_control(self) -> None:
        makefile = MAKEFILE.read_text()

        self.assertIn("test_riscv_sifive_u:", makefile)
        self.assertIn("test_riscv_sifive_u_linux_reference:", makefile)
        self.assertIn("sifive-u-asterinas-smoke", makefile)
        self.assertIn("sifive-u-linux-reference", makefile)
        self.assertIn("SIFIVE_U_KERNEL_LABEL := ASTERINAS_RISCV_BOOTI", makefile)
        self.assertIn("SIFIVE_U_INITRAMFS_LABEL := ASTERINAS_INITRAMFS", makefile)
        self.assertIn("SIFIVE_U_KERNEL_LABEL := RISCV_LINUX_IMAGE", makefile)
        self.assertIn("SIFIVE_U_INITRAMFS_LABEL := RISCV_LINUX_INITRAMFS", makefile)
        self.assertIn('$(SIFIVE_U_KERNEL_LABEL) is required', makefile)
        self.assertIn('$(SIFIVE_U_INITRAMFS_LABEL) is required', makefile)

        target_text = makefile[
            makefile.index("test_riscv_sifive_u:") :
            makefile.index(".PHONY: check_vdso")
        ]
        for downloader in ("curl ", "wget "):
            self.assertNotIn(downloader, target_text)
        invoked_python = {
            token for token in target_text.split() if token.endswith(".py")
        }
        self.assertEqual(invoked_python, {"tools/riscv/qemu_uboot_booti.py"})

    def test_make_uses_one_nonempty_absolute_path_contract(self) -> None:
        default_out = REPO_ROOT / "target/qemu-uboot/current"
        default_build = REPO_ROOT / "target/qemu-uboot/cache/u-boot-build"
        empty = subprocess.run(
            [
                "make",
                "-n",
                "test_riscv_uboot_booti",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                "QEMU_UBOOT_OUT_DIR=",
                "QEMU_UBOOT_BUILD_DIR=",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn(f'QEMU_UBOOT_OUT_DIR="{default_out}"', empty.stdout)
        self.assertIn(f'QEMU_UBOOT_BUILD_DIR="{default_build}"', empty.stdout)
        self.assertIn(f'--uboot "{default_build}/u-boot"', empty.stdout)
        self.assertIn(f'--manifest "{default_out}/artifacts.json"', empty.stdout)

        normalized_out = REPO_ROOT / "target/qemu-uboot/final"
        normalized_build = REPO_ROOT / "target/qemu-uboot/custom-build"
        relative = subprocess.run(
            [
                "make",
                "-n",
                "test_riscv_uboot_booti",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                "QEMU_UBOOT_OUT_DIR=target/qemu-uboot/missing/../final",
                "QEMU_UBOOT_BUILD_DIR=target/qemu-uboot/cache/../custom-build",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(relative.returncode, 0, relative.stderr)
        self.assertNotIn("missing/..", relative.stdout)
        self.assertNotIn("cache/..", relative.stdout)
        self.assertIn(f'QEMU_UBOOT_OUT_DIR="{normalized_out}"', relative.stdout)
        self.assertIn(f'QEMU_UBOOT_BUILD_DIR="{normalized_build}"', relative.stdout)
        self.assertIn(f'--uboot "{normalized_build}/u-boot"', relative.stdout)
        self.assertIn(f'--manifest "{normalized_out}/artifacts.json"', relative.stdout)

        sifive = subprocess.run(
            [
                "make", "-n", "test_riscv_sifive_u",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                "RISCV_SIFIVE_U_OUT_DIR=",
                "RISCV_SIFIVE_U_BUILD_DIR=",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        expected_sifive_out = REPO_ROOT / "target/qemu-uboot/sifive-u"
        expected_sifive_build = (
            REPO_ROOT / "target/qemu-uboot/cache/sifive-u-uboot-build"
        )
        self.assertEqual(sifive.returncode, 0, sifive.stderr)
        self.assertIn(f'QEMU_UBOOT_OUT_DIR="{expected_sifive_out}"', sifive.stdout)
        self.assertIn(f'--uboot "{expected_sifive_build}/u-boot.bin"', sifive.stdout)

    def test_make_never_sources_generated_path_metadata(self) -> None:
        hostile_out_dir = REPO_ROOT / "target/qemu-uboot/x;touch injected;#"
        result = subprocess.run(
            [
                "make",
                "-n",
                "test_riscv_uboot_booti",
                "ASTERINAS_RISCV_BOOTI=/tmp/kernel",
                "ASTERINAS_INITRAMFS=/tmp/initramfs",
                f"QEMU_UBOOT_OUT_DIR={hostile_out_dir}",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("prepared.env", result.stdout)
        self.assertNotIn("prepared.env", PREPARE_SCRIPT.read_text())

    def test_readme_documents_entry_points_and_qemu_limit(self) -> None:
        readme = README.read_text()

        self.assertIn("make test_riscv_uboot_booti_unit", readme)
        self.assertIn("make test_riscv_uboot_booti", readme)
        self.assertIn("make test_riscv_sifive_u", readme)
        self.assertIn("make test_riscv_sifive_u_linux_reference", readme)
        self.assertIn("evidence, not hardware emulation", readme)
        self.assertIn("userspace marker", readme)
        self.assertIn("RISCV_LINUX_IMAGE", readme)
        self.assertIn("RISCV_LINUX_INITRAMFS", readme)


if __name__ == "__main__":
    unittest.main()
