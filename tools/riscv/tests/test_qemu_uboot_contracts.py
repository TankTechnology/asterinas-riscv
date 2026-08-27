#!/usr/bin/env python3

from __future__ import annotations

import json
import shutil
import subprocess
import struct
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
RUNNER = TOOLS / "qemu_uboot_booti.py"
sys.path.insert(0, str(TOOLS))

from qemu_uboot_profiles import (  # noqa: E402
    ASTERINAS_USERSPACE_SMOKE,
    GENERIC_SV39,
    MEGREZ_SV48_SLOW,
    MEGREZ_SV48_SVADE_FAST,
    MEGREZ_SV48_SVADU_FAST,
    QEMU_VIRT,
    QEMU_VIRT_SMP4,
    SIFIVE_U,
    SIFIVE_U_ASTERINAS_SMOKE,
    SIFIVE_U_LINUX_REFERENCE,
    UBOOT_BOOTI,
    AuditPolicy,
    BootActionKind,
    BootFlow,
    BootMilestone,
    DtbProvider,
    Fidelity,
    MachineContract,
    MilestoneExpectation,
    QemuMachine,
    QemuUbootProfile,
    ResultScope,
    StorageTransport,
    ValidationScenario,
    profile_by_name,
    validate_registered_profile,
)
from qemu_uboot_commands import BootCommand, boot_commands, qemu_argv  # noqa: E402
from qemu_uboot_devices import (  # noqa: E402
    BOCHS_XRGB8888,
    HEADLESS,
    MEGREZ_BASIC,
    DeviceKind,
    QemuDeviceSet,
    RuntimeDevicePaths,
    device_set_by_name,
    render_device_argv,
    validate_registered_device_set,
)
from qemu_uboot_dtb import generated_dtb_qemu_argv  # noqa: E402
from qemu_uboot_dtb import (  # noqa: E402
    GeneratedDtbAudit,
    generated_dtb_audit_record,
    load_generated_dtb_audit,
)
from megrez_contract import artifact_identity  # noqa: E402
from qemu_uboot_artifacts import (  # noqa: E402
    artifact_expectations_from_paths,
    verify_boot_disk_artifacts,
)
from qemu_uboot_execution import (  # noqa: E402
    RunStatus,
    TerminalClassification,
    _run_status,
    ktap_summary,
)
from qemu_uboot_session import MilestoneTracker  # noqa: E402
from qemu_uboot_booti import _parse_args  # noqa: E402


class ContractCompositionTests(unittest.TestCase):
    @staticmethod
    def _linux_image() -> bytes:
        image = bytearray(64)
        struct.pack_into("<I", image, 0x00, 0x0400006F)
        struct.pack_into("<Q", image, 0x08, 0x20_0000)
        struct.pack_into("<Q", image, 0x10, len(image))
        struct.pack_into("<I", image, 0x20, 2)
        image[0x30:0x38] = b"RISCV\0\0\0"
        struct.pack_into("<I", image, 0x38, 0x05435352)
        return bytes(image)

    def test_device_sets_are_registered_and_frozen(self) -> None:
        self.assertIs(device_set_by_name("headless"), HEADLESS)
        self.assertIs(device_set_by_name("megrez-basic"), MEGREZ_BASIC)
        self.assertEqual(
            MEGREZ_BASIC.devices,
            (DeviceKind.BOCHS_DISPLAY,),
        )
        with self.assertRaises(FrozenInstanceError):
            MEGREZ_BASIC.name = "changed"

    def test_replaced_device_set_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "registered device set"):
            validate_registered_device_set(
                replace(MEGREZ_BASIC, devices=(DeviceKind.BOCHS_DISPLAY,))
            )

    def test_boot_commands_rejects_replaced_device_set_at_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "registered device set"):
            boot_commands(
                device_set=replace(
                    MEGREZ_BASIC,
                    devices=(DeviceKind.BOCHS_DISPLAY,),
                )
            )

    def test_megrez_basic_injects_one_fixed_framebuffer_before_booti(self) -> None:
        commands = boot_commands(
            profile=MEGREZ_SV48_SVADE_FAST,
            device_set=MEGREZ_BASIC,
        )
        names = [command.name for command in commands]
        expected_framebuffer_plan = (
            BootCommand("framebuffer-resize", "fdt resize 0x2000", "=>"),
            BootCommand("framebuffer-pci-probe", "pci display 0.1.0", "=>"),
            BootCommand(
                "framebuffer-node",
                "fdt mknode / framebuffer@40000000",
                "=>",
            ),
            BootCommand(
                "framebuffer-compatible",
                'fdt set /framebuffer@40000000 compatible "simple-framebuffer"',
                "=>",
            ),
            BootCommand(
                "framebuffer-reg",
                "fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x1000000>",
                "=>",
            ),
            BootCommand(
                "framebuffer-width",
                "fdt set /framebuffer@40000000 width <0x500>",
                "=>",
            ),
            BootCommand(
                "framebuffer-height",
                "fdt set /framebuffer@40000000 height <0x400>",
                "=>",
            ),
            BootCommand(
                "framebuffer-stride",
                "fdt set /framebuffer@40000000 stride <0x1400>",
                "=>",
            ),
            BootCommand(
                "framebuffer-format",
                'fdt set /framebuffer@40000000 format "x8r8g8b8"',
                "=>",
            ),
            BootCommand(
                "framebuffer-status",
                'fdt set /framebuffer@40000000 status "okay"',
                "=>",
            ),
            BootCommand(
                "framebuffer-verify",
                "fdt print /framebuffer@40000000",
                "simple-framebuffer",
            ),
        )
        self.assertEqual(names.count("dtb-resize"), 1)
        dtb_resize_index = names.index("dtb-resize")
        framebuffer_start = dtb_resize_index + 1
        framebuffer_end = framebuffer_start + len(expected_framebuffer_plan)
        self.assertEqual(
            commands[framebuffer_start:framebuffer_end],
            expected_framebuffer_plan,
        )
        self.assertEqual(names.count("bootargs-env"), 1)
        self.assertEqual(commands[framebuffer_end].name, "bootargs-env")
        for command in expected_framebuffer_plan:
            with self.subTest(command=command.name):
                self.assertEqual(names.count(command.name), 1)
        self.assertLess(names.index("framebuffer-verify"), names.index("booti"))
        self.assertEqual(names.count("booti"), 1)

    def test_headless_commands_remain_byte_for_byte_compatible(self) -> None:
        self.assertEqual(
            boot_commands(device_set=HEADLESS),
            boot_commands(),
        )

    def test_device_set_validation_rejects_invalid_shapes(self) -> None:
        for device_set, message in (
            (
                QemuDeviceSet(
                    "duplicate",
                    (DeviceKind.BOCHS_DISPLAY, DeviceKind.BOCHS_DISPLAY),
                ),
                "duplicate devices",
            ),
            (
                QemuDeviceSet("bad-framebuffer", (), BOCHS_XRGB8888),
                "framebuffer requires bochs-display",
            ),
            (
                QemuDeviceSet("bochs-without-framebuffer", (DeviceKind.BOCHS_DISPLAY,)),
                "bochs-display requires framebuffer",
            ),
        ):
            with self.subTest(device_set=device_set):
                with self.assertRaisesRegex(ValueError, message):
                    validate_registered_device_set(device_set)

    def test_megrez_basic_renders_its_fixed_devices_and_qmp_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            capture_root = Path(temporary) / "capture"
            capture_root.mkdir(mode=0o700)
            monitor_socket = capture_root / "qmp.sock"

            self.assertEqual(
                render_device_argv(
                    MEGREZ_BASIC,
                    RuntimeDevicePaths(
                        capture_root=capture_root,
                        monitor_socket=monitor_socket,
                    ),
                ),
                (
                    "-device",
                    "bochs-display,xres=1280,yres=1024",
                    "-qmp",
                    f"unix:{monitor_socket},server=on,wait=off",
                ),
            )
            self.assertEqual(
                qemu_argv(
                    uboot=Path("/tmp/u-boot"),
                    boot_disk=Path("/tmp/boot.ext4"),
                    device_set=MEGREZ_BASIC,
                    device_paths=RuntimeDevicePaths(
                        capture_root=capture_root,
                        monitor_socket=monitor_socket,
                    ),
                )[-4:],
                [
                    "-device",
                    "bochs-display,xres=1280,yres=1024",
                    "-qmp",
                    f"unix:{monitor_socket},server=on,wait=off",
                ],
            )

    def test_headless_rejects_all_runtime_paths(self) -> None:
        for field, path in (
            ("capture_root", Path("/tmp/capture")),
            ("monitor_socket", Path("/tmp/qmp.sock")),
            ("scratch_disk", Path("/tmp/scratch.img")),
            ("nvme_disk", Path("/tmp/nvme.img")),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "headless"):
                    render_device_argv(HEADLESS, RuntimeDevicePaths(**{field: path}))

    def test_framebuffer_paths_must_be_present_and_unused_disks_are_rejected(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "requires capture_root"):
            render_device_argv(MEGREZ_BASIC, None)

        with tempfile.TemporaryDirectory() as temporary:
            capture_root = Path(temporary) / "capture"
            capture_root.mkdir(mode=0o700)
            for field, path in (
                ("scratch_disk", Path("/tmp/scratch.img")),
                ("nvme_disk", Path("/tmp/nvme.img")),
            ):
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, "unused"):
                        render_device_argv(
                            MEGREZ_BASIC,
                            RuntimeDevicePaths(
                                capture_root=capture_root,
                                monitor_socket=capture_root / "qmp.sock",
                                **{field: path},
                            ),
                        )

    def test_framebuffer_paths_are_confined_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            capture_root = parent / "capture"
            capture_root.mkdir(mode=0o700)
            outside = parent / "outside"
            outside.mkdir()

            unsafe_paths = (
                RuntimeDevicePaths(
                    capture_root=Path("relative"),
                    monitor_socket=Path("/tmp/qmp.sock"),
                ),
                RuntimeDevicePaths(
                    capture_root=capture_root,
                    monitor_socket=Path("relative.sock"),
                ),
                RuntimeDevicePaths(
                    capture_root=capture_root,
                    monitor_socket=outside / "qmp.sock",
                ),
                RuntimeDevicePaths(
                    capture_root=capture_root,
                    monitor_socket=capture_root / "qmp,sock",
                ),
            )
            for paths in unsafe_paths:
                with self.subTest(paths=paths):
                    with self.assertRaises(ValueError):
                        render_device_argv(MEGREZ_BASIC, paths)

            bad_mode = parent / "bad-mode"
            bad_mode.mkdir()
            bad_mode.chmod(0o755)
            self.assertEqual(bad_mode.stat().st_mode & 0o777, 0o755)
            with self.assertRaisesRegex(ValueError, "mode 0700"):
                render_device_argv(
                    MEGREZ_BASIC,
                    RuntimeDevicePaths(
                        capture_root=bad_mode,
                        monitor_socket=bad_mode / "qmp.sock",
                    ),
                )

            root_link = parent / "capture-link"
            root_link.symlink_to(capture_root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "non-symlinked"):
                render_device_argv(
                    MEGREZ_BASIC,
                    RuntimeDevicePaths(
                        capture_root=root_link,
                        monitor_socket=root_link / "qmp.sock",
                    ),
                )

            socket_link = capture_root / "qmp.sock"
            socket_link.symlink_to(outside / "socket")
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                render_device_argv(
                    MEGREZ_BASIC,
                    RuntimeDevicePaths(
                        capture_root=capture_root,
                        monitor_socket=socket_link,
                    ),
                )

    def test_contract_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            tuple((kind.name, kind.value) for kind in DeviceKind),
            (
                ("BOCHS_DISPLAY", "bochs-display"),
                ("VIRTIO_KEYBOARD", "virtio-keyboard"),
                ("VIRTIO_RNG", "virtio-rng"),
                ("VIRTIO_NET", "virtio-net"),
                ("VIRTIO_GPU", "virtio-gpu"),
                ("SCRATCH_VIRTIO_BLOCK", "scratch-virtio-block"),
                ("NVME", "nvme"),
            ),
        )
        self.assertEqual(tuple(QemuMachine), (QemuMachine.VIRT, QemuMachine.SIFIVE_U))
        self.assertEqual(
            tuple(StorageTransport),
            (StorageTransport.VIRTIO_EXT4, StorageTransport.MMC_EXT4),
        )
        self.assertEqual(tuple(DtbProvider), (DtbProvider.GENERATED_PAYLOAD,))
        self.assertEqual(
            tuple(Fidelity),
            (
                Fidelity.VIRTUAL_PLATFORM,
                Fidelity.BOARD_MODEL,
                Fidelity.CONTRACT_APPROXIMATION,
            ),
        )
        self.assertEqual(
            tuple(ResultScope),
            (ResultScope.COMPLETE_BOOT, ResultScope.PROBE),
        )
        self.assertEqual(
            tuple(BootActionKind),
            (
                BootActionKind.WAIT_FOR_FIRMWARE,
                BootActionKind.WAIT_FOR_BOOTLOADER_PROMPT,
                BootActionKind.LOAD_ARTIFACTS,
                BootActionKind.SELECT_DEVICE_TREE,
                BootActionKind.SET_BOOT_ARGUMENTS,
                BootActionKind.REMOVE_DEVICE_TREE_PROPERTY,
                BootActionKind.BOOT_LINUX_IMAGE,
                BootActionKind.EXPECT_MILESTONE,
            ),
        )

    def test_registered_profile_is_a_frozen_three_part_composition(self) -> None:
        self.assertIs(GENERIC_SV39.machine, QEMU_VIRT)
        self.assertIs(GENERIC_SV39.boot_flow, UBOOT_BOOTI)
        self.assertIs(GENERIC_SV39.validation, ASTERINAS_USERSPACE_SMOKE)
        self.assertIsInstance(GENERIC_SV39.machine, MachineContract)
        self.assertIsInstance(GENERIC_SV39.boot_flow, BootFlow)
        self.assertIsInstance(GENERIC_SV39.validation, ValidationScenario)

        for value, field in (
            (GENERIC_SV39.machine, "memory"),
            (GENERIC_SV39.boot_flow, "name"),
            (GENERIC_SV39.validation, "terminal"),
            (GENERIC_SV39, "name"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, field, "changed")

    def test_generic_sv39_smp4_tcp_probe_uses_its_own_terminal(self) -> None:
        profile = profile_by_name("generic-sv39-smp4-tcp-probe")

        self.assertIs(profile.machine, QEMU_VIRT_SMP4)
        self.assertIs(profile.boot_flow, UBOOT_BOOTI)
        self.assertEqual(profile.validation.name, "megrez-tcp-probe")
        self.assertEqual(
            profile.validation.completion_line,
            b"ASTERINAS_GMAC_TCP_PROBE_READY",
        )
        self.assertEqual(
            profile.validation.milestones[-1].line,
            b"ASTERINAS_GMAC_TCP_PROBE_READY",
        )
        self.assertEqual(profile.machine.hart_count, 4)
        self.assertEqual(profile.machine.mmu_types, ("riscv,sv39",) * 4)
        self.assertEqual(
            profile.cpu,
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        )
        self.assertFalse(profile.requires_resource_gate)
        validate_registered_profile(profile)

    def test_existing_profiles_retain_their_public_policy(self) -> None:
        expected = {
            GENERIC_SV39: (
                "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
                "2G",
                0x8000_0000,
                1,
                "console=ttyS0 loglevel=info init=/init",
                "riscv,sv39",
                "svade",
                False,
                "zkr",
                False,
                Fidelity.VIRTUAL_PLATFORM,
            ),
            MEGREZ_SV48_SVADE_FAST: (
                "rv64,sv57=false,svpbmt=false,zkr=false,svadu=false,svade=true",
                "2G",
                0x8000_0000,
                4,
                "cpu_no_boost_1_6ghz loglevel=info init=/init",
                "riscv,sv48",
                "svade",
                True,
                None,
                False,
                Fidelity.CONTRACT_APPROXIMATION,
            ),
            MEGREZ_SV48_SVADU_FAST: (
                "rv64,sv57=false,svpbmt=false,zkr=false,svadu=true,svade=false",
                "2G",
                0x8000_0000,
                4,
                "cpu_no_boost_1_6ghz loglevel=info init=/init",
                "riscv,sv48",
                "svadu",
                True,
                None,
                False,
                Fidelity.CONTRACT_APPROXIMATION,
            ),
            MEGREZ_SV48_SLOW: (
                "rv64,sv57=false,svpbmt=false,zkr=false,svadu=false,svade=true",
                "16G",
                0x4_0000_0000,
                4,
                "cpu_no_boost_1_6ghz loglevel=info init=/init",
                "riscv,sv48",
                "svade",
                True,
                None,
                True,
                Fidelity.CONTRACT_APPROXIMATION,
            ),
        }

        for profile, fields in expected.items():
            with self.subTest(profile=profile.name):
                self.assertEqual(
                    (
                        profile.cpu,
                        profile.memory,
                        profile.memory_bytes,
                        profile.hart_count,
                        profile.bootargs,
                        profile.mmu_type,
                        profile.ad_extension,
                        profile.remove_rng_seed,
                        profile.required_random_source,
                        profile.requires_resource_gate,
                        profile.fidelity,
                    ),
                    fields,
                )
                validate_registered_profile(profile)

    def test_profile_validation_rejects_replaced_components(self) -> None:
        replacements = (
            replace(GENERIC_SV39, machine=replace(QEMU_VIRT, memory="1G")),
            replace(GENERIC_SV39, boot_flow=replace(UBOOT_BOOTI, name="other")),
            replace(
                GENERIC_SV39,
                validation=replace(
                    ASTERINAS_USERSPACE_SMOKE,
                    bootargs="console=ttyS0 init=/wrong",
                ),
            ),
        )

        for profile in replacements:
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, "registered"):
                    validate_registered_profile(profile)

    def test_profile_has_no_arbitrary_command_or_argument_escape_hatch(self) -> None:
        forbidden_fields = {
            "args",
            "commands",
            "extra_args",
            "hook",
            "script",
        }
        for contract_type in (
            MachineContract,
            BootFlow,
            ValidationScenario,
            QemuUbootProfile,
        ):
            with self.subTest(contract_type=contract_type.__name__):
                self.assertTrue(
                    forbidden_fields.isdisjoint(contract_type.__dataclass_fields__)
                )

    def test_complete_boot_scenario_has_a_fixed_userspace_terminal(self) -> None:
        scenario = ASTERINAS_USERSPACE_SMOKE

        self.assertEqual(scenario.scope, ResultScope.COMPLETE_BOOT)
        self.assertEqual(scenario.terminal, BootMilestone.USERSPACE_READY)
        self.assertEqual(scenario.audit_policy, AuditPolicy.ASTERINAS_STRICT)
        self.assertEqual(scenario.milestones[-1].stage, scenario.terminal)

    def test_ltp_profiles_are_registered_complete_boots(self) -> None:
        profiles = tuple(
            (profile_by_name(name), smp)
            for name, smp in (
                ("generic-sv39-ltp-smp1", 1),
                ("generic-sv39-ltp-smp4", 4),
            )
        )

        for profile, smp in profiles:
            with self.subTest(profile=profile.name):
                validate_registered_profile(profile)
                self.assertIs(profile.boot_flow, UBOOT_BOOTI)
                self.assertEqual(profile.machine.qemu_machine, QemuMachine.VIRT)
                self.assertEqual(profile.hart_count, smp)
                self.assertEqual(profile.memory, "2G")
                self.assertEqual(profile.validation.scope, ResultScope.COMPLETE_BOOT)
                self.assertEqual(
                    profile.validation.audit_policy,
                    AuditPolicy.REGISTERED_MILESTONES,
                )
                self.assertEqual(
                    profile.validation.completion_line,
                    b"__LTP_GATE_TERMINAL__",
                )
                self.assertEqual(
                    profile.validation.milestones[-1].line,
                    b"__LTP_GATE_TERMINAL__",
                )
                self.assertIn(
                    b"[BROK] LTP runner",
                    profile.validation.forbidden_markers,
                )
                argv = qemu_argv(
                    uboot=Path("/u-boot"),
                    boot_disk=Path("/boot.ext4"),
                    profile=profile,
                    snapshot_disk=False,
                )
                self.assertEqual(argv[argv.index("-smp") + 1], str(smp))

        self.assertIs(profiles[0][0].validation, profiles[1][0].validation)
        self.assertEqual(profiles[0][0].cpu, profiles[1][0].cpu)

    def test_sifive_asterinas_scenario_completes_in_userspace(self) -> None:
        scenario = SIFIVE_U_ASTERINAS_SMOKE.validation

        self.assertEqual(scenario.name, "sifive-u-asterinas-userspace-smoke")
        self.assertEqual(
            scenario.bootargs,
            "console=ttyS0 loglevel=info init=/init",
        )
        self.assertEqual(scenario.scope, ResultScope.COMPLETE_BOOT)
        self.assertEqual(scenario.terminal, BootMilestone.USERSPACE_READY)
        self.assertEqual(
            tuple(stage.stage.value for stage in scenario.milestones[-3:]),
            ("KernelReady", "RootfsReady", "UserspaceReady"),
        )
        self.assertEqual(
            tuple(stage.line for stage in scenario.milestones[-3:]),
            (
                b"OSTD initialized. Preparing components.",
                b"[kernel] rootfs is ready",
                ASTERINAS_USERSPACE_SMOKE.completion_line,
            ),
        )
        self.assertEqual(
            scenario.completion_line,
            ASTERINAS_USERSPACE_SMOKE.completion_line,
        )
        self.assertNotIn(scenario.completion_line, scenario.forbidden_markers)

    def test_sifive_userspace_profile_replaces_the_early_probe(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown QEMU U-Boot profile"):
            profile_by_name("sifive-u-asterinas-probe")

        profile = profile_by_name("sifive-u-asterinas-smoke")
        self.assertEqual(profile.validation.scope, ResultScope.COMPLETE_BOOT)
        self.assertEqual(
            profile.validation.terminal,
            BootMilestone.USERSPACE_READY,
        )

    def test_sifive_scenarios_own_the_slow_sd_command_deadline(self) -> None:
        virt = _parse_args(
            [
                "run",
                "--uboot", "/tmp/u-boot",
                "--boot-disk", "/tmp/boot.ext4",
                "--manifest", "/tmp/artifacts.json",
                "--serial-log", "/tmp/serial.log",
                "--marker-event", "/tmp/marker.txt",
                "--result", "/tmp/result.json",
            ]
        )
        sifive = _parse_args(
            [
                "run",
                "--profile", SIFIVE_U_ASTERINAS_SMOKE.name,
                "--uboot", "/tmp/u-boot.bin",
                "--boot-disk", "/tmp/boot.ext4",
                "--manifest", "/tmp/artifacts.json",
                "--serial-log", "/tmp/serial.log",
                "--marker-event", "/tmp/marker.txt",
                "--result", "/tmp/result.json",
            ]
        )

        self.assertEqual(virt.command_timeout, 10.0)
        self.assertEqual(sifive.command_timeout, 120.0)
        self.assertEqual(sifive.startup_timeout, 30.0)
        self.assertEqual(sifive.boot_timeout, 60.0)

    def test_sifive_smoke_and_linux_control_share_machine_and_flow(self) -> None:
        self.assertIs(SIFIVE_U_ASTERINAS_SMOKE.machine, SIFIVE_U)
        self.assertIs(SIFIVE_U_LINUX_REFERENCE.machine, SIFIVE_U)
        self.assertIs(
            SIFIVE_U_ASTERINAS_SMOKE.boot_flow,
            SIFIVE_U_LINUX_REFERENCE.boot_flow,
        )
        self.assertIsNot(
            SIFIVE_U_ASTERINAS_SMOKE.validation,
            SIFIVE_U_LINUX_REFERENCE.validation,
        )
        self.assertEqual(
            SIFIVE_U_LINUX_REFERENCE.validation.completion_line,
            b"ASTERINAS_LINUX_REFERENCE_READY",
        )
        self.assertEqual(
            SIFIVE_U_LINUX_REFERENCE.validation.scope,
            ResultScope.COMPLETE_BOOT,
        )

    def test_generated_dtb_evidence_binds_the_shared_machine_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dtb = directory / "machine.dtb"
            dtb.write_bytes(b"fixed machine DTB")
            identity = artifact_identity(dtb)
            audit = GeneratedDtbAudit(
                machine=SIFIVE_U.name,
                cpu_ids=tuple(range(5)),
                mmu_types=SIFIVE_U.mmu_types,
                memory=range(0x8000_0000, 0x1_0000_0000),
                ad_extension=SIFIVE_U.ad_extension,
                rng_seed_present=False,
                sha256=identity.sha256,
            )
            record = directory / "audit.json"
            record.write_text(json.dumps(generated_dtb_audit_record(audit, dtb)))

            for profile in (
                SIFIVE_U_ASTERINAS_SMOKE,
                SIFIVE_U_LINUX_REFERENCE,
            ):
                with self.subTest(profile=profile.name):
                    restored = load_generated_dtb_audit(
                        record,
                        profile=profile,
                        expected_size=identity.size,
                        expected_crc32=identity.crc32,
                    )
                    self.assertEqual(restored.machine, SIFIVE_U.name)

    def test_default_qemu_arguments_remain_byte_for_byte_compatible(self) -> None:
        self.assertEqual(
            qemu_argv(
                uboot=Path("/tmp/u-boot"),
                boot_disk=Path("/tmp/boot.ext4"),
            ),
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

    def test_sifive_u_uses_native_machine_harts_and_sd_transport(self) -> None:
        self.assertIs(SIFIVE_U_ASTERINAS_SMOKE.machine, SIFIVE_U)

        argv = qemu_argv(
            uboot=Path("/tmp/u-boot.bin"),
            boot_disk=Path("/tmp/boot.ext4"),
            profile=SIFIVE_U_ASTERINAS_SMOKE,
        )

        self.assertEqual(argv[argv.index("-machine") + 1], "sifive_u")
        self.assertEqual(argv[argv.index("-smp") + 1], "5")
        self.assertEqual(argv[argv.index("-m") + 1], "2G")
        self.assertNotIn("-cpu", argv)
        self.assertNotIn("-device", argv)
        self.assertIn("file=/tmp/boot.ext4,if=sd,format=raw", argv)

    def test_common_boot_flow_renders_only_the_registered_transport(self) -> None:
        virt = tuple(command.text for command in boot_commands())
        sifive = tuple(
            command.text
            for command in boot_commands(profile=SIFIVE_U_ASTERINAS_SMOKE)
        )

        self.assertIn("virtio scan", virt)
        self.assertIn("ext4ls virtio 0:0 /", virt)
        self.assertIn(
            "ext4load virtio 0:0 0x88000000 /qemu-virt.dtb",
            virt,
        )
        self.assertFalse(any("mmc" in command for command in virt))

        self.assertIn("mmc dev 0", sifive)
        self.assertIn("mmc rescan", sifive)
        self.assertIn("ext4ls mmc 0:0 /", sifive)
        self.assertIn(
            "ext4load mmc 0:0 0x88000000 /qemu-sifive-u.dtb",
            sifive,
        )
        self.assertFalse(any("virtio" in command for command in sifive))
        self.assertEqual(sum(command.startswith("booti ") for command in sifive), 1)

    def test_generated_dtb_command_uses_the_registered_machine(self) -> None:
        self.assertEqual(
            generated_dtb_qemu_argv(
                profile=GENERIC_SV39,
                dtb=Path("/tmp/virt.dtb"),
            ),
            [
                "qemu-system-riscv64",
                "-machine",
                "virt,dumpdtb=/tmp/virt.dtb",
                "-cpu",
                GENERIC_SV39.cpu,
                "-m",
                "2G",
                "-smp",
                "1",
            ],
        )
        self.assertEqual(
            generated_dtb_qemu_argv(
                profile=SIFIVE_U_ASTERINAS_SMOKE,
                dtb=Path("/tmp/sifive.dtb"),
            ),
            [
                "qemu-system-riscv64",
                "-machine",
                "sifive_u,dumpdtb=/tmp/sifive.dtb",
                "-m",
                "2G",
                "-smp",
                "5",
            ],
        )

    def test_structured_drive_path_rejects_a_comma_for_every_transport(self) -> None:
        for profile in (GENERIC_SV39, SIFIVE_U_ASTERINAS_SMOKE):
            with self.subTest(profile=profile.name):
                with self.assertRaisesRegex(ValueError, "must not contain a comma"):
                    qemu_argv(
                        uboot=Path("/tmp/u-boot"),
                        boot_disk=Path("/tmp/boot,unsafe.ext4"),
                        profile=profile,
                    )

    def test_existing_runner_exposes_only_typed_preparation_fields(self) -> None:
        expected = {
            "qemu-machine": "sifive_u",
            "storage-transport": "mmc-ext4",
            "dtb-filename": "qemu-sifive-u.dtb",
            "uboot-defconfig": "sifive_unleashed_defconfig",
            "uboot-binary": "u-boot.bin",
            "uboot-build-mode": "board-smode",
        }
        for field, value in expected.items():
            with self.subTest(field=field):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(RUNNER),
                        "profile-field",
                        "--profile",
                        SIFIVE_U_ASTERINAS_SMOKE.name,
                        "--field",
                        field,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{value}\n")

    def test_milestone_tracker_records_only_monotonic_progress(self) -> None:
        tracker = MilestoneTracker(ASTERINAS_USERSPACE_SMOKE.milestones)

        tracker.observe(b"OpenSBI v1.7\n", elapsed_seconds=0.1)
        tracker.observe(b"U-Boot 2026.07\n", elapsed_seconds=0.2)
        tracker.observe(b"U-Boot 2026.07\n", elapsed_seconds=0.25)
        tracker.observe(b"ASTERINAS_PRE_BOOTI\n", elapsed_seconds=0.3)
        tracker.observe(b"ASTERINAS_PRE_BOOTI\n", elapsed_seconds=0.4)

        self.assertEqual(
            tuple(event.stage for event in tracker.events),
            (
                BootMilestone.FIRMWARE_READY,
                BootMilestone.BOOTLOADER_READY,
                BootMilestone.ARTIFACTS_LOADED,
            ),
        )
        self.assertEqual(
            tuple(event.elapsed_seconds for event in tracker.events),
            (0.1, 0.25, 0.4),
        )
        self.assertEqual(tracker.last_stage, BootMilestone.ARTIFACTS_LOADED)

    def test_milestone_tracker_waits_for_every_required_occurrence(self) -> None:
        expectation = MilestoneExpectation(
            BootMilestone.BOOTLOADER_READY,
            b"U-Boot 2026.07",
            expected_occurrences=2,
        )
        tracker = MilestoneTracker((expectation,))

        tracker.observe(b"U-Boot 2026.07\n", elapsed_seconds=0.1)
        self.assertEqual(tracker.events, ())

        tracker.observe(b"U-Boot 2026.07\n", elapsed_seconds=0.2)
        self.assertEqual(len(tracker.events), 1)
        self.assertEqual(tracker.events[0].elapsed_seconds, 0.2)

    def test_cli_rejects_nonfinite_or_nonpositive_timeouts(self) -> None:
        prefix = [
            "run",
            "--uboot", "/tmp/u-boot",
            "--boot-disk", "/tmp/boot.ext4",
            "--manifest", "/tmp/artifacts.json",
            "--serial-log", "/tmp/serial.log",
            "--marker-event", "/tmp/marker.txt",
            "--result", "/tmp/result.json",
        ]
        for option, value in (
            ("--startup-timeout", "0"),
            ("--command-timeout", "nan"),
            ("--boot-timeout", "inf"),
            ("--termination-grace", "-1"),
        ):
            with self.subTest(option=option, value=value):
                with self.assertRaises(SystemExit):
                    _parse_args([*prefix, option, value])

    def test_milestone_tracker_rejects_a_later_stage_before_the_next_stage(
        self,
    ) -> None:
        tracker = MilestoneTracker(ASTERINAS_USERSPACE_SMOKE.milestones)

        with self.assertRaisesRegex(ValueError, "out-of-order milestone"):
            tracker.observe(b"Starting kernel ...\n", elapsed_seconds=0.1)

        self.assertEqual(tracker.events, ())
        self.assertIsNone(tracker.last_stage)

    def test_ktap_distinguishes_complete_boot_and_timeout(self) -> None:
        cases = (
            (
                SimpleNamespace(
                    profile="generic-sv39",
                    status=RunStatus.PASS.value,
                    terminal_classification=(
                        TerminalClassification.BOOT_COMPLETED.value
                    ),
                    scope=ResultScope.COMPLETE_BOOT.value,
                    expected_terminal=BootMilestone.USERSPACE_READY.value,
                    fidelity=Fidelity.VIRTUAL_PLATFORM.value,
                    session=SimpleNamespace(timed_out=False),
                ),
                "ok 1 - generic-sv39",
            ),
            (
                SimpleNamespace(
                    profile="sifive-u-asterinas-smoke",
                    status=RunStatus.PASS.value,
                    terminal_classification=TerminalClassification.BOOT_COMPLETED.value,
                    scope=ResultScope.COMPLETE_BOOT.value,
                    expected_terminal=BootMilestone.USERSPACE_READY.value,
                    fidelity=Fidelity.BOARD_MODEL.value,
                    session=SimpleNamespace(timed_out=False),
                ),
                "ok 1 - sifive-u-asterinas-smoke",
            ),
            (
                SimpleNamespace(
                    profile="sifive-u-asterinas-smoke",
                    status=RunStatus.FAIL.value,
                    terminal_classification=TerminalClassification.INCOMPLETE.value,
                    scope=ResultScope.COMPLETE_BOOT.value,
                    expected_terminal=BootMilestone.USERSPACE_READY.value,
                    fidelity=Fidelity.BOARD_MODEL.value,
                    session=SimpleNamespace(timed_out=True),
                ),
                "not ok 1 - sifive-u-asterinas-smoke # TIMEOUT",
            ),
        )

        for result, expected_line in cases:
            with self.subTest(expected_line=expected_line):
                summary = ktap_summary(result)
                self.assertTrue(summary.startswith("KTAP version 1\n1..1\n"))
                self.assertIn(expected_line, summary.splitlines())
                self.assertIn(f"# scope: {result.scope}", summary)
                self.assertIn(
                    f"# terminal-classification: {result.terminal_classification}",
                    summary,
                )

    def test_run_status_distinguishes_test_failure_from_harness_error(self) -> None:
        self.assertEqual(
            _run_status(True, SimpleNamespace(cleanup_complete=True, failure=None)),
            RunStatus.PASS,
        )
        self.assertEqual(
            _run_status(
                False,
                SimpleNamespace(cleanup_complete=True, failure="boot-timeout"),
            ),
            RunStatus.FAIL,
        )
        self.assertEqual(
            _run_status(
                False,
                SimpleNamespace(
                    cleanup_complete=True,
                    failure="process-error:bootloader-prompt",
                ),
            ),
            RunStatus.ERROR,
        )
        self.assertEqual(
            _run_status(False, SimpleNamespace(cleanup_complete=False, failure=None)),
            RunStatus.ERROR,
        )

    def test_artifact_manifest_records_each_caller_provided_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "Image"
            dtb = directory / "machine.dtb"
            initrd = directory / "initramfs.cpio"
            kernel.write_bytes(self._linux_image())
            dtb.write_bytes(b"dtb bytes")
            initrd.write_bytes(b"initrd bytes")

            artifacts = artifact_expectations_from_paths(
                kernel=kernel,
                dtb=dtb,
                initrd=initrd,
            )

        self.assertEqual(
            artifacts.kernel_sha256,
            "948af20de762380321d412ebdb8a2fc9e44c21b1602ca8e5b5b3dd47d3746c89",
        )
        self.assertEqual(
            artifacts.dtb_sha256,
            "3108c9b43aeea90a7c7d4b347b748fd48a923026c7461c26fca12c3fef821617",
        )
        self.assertEqual(
            artifacts.initrd_sha256,
            "3e80041bda6e34a9397ae625c212916b0520a14975ca58a8f5d93c4e5b50d184",
        )

    def test_boot_disk_payload_sha256_must_match_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kernel = directory / "Image"
            dtb = directory / "machine.dtb"
            initrd = directory / "initramfs.cpio"
            kernel.write_bytes(self._linux_image())
            dtb.write_bytes(b"dtb bytes")
            initrd.write_bytes(b"initrd bytes")
            expected = artifact_expectations_from_paths(
                kernel=kernel,
                dtb=dtb,
                initrd=initrd,
            )
            payloads = {
                "/asterinas.booti": kernel,
                "/qemu-sifive-u.dtb": dtb,
                "/initramfs.cpio.gz": initrd,
            }

            def extract(argv: list[str], **_kwargs: object) -> None:
                _dump, _preserve, source, destination = argv[2].split()
                shutil.copyfile(payloads[source], destination)

            with mock.patch(
                "qemu_uboot_artifacts.subprocess.run",
                side_effect=extract,
            ):
                self.assertEqual(
                    verify_boot_disk_artifacts(
                        boot_disk=directory / "boot.ext4",
                        dtb_filename="qemu-sifive-u.dtb",
                        expected=expected,
                    ),
                    expected,
                )
                for field in ("kernel_sha256", "dtb_sha256", "initrd_sha256"):
                    with self.subTest(field=field):
                        with self.assertRaisesRegex(ValueError, "payload identities"):
                            verify_boot_disk_artifacts(
                                boot_disk=directory / "boot.ext4",
                                dtb_filename="qemu-sifive-u.dtb",
                                expected=replace(expected, **{field: "0" * 64}),
                            )


class ThirdBoardContractTests(unittest.TestCase):
    """The third board is a CONTRACT_APPROXIMATION: the payload DTB claims a
    different SoC while QEMU runs the virt machine."""

    def test_third_board_profile_is_registered(self) -> None:
        profile = profile_by_name("third-board-asterinas-smoke")

        self.assertEqual(profile.name, "third-board-asterinas-smoke")
        self.assertEqual(profile.machine.name, "third-board")
        self.assertEqual(profile.machine.qemu_machine, QemuMachine.VIRT)
        self.assertEqual(profile.machine.fidelity, Fidelity.CONTRACT_APPROXIMATION)
        self.assertEqual(
            profile.machine.root_compatible_override,
            ("starfive,visionfive-v2", "riscv-virtio"),
        )

    def test_third_board_uses_generated_payload_dtb(self) -> None:
        profile = profile_by_name("third-board-asterinas-smoke")

        self.assertEqual(profile.machine.dtb_provider, DtbProvider.GENERATED_PAYLOAD)
        self.assertEqual(profile.machine.dtb_filename, "qemu-third-board.dtb")
        self.assertEqual(
            profile.machine.storage_transport,
            StorageTransport.VIRTIO_EXT4,
        )
        # A non-override machine must keep its default DTB untouched.
        self.assertIsNone(profile_by_name("sifive-u-asterinas-smoke").machine.root_compatible_override)

    def test_third_board_scenario_completes_in_userspace(self) -> None:
        scenario = profile_by_name("third-board-asterinas-smoke").validation

        self.assertEqual(scenario.name, "third-board-asterinas-userspace-smoke")
        self.assertEqual(scenario.scope, ResultScope.COMPLETE_BOOT)
        self.assertEqual(scenario.terminal, BootMilestone.USERSPACE_READY)
        self.assertEqual(
            tuple(stage.line for stage in scenario.milestones[-3:]),
            (
                b"OSTD initialized. Preparing components.",
                b"[kernel] rootfs is ready",
                ASTERINAS_USERSPACE_SMOKE.completion_line,
            ),
        )
        self.assertNotIn(scenario.completion_line, scenario.forbidden_markers)


if __name__ == "__main__":
    unittest.main()
