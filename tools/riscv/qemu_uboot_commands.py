"""QEMU configuration and guarded U-Boot command planning."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qemu_uboot_artifacts import (
    DEFAULT_ARTIFACTS,
    DTB_EXPANSION_SIZE,
    DTB_LOAD_ADDRESS,
    INITRD_LOAD_ADDRESS,
    KERNEL_LOAD_ADDRESS,
    ArtifactExpectations,
)
from qemu_uboot_devices import (
    HEADLESS,
    QemuDeviceSet,
    RuntimeDevicePaths,
    render_device_argv,
    validate_registered_device_set,
)
from qemu_uboot_profiles import (
    GENERIC_SV39,
    BootActionKind,
    QemuUbootProfile,
    StorageTransport,
    require_profile_launch_allowed,
    validate_registered_profile,
)
from qemu_uboot_variants import (
    FIRST_PROCESS_CONSOLE_LOSS,
    QemuUbootVariant,
    effective_bootargs as variant_effective_bootargs,
    validate_registered_variant,
)


QEMU_CPU = GENERIC_SV39.cpu
QEMU_MEMORY = GENERIC_SV39.memory
KERNEL_BOOTARGS = GENERIC_SV39.bootargs
USERSPACE_MARKER_TEXT = ">>> Hello from RISC-V userspace on Asterinas! <<<"
USERSPACE_MARKER = USERSPACE_MARKER_TEXT.encode()
BOOTI_COMMAND = (
    f"booti {KERNEL_LOAD_ADDRESS:#x} "
    f"{INITRD_LOAD_ADDRESS:#x}:${{initrd_size}} {DTB_LOAD_ADDRESS:#x}"
)
STALE_BOOTARGS = "cpu_no_boost_1_6ghz"
FIRST_PROCESS_DIAGNOSTIC_BOOTARG = "asterinas.first_process_diag=1"
RNG_SEED_REMOVE_COMMAND = (
    "if fdt get value aster_rng_seed /chosen rng-seed; then fdt rm /chosen rng-seed; fi"
)


class BootScenario(str, Enum):
    """Named positive and expected-negative U-Boot handoff scenarios."""

    POSITIVE = "positive"
    STALE_BOOTARGS = "stale-bootargs"
    REGISTERED_CONSOLE_SUPPRESSION = "registered-console-suppression"
    FIRST_PROCESS_CONSOLE_LOSS = "first-process-console-loss"


def qemu_version(executable: str = "qemu-system-riscv64") -> str:
    """Return the first QEMU version line used for run provenance."""

    result = subprocess.run(
        [executable, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    lines = result.stdout.splitlines()
    if not lines:
        raise RuntimeError("QEMU returned empty version output")
    return lines[0]


@dataclass(frozen=True)
class BootCommand:
    """One ordered U-Boot command and the output that proves it completed."""

    name: str
    text: str
    expected_output: str


def qemu_argv(
    *,
    uboot: Path,
    boot_disk: Path,
    profile: QemuUbootProfile = GENERIC_SV39,
    slow_permit: object | None = None,
    guest_reboot: bool = False,
    snapshot_disk: bool = False,
    device_set: QemuDeviceSet = HEADLESS,
    device_paths: RuntimeDevicePaths | None = None,
) -> list[str]:
    """Construct the selected guarded QEMU U-Boot command line."""

    validate_registered_device_set(device_set)
    argv = _base_qemu_argv(
        uboot=uboot,
        boot_disk=boot_disk,
        profile=profile,
        slow_permit=slow_permit,
        guest_reboot=guest_reboot,
        snapshot_disk=snapshot_disk,
    )
    argv.extend(render_device_argv(device_set, device_paths))
    return argv


def _base_qemu_argv(
    *,
    uboot: Path,
    boot_disk: Path,
    profile: QemuUbootProfile = GENERIC_SV39,
    slow_permit: object | None = None,
    guest_reboot: bool = False,
    snapshot_disk: bool = False,
) -> list[str]:
    """Construct the pre-device QEMU U-Boot command line."""

    if "," in os.fspath(boot_disk):
        raise ValueError("QEMU boot disk path must not contain a comma")

    require_profile_launch_allowed(
        profile,
        slow_permit=slow_permit,
    )

    argv = [
        "qemu-system-riscv64",
        "-machine",
        profile.machine.qemu_machine.value,
    ]
    if profile.machine.cpu is not None:
        argv.extend(("-cpu", profile.machine.cpu))
    argv.extend(
        [
        "-m",
        profile.memory,
        "-smp",
        str(profile.hart_count),
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "stdio",
        ]
    )
    if not guest_reboot:
        argv.append("-no-reboot")
    argv.extend(("-kernel", str(uboot)))
    if profile.machine.storage_transport is StorageTransport.VIRTIO_EXT4:
        drive = f"if=none,format=raw,file={boot_disk},id=bootdisk"
        if guest_reboot or snapshot_disk:
            drive += ",snapshot=on"
        argv.extend(
            (
                "-drive",
                drive,
                "-device",
                "virtio-blk-device,drive=bootdisk",
            )
        )
    elif profile.machine.storage_transport is StorageTransport.MMC_EXT4:
        drive = f"file={boot_disk},if=sd,format=raw"
        if guest_reboot or snapshot_disk:
            drive += ",snapshot=on"
        argv.extend(("-drive", drive))
    else:
        raise AssertionError("unhandled registered storage transport")
    return argv


def _filesystem_plan(profile: QemuUbootProfile) -> tuple[list[BootCommand], str]:
    """Return built-in discovery actions and the U-Boot filesystem selector."""

    transport = profile.machine.storage_transport
    if transport is StorageTransport.VIRTIO_EXT4:
        return [BootCommand("virtio-scan", "virtio scan", "=>")], "virtio 0:0"
    if transport is StorageTransport.MMC_EXT4:
        return [
            BootCommand("mmc-select", "mmc dev 0", "=>"),
            BootCommand("mmc-rescan", "mmc rescan", "=>"),
        ], "mmc 0:0"
    raise AssertionError("unhandled registered storage transport")


def boot_commands(
    artifacts: ArtifactExpectations = DEFAULT_ARTIFACTS,
    *,
    profile: QemuUbootProfile = GENERIC_SV39,
    scenario: BootScenario = BootScenario.POSITIVE,
    bootargs_override: str | None = None,
    variant: QemuUbootVariant | None = None,
) -> tuple[BootCommand, ...]:
    """Return the guarded command sequence for a registered U-Boot flow."""

    validate_registered_profile(profile)
    if profile.boot_flow.actions != tuple(BootActionKind):
        raise ValueError("registered U-Boot flow has an unsupported action order")

    if scenario is BootScenario.REGISTERED_CONSOLE_SUPPRESSION:
        if variant is not None:
            raise ValueError("registered-console-suppression does not accept a variant")
        validate_registered_profile(profile)
        if profile.name != FIRST_PROCESS_CONSOLE_LOSS.base_profile_name:
            raise ValueError(
                "registered-console-suppression requires profile "
                f"{FIRST_PROCESS_CONSOLE_LOSS.base_profile_name}"
            )
        effective_bootargs = f"{profile.bootargs} {FIRST_PROCESS_DIAGNOSTIC_BOOTARG}"
    elif scenario is BootScenario.FIRST_PROCESS_CONSOLE_LOSS:
        if variant is None:
            raise ValueError("first-process-console-loss requires its fixed variant")
        validate_registered_variant(variant)
        if variant is not FIRST_PROCESS_CONSOLE_LOSS:
            raise ValueError("first-process-console-loss requires its fixed variant")
        effective_bootargs = variant_effective_bootargs(profile, variant)
    elif variant is not None:
        raise ValueError("variants require the first-process-console-loss scenario")
    elif scenario is BootScenario.STALE_BOOTARGS and profile == GENERIC_SV39:
        raise ValueError("stale-bootargs requires a Megrez profile")
    if bootargs_override is not None and scenario is not BootScenario.POSITIVE:
        raise ValueError("bootargs override requires the positive scenario")
    if bootargs_override is not None and (
        not bootargs_override.strip()
        or any(character in bootargs_override for character in '"\\;\r\n')
    ):
        raise ValueError("bootargs override contains an unsafe character")
    kernel_address = f"{KERNEL_LOAD_ADDRESS:#x}"
    dtb_address = f"{DTB_LOAD_ADDRESS:#x}"
    initrd_address = f"{INITRD_LOAD_ADDRESS:#x}"
    if scenario not in {
        BootScenario.REGISTERED_CONSOLE_SUPPRESSION,
        BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
    }:
        effective_bootargs = (
            profile.bootargs if bootargs_override is None else bootargs_override
        )
    environment_bootargs = (
        STALE_BOOTARGS
        if scenario is BootScenario.STALE_BOOTARGS
        else effective_bootargs
    )
    discovery_commands, filesystem = _filesystem_plan(profile)
    commands = [
        BootCommand("version", "version", "U-Boot 2026.07"),
        BootCommand("memory-layout", "bdinfo", "reserved[2]"),
        *discovery_commands,
        BootCommand("filesystem", f"ext4ls {filesystem} /", "asterinas.booti"),
        BootCommand(
            "kernel-load",
            f"ext4load {filesystem} {kernel_address} /asterinas.booti",
            f"{artifacts.kernel_size} bytes read",
        ),
        BootCommand("kernel-size-save", "setenv aster_size ${filesize}", "=>"),
        BootCommand(
            "kernel-size",
            "echo ASTER_KERNEL_SIZE=${aster_size}",
            f"{artifacts.kernel_size:x}",
        ),
        BootCommand(
            "kernel-crc",
            f"crc32 {kernel_address} ${{aster_size}}",
            artifacts.kernel_crc32,
        ),
        BootCommand(
            "dtb-load",
            f"ext4load {filesystem} {dtb_address} /{profile.machine.dtb_filename}",
            f"{artifacts.dtb_size} bytes read",
        ),
        BootCommand("dtb-size-save", "setenv dtb_size ${filesize}", "=>"),
        BootCommand(
            "dtb-crc",
            f"crc32 {dtb_address} ${{dtb_size}}",
            artifacts.dtb_crc32,
        ),
        BootCommand("dtb-select", f"fdt addr {dtb_address}", "Working FDT set"),
        BootCommand("dtb-resize", f"fdt resize {DTB_EXPANSION_SIZE:#x}", "=>"),
        BootCommand(
            "bootargs-env",
            f'setenv bootargs "{environment_bootargs}"',
            "=>",
        ),
        BootCommand(
            "bootargs-env-proof",
            "printenv bootargs",
            f"bootargs={environment_bootargs}",
        ),
        BootCommand(
            "bootargs-dtb",
            f'fdt set /chosen bootargs "{effective_bootargs}"',
            "=>",
        ),
    ]
    if (
        BootActionKind.REMOVE_DEVICE_TREE_PROPERTY in profile.boot_flow.actions
        and profile.remove_rng_seed
    ):
        commands.append(BootCommand("rng-seed-remove", RNG_SEED_REMOVE_COMMAND, "=>"))
    commands.extend(
        [
            BootCommand(
                "bootargs-dtb-proof",
                "fdt print /chosen",
                effective_bootargs,
            ),
            BootCommand(
                "initrd-load",
                f"ext4load {filesystem} {initrd_address} /initramfs.cpio.gz",
                f"{artifacts.initrd_size} bytes read",
            ),
            BootCommand("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
            BootCommand(
                "initrd-size",
                "echo ASTER_INITRD_SIZE=${initrd_size}",
                f"{artifacts.initrd_size:x}",
            ),
            BootCommand(
                "initrd-crc",
                f"crc32 {initrd_address} ${{initrd_size}}",
                artifacts.initrd_crc32,
            ),
            BootCommand("pre-booti", "echo ASTERINAS_PRE_BOOTI", "ASTERINAS_PRE_BOOTI"),
            BootCommand("booti", BOOTI_COMMAND, "Starting kernel ..."),
        ]
    )
    booti_count = sum(command.text.startswith("booti ") for command in commands)
    if booti_count != 1:
        raise ValueError(f"expected exactly one booti command, got {booti_count}")
    return tuple(commands)
