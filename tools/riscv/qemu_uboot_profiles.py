"""Immutable QEMU profiles for guarded RISC-V U-Boot runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType

from megrez_contract import MegrezContract
from qemu_uboot_gate import has_valid_slow_run_permit


class QemuMachine(str, Enum):
    """QEMU RISC-V machine models supported by the guarded renderer."""

    VIRT = "virt"
    SIFIVE_U = "sifive_u"


class StorageTransport(str, Enum):
    """U-Boot filesystem transports with a built-in command renderer."""

    VIRTIO_EXT4 = "virtio-ext4"
    MMC_EXT4 = "mmc-ext4"


class DtbProvider(str, Enum):
    """How the kernel payload DTB is obtained for the initial profiles."""

    GENERATED_PAYLOAD = "generated-payload"


class UbootBuildMode(str, Enum):
    """Reviewed U-Boot configuration transformations supported by preparation."""

    STANDARD_SMODE = "standard-smode"
    BOARD_SMODE = "board-smode"


class Fidelity(str, Enum):
    """What relationship a machine contract has to physical hardware."""

    VIRTUAL_PLATFORM = "virtual-platform"
    BOARD_MODEL = "board-model"
    CONTRACT_APPROXIMATION = "contract-approximation"


class ResultScope(str, Enum):
    """Whether success proves a complete boot or one reviewed boundary."""

    COMPLETE_BOOT = "complete-boot"
    PROBE = "probe"


class BootMilestone(str, Enum):
    """Common observable stages shared by registered boot scenarios."""

    EMULATOR_STARTED = "EmulatorStarted"
    FIRMWARE_READY = "FirmwareReady"
    BOOTLOADER_READY = "BootloaderReady"
    ARTIFACTS_LOADED = "ArtifactsLoaded"
    KERNEL_HANDOFF = "KernelHandoff"
    KERNEL_EARLY = "KernelEarly"
    KERNEL_READY = "KernelReady"
    ROOTFS_READY = "RootfsReady"
    USERSPACE_READY = "UserspaceReady"


class BootActionKind(str, Enum):
    """Closed action vocabulary accepted by the U-Boot flow renderer."""

    WAIT_FOR_FIRMWARE = "wait-for-firmware"
    WAIT_FOR_BOOTLOADER_PROMPT = "wait-for-bootloader-prompt"
    LOAD_ARTIFACTS = "load-artifacts"
    SELECT_DEVICE_TREE = "select-device-tree"
    SET_BOOT_ARGUMENTS = "set-boot-arguments"
    REMOVE_DEVICE_TREE_PROPERTY = "remove-device-tree-property"
    BOOT_LINUX_IMAGE = "boot-linux-image"
    EXPECT_MILESTONE = "expect-milestone"


class AuditPolicy(str, Enum):
    """Built-in serial audit selected by a registered scenario."""

    ASTERINAS_STRICT = "asterinas-strict"
    REGISTERED_MILESTONES = "registered-milestones"


_MILESTONE_ORDER = {stage: index for index, stage in enumerate(BootMilestone)}


@dataclass(frozen=True)
class MilestoneExpectation:
    """One exact line that proves a common boot milestone."""

    stage: BootMilestone
    line: bytes
    expected_occurrences: int = 1

    def __post_init__(self) -> None:
        if not self.line or b"\n" in self.line or b"\r" in self.line:
            raise ValueError("milestone line must be one non-empty unterminated line")
        if self.expected_occurrences < 1:
            raise ValueError("milestone occurrence count must be positive")


@dataclass(frozen=True)
class MachineContract:
    """One immutable QEMU machine and hardware-validation envelope."""

    name: str
    qemu_machine: QemuMachine
    cpu: str | None
    memory: str
    memory_bytes: int
    hart_count: int
    mmu_types: tuple[str | None, ...]
    storage_transport: StorageTransport
    dtb_provider: DtbProvider
    dtb_filename: str
    uboot_defconfig: str
    uboot_binary: str
    uboot_build_mode: UbootBuildMode
    fidelity: Fidelity
    ad_extension: str
    remove_rng_seed: bool
    required_random_source: str | None
    requires_resource_gate: bool
    provenance: str
    # Optional override of the generated DTB's root `compatible` list. When
    # set, the prepared payload DTB claims a different SoC than the QEMU
    # machine's default (CONTRACT_APPROXIMATION: validate the kernel against
    # a third board's DTB while still running on a supported machine).
    root_compatible_override: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.memory or self.memory_bytes <= 0:
            raise ValueError("machine contract identity and memory must be non-empty")
        if self.hart_count <= 0 or len(self.mmu_types) != self.hart_count:
            raise ValueError("machine contract MMU entries must match its hart count")
        if not self.dtb_filename or "/" in self.dtb_filename:
            raise ValueError("machine contract DTB filename must be a basename")
        if not self.uboot_defconfig or not self.uboot_binary:
            raise ValueError("machine contract U-Boot selection must be non-empty")
        if not self.provenance:
            raise ValueError("machine contract provenance must be non-empty")


@dataclass(frozen=True)
class BootFlow:
    """One ordered flow assembled only from built-in action kinds."""

    name: str
    actions: tuple[BootActionKind, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.actions:
            raise ValueError("boot flow identity and actions must be non-empty")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("boot flow action kinds must not repeat")


@dataclass(frozen=True)
class ValidationScenario:
    """One fixed guest expectation and its externally reported scope."""

    name: str
    bootargs: str
    scope: ResultScope
    milestones: tuple[MilestoneExpectation, ...]
    terminal: BootMilestone
    completion_line: bytes
    forbidden_markers: tuple[bytes, ...]
    audit_policy: AuditPolicy
    startup_timeout: float
    command_timeout: float
    boot_timeout: float
    post_terminal_timeout: float

    def __post_init__(self) -> None:
        if not self.name or not self.bootargs:
            raise ValueError(
                "validation scenario identity and bootargs must be non-empty"
            )
        if not self.milestones or self.milestones[-1].stage is not self.terminal:
            raise ValueError("validation scenario terminal must be its last milestone")
        stages = tuple(item.stage for item in self.milestones)
        if len(set(stages)) != len(stages):
            raise ValueError("validation scenario milestones must not repeat")
        if any(
            _MILESTONE_ORDER[current] >= _MILESTONE_ORDER[following]
            for current, following in zip(stages, stages[1:])
        ):
            raise ValueError("validation scenario milestones must move forward")
        if self.scope is ResultScope.COMPLETE_BOOT:
            if self.terminal is not BootMilestone.USERSPACE_READY:
                raise ValueError("complete-boot scenario must reach UserspaceReady")
        elif self.terminal is BootMilestone.USERSPACE_READY:
            raise ValueError("probe scenario must stop before UserspaceReady")
        if self.completion_line != self.milestones[-1].line:
            raise ValueError("completion line must prove the terminal milestone")
        if any(not marker for marker in self.forbidden_markers):
            raise ValueError("forbidden markers must be non-empty")
        if min(self.startup_timeout, self.command_timeout, self.boot_timeout) <= 0:
            raise ValueError("validation scenario timeouts must be positive")
        if self.post_terminal_timeout < 0:
            raise ValueError("post-terminal timeout must not be negative")


@dataclass(frozen=True)
class QemuUbootProfile:
    """One registered machine, boot flow, and validation scenario."""

    name: str
    machine: MachineContract
    boot_flow: BootFlow
    validation: ValidationScenario

    @property
    def cpu(self) -> str:
        if self.machine.cpu is None:
            raise ValueError(f"profile {self.name} has no QEMU CPU override")
        return self.machine.cpu

    @property
    def memory(self) -> str:
        return self.machine.memory

    @property
    def memory_bytes(self) -> int:
        return self.machine.memory_bytes

    @property
    def hart_count(self) -> int:
        return self.machine.hart_count

    @property
    def bootargs(self) -> str:
        return self.validation.bootargs

    @property
    def mmu_type(self) -> str:
        mmu_types = {item for item in self.machine.mmu_types if item is not None}
        if len(mmu_types) != 1:
            raise ValueError(f"profile {self.name} has no single kernel MMU type")
        return next(iter(mmu_types))

    @property
    def ad_extension(self) -> str:
        return self.machine.ad_extension

    @property
    def remove_rng_seed(self) -> bool:
        return self.machine.remove_rng_seed

    @property
    def required_random_source(self) -> str | None:
        return self.machine.required_random_source

    @property
    def requires_resource_gate(self) -> bool:
        return self.machine.requires_resource_gate

    @property
    def fidelity(self) -> Fidelity:
        return self.machine.fidelity


UBOOT_BOOTI = BootFlow(
    name="uboot-booti",
    actions=tuple(BootActionKind),
)

# U-Boot prints its version in the startup banner and again for the `version` command.
_BOOTLOADER_VERSION_OCCURRENCES = 2
# The serial log contains both the echoed command and the marker printed by that command.
_ECHOED_MARKER_OCCURRENCES = 2

_ASTERINAS_COMMON_MILESTONES = (
    MilestoneExpectation(BootMilestone.FIRMWARE_READY, b"OpenSBI v"),
    MilestoneExpectation(
        BootMilestone.BOOTLOADER_READY,
        b"U-Boot 2026.07",
        expected_occurrences=_BOOTLOADER_VERSION_OCCURRENCES,
    ),
    MilestoneExpectation(
        BootMilestone.ARTIFACTS_LOADED,
        b"ASTERINAS_PRE_BOOTI",
        expected_occurrences=_ECHOED_MARKER_OCCURRENCES,
    ),
    MilestoneExpectation(BootMilestone.KERNEL_HANDOFF, b"Starting kernel ..."),
    MilestoneExpectation(BootMilestone.KERNEL_EARLY, b"Enter riscv_boot"),
)

ASTERINAS_USERSPACE_SMOKE = ValidationScenario(
    name="asterinas-userspace-smoke",
    bootargs="console=ttyS0 loglevel=info init=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        *_ASTERINAS_COMMON_MILESTONES,
        MilestoneExpectation(
            BootMilestone.USERSPACE_READY,
            b">>> Hello from RISC-V userspace on Asterinas! <<<",
        ),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=b">>> Hello from RISC-V userspace on Asterinas! <<<",
    forbidden_markers=(b"Uncaught panic", b"unexpected exception"),
    audit_policy=AuditPolicy.ASTERINAS_STRICT,
    startup_timeout=30.0,
    command_timeout=10.0,
    boot_timeout=60.0,
    post_terminal_timeout=0.25,
)

MEGREZ_TCP_PROBE_READY_LINE = (
    b"ASTERINAS_GMAC_TCP_PROBE_READY peer=10.100.19.216:18080 "
    b"status=200 sizes=16384,65536,1048576,16777216 "
    b"completed_bytes=17907712 pattern=mod251"
)

MEGREZ_TCP_PROBE = ValidationScenario(
    name="megrez-tcp-probe",
    bootargs=(
        "console=ttyS0 loglevel=info init=/init "
        "asterinas.net=eic7700-rj45,10.100.19.200/21 "
        "asterinas.reboot_after=60"
    ),
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        *_ASTERINAS_COMMON_MILESTONES,
        MilestoneExpectation(
            BootMilestone.USERSPACE_READY,
            MEGREZ_TCP_PROBE_READY_LINE,
        ),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=MEGREZ_TCP_PROBE_READY_LINE,
    forbidden_markers=ASTERINAS_USERSPACE_SMOKE.forbidden_markers,
    audit_policy=AuditPolicy.REGISTERED_MILESTONES,
    startup_timeout=30.0,
    command_timeout=10.0,
    boot_timeout=90.0,
    post_terminal_timeout=0.25,
)

LTP_SYSCALL_GATE = ValidationScenario(
    name="asterinas-ltp-syscall-gate",
    bootargs="console=ttyS0 loglevel=error init=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        *_ASTERINAS_COMMON_MILESTONES,
        MilestoneExpectation(
            BootMilestone.KERNEL_READY,
            b"OSTD initialized. Preparing components.",
        ),
        MilestoneExpectation(
            BootMilestone.ROOTFS_READY,
            b"[kernel] rootfs is ready",
        ),
        MilestoneExpectation(
            BootMilestone.USERSPACE_READY,
            b"__LTP_GATE_TERMINAL__",
        ),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=b"__LTP_GATE_TERMINAL__",
    forbidden_markers=(
        b"Uncaught panic",
        b"unexpected exception",
        b"[BROK] LTP runner",
    ),
    audit_policy=AuditPolicy.REGISTERED_MILESTONES,
    startup_timeout=30.0,
    command_timeout=120.0,
    boot_timeout=7200.0,
    post_terminal_timeout=2.0,
)

DRM_CURSOR_READY_LINE = b"ASTERINAS_DRM_CURSOR_R1_READY"
DRM_CURSOR_GATE = ValidationScenario(
    name="asterinas-drm-cursor-r1",
    bootargs="console=ttyS0 loglevel=info init=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        *_ASTERINAS_COMMON_MILESTONES,
        MilestoneExpectation(
            BootMilestone.KERNEL_READY,
            b"OSTD initialized. Preparing components.",
        ),
        MilestoneExpectation(
            BootMilestone.ROOTFS_READY,
            b"[kernel] rootfs is ready",
        ),
        MilestoneExpectation(
            BootMilestone.USERSPACE_READY,
            DRM_CURSOR_READY_LINE,
        ),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=DRM_CURSOR_READY_LINE,
    forbidden_markers=(
        b"Uncaught panic",
        b"unexpected exception",
        b"virtio-gpu cursor update failed",
    ),
    audit_policy=AuditPolicy.REGISTERED_MILESTONES,
    startup_timeout=30.0,
    command_timeout=10.0,
    boot_timeout=90.0,
    post_terminal_timeout=0.25,
)

MEGREZ_USERSPACE_SMOKE = ValidationScenario(
    name="megrez-userspace-smoke",
    bootargs="cpu_no_boost_1_6ghz loglevel=info init=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=ASTERINAS_USERSPACE_SMOKE.milestones,
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=ASTERINAS_USERSPACE_SMOKE.completion_line,
    forbidden_markers=ASTERINAS_USERSPACE_SMOKE.forbidden_markers,
    audit_policy=AuditPolicy.ASTERINAS_STRICT,
    startup_timeout=30.0,
    command_timeout=10.0,
    boot_timeout=60.0,
    post_terminal_timeout=0.25,
)

QEMU_VIRT = MachineContract(
    name="qemu-virt",
    qemu_machine=QemuMachine.VIRT,
    cpu="rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
    memory="2G",
    memory_bytes=0x8000_0000,
    hart_count=1,
    mmu_types=("riscv,sv39",),
    storage_transport=StorageTransport.VIRTIO_EXT4,
    dtb_provider=DtbProvider.GENERATED_PAYLOAD,
    dtb_filename="qemu-virt.dtb",
    uboot_defconfig="qemu-riscv64_smode_defconfig",
    uboot_binary="u-boot",
    uboot_build_mode=UbootBuildMode.STANDARD_SMODE,
    fidelity=Fidelity.VIRTUAL_PLATFORM,
    ad_extension="svade",
    remove_rng_seed=False,
    required_random_source="zkr",
    requires_resource_gate=False,
    provenance="QEMU-generated virt machine and device tree",
)

QEMU_VIRT_SMP4 = replace(
    QEMU_VIRT,
    name="qemu-virt-smp4",
    hart_count=4,
    mmu_types=("riscv,sv39",) * 4,
)

QEMU_VIRT_SMP4_FROZEN_DTB = replace(
    QEMU_VIRT_SMP4,
    name="qemu-virt-smp4-frozen-dtb",
    remove_rng_seed=True,
    provenance="QEMU virt SMP=4 with rng-seed removed for exact DTB identity",
)

SIFIVE_U = MachineContract(
    name="sifive-u",
    qemu_machine=QemuMachine.SIFIVE_U,
    cpu=None,
    memory="2G",
    memory_bytes=0x8000_0000,
    hart_count=5,
    mmu_types=(None, *("riscv,sv39",) * 4),
    storage_transport=StorageTransport.MMC_EXT4,
    dtb_provider=DtbProvider.GENERATED_PAYLOAD,
    dtb_filename="qemu-sifive-u.dtb",
    uboot_defconfig="sifive_unleashed_defconfig",
    uboot_binary="u-boot.bin",
    uboot_build_mode=UbootBuildMode.BOARD_SMODE,
    fidelity=Fidelity.BOARD_MODEL,
    ad_extension="unspecified",
    remove_rng_seed=False,
    required_random_source=None,
    requires_resource_gate=False,
    provenance="QEMU model of the SiFive HiFive Unleashed board",
)

SIFIVE_U_ASTERINAS_USERSPACE_SMOKE = ValidationScenario(
    name="sifive-u-asterinas-userspace-smoke",
    bootargs="console=ttyS0 loglevel=info init=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        *_ASTERINAS_COMMON_MILESTONES,
        MilestoneExpectation(
            BootMilestone.KERNEL_READY,
            b"OSTD initialized. Preparing components.",
        ),
        MilestoneExpectation(
            BootMilestone.ROOTFS_READY,
            b"[kernel] rootfs is ready",
        ),
        MilestoneExpectation(
            BootMilestone.USERSPACE_READY,
            ASTERINAS_USERSPACE_SMOKE.completion_line,
        ),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=ASTERINAS_USERSPACE_SMOKE.completion_line,
    forbidden_markers=(
        *ASTERINAS_USERSPACE_SMOKE.forbidden_markers,
        b"Bad Linux RISCV Image magic",
    ),
    audit_policy=AuditPolicy.REGISTERED_MILESTONES,
    startup_timeout=30.0,
    command_timeout=120.0,
    boot_timeout=60.0,
    post_terminal_timeout=1.0,
)

SIFIVE_U_LINUX_CONTROL = ValidationScenario(
    name="linux-reference-userspace-smoke",
    bootargs="console=ttySIF0 rdinit=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        MilestoneExpectation(BootMilestone.FIRMWARE_READY, b"OpenSBI v"),
        MilestoneExpectation(
            BootMilestone.BOOTLOADER_READY,
            b"U-Boot 2026.07",
            expected_occurrences=_BOOTLOADER_VERSION_OCCURRENCES,
        ),
        MilestoneExpectation(
            BootMilestone.ARTIFACTS_LOADED,
            b"ASTERINAS_PRE_BOOTI",
            expected_occurrences=_ECHOED_MARKER_OCCURRENCES,
        ),
        MilestoneExpectation(BootMilestone.KERNEL_HANDOFF, b"Starting kernel ..."),
        MilestoneExpectation(BootMilestone.KERNEL_EARLY, b"Linux version"),
        MilestoneExpectation(
            BootMilestone.USERSPACE_READY,
            b"ASTERINAS_LINUX_REFERENCE_READY",
        ),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=b"ASTERINAS_LINUX_REFERENCE_READY",
    forbidden_markers=(
        b"Kernel panic - not syncing",
        b"Unhandled fault",
        b"Bad Linux RISCV Image magic",
    ),
    audit_policy=AuditPolicy.REGISTERED_MILESTONES,
    startup_timeout=30.0,
    command_timeout=120.0,
    boot_timeout=60.0,
    post_terminal_timeout=1.0,
)

MEGREZ_SVADE_FAST_MACHINE = MachineContract(
    name="megrez-contract-svade-fast",
    qemu_machine=QemuMachine.VIRT,
    cpu="rv64,sv57=false,svpbmt=false,zkr=false,svadu=false,svade=true",
    memory="2G",
    memory_bytes=0x8000_0000,
    hart_count=4,
    mmu_types=("riscv,sv48",) * 4,
    storage_transport=StorageTransport.VIRTIO_EXT4,
    dtb_provider=DtbProvider.GENERATED_PAYLOAD,
    dtb_filename="qemu-virt.dtb",
    uboot_defconfig="qemu-riscv64_smode_defconfig",
    uboot_binary="u-boot",
    uboot_build_mode=UbootBuildMode.STANDARD_SMODE,
    fidelity=Fidelity.CONTRACT_APPROXIMATION,
    ad_extension="svade",
    remove_rng_seed=True,
    required_random_source=None,
    requires_resource_gate=False,
    provenance="QEMU virt approximation of reviewed Megrez contracts",
)

MEGREZ_SVADU_FAST_MACHINE = MachineContract(
    name="megrez-contract-svadu-fast",
    qemu_machine=QemuMachine.VIRT,
    cpu="rv64,sv57=false,svpbmt=false,zkr=false,svadu=true,svade=false",
    memory="2G",
    memory_bytes=0x8000_0000,
    hart_count=4,
    mmu_types=("riscv,sv48",) * 4,
    storage_transport=StorageTransport.VIRTIO_EXT4,
    dtb_provider=DtbProvider.GENERATED_PAYLOAD,
    dtb_filename="qemu-virt.dtb",
    uboot_defconfig="qemu-riscv64_smode_defconfig",
    uboot_binary="u-boot",
    uboot_build_mode=UbootBuildMode.STANDARD_SMODE,
    fidelity=Fidelity.CONTRACT_APPROXIMATION,
    ad_extension="svadu",
    remove_rng_seed=True,
    required_random_source=None,
    requires_resource_gate=False,
    provenance="QEMU virt approximation of reviewed Megrez contracts",
)

MEGREZ_SVADE_SLOW_MACHINE = MachineContract(
    name="megrez-contract-svade-slow",
    qemu_machine=QemuMachine.VIRT,
    cpu=MEGREZ_SVADE_FAST_MACHINE.cpu,
    memory="16G",
    memory_bytes=0x4_0000_0000,
    hart_count=4,
    mmu_types=("riscv,sv48",) * 4,
    storage_transport=StorageTransport.VIRTIO_EXT4,
    dtb_provider=DtbProvider.GENERATED_PAYLOAD,
    dtb_filename="qemu-virt.dtb",
    uboot_defconfig="qemu-riscv64_smode_defconfig",
    uboot_binary="u-boot",
    uboot_build_mode=UbootBuildMode.STANDARD_SMODE,
    fidelity=Fidelity.CONTRACT_APPROXIMATION,
    ad_extension="svade",
    remove_rng_seed=True,
    required_random_source=None,
    requires_resource_gate=True,
    provenance="QEMU virt approximation of reviewed Megrez contracts",
)


GENERIC_SV39 = QemuUbootProfile(
    name="generic-sv39",
    machine=QEMU_VIRT,
    boot_flow=UBOOT_BOOTI,
    validation=ASTERINAS_USERSPACE_SMOKE,
)

GENERIC_SV39_SMP4_TCP_PROBE = QemuUbootProfile(
    name="generic-sv39-smp4-tcp-probe",
    machine=QEMU_VIRT_SMP4_FROZEN_DTB,
    boot_flow=UBOOT_BOOTI,
    validation=MEGREZ_TCP_PROBE,
)

GENERIC_SV39_LTP_SMP1 = QemuUbootProfile(
    name="generic-sv39-ltp-smp1",
    machine=QEMU_VIRT,
    boot_flow=UBOOT_BOOTI,
    validation=LTP_SYSCALL_GATE,
)

GENERIC_SV39_LTP_SMP4 = QemuUbootProfile(
    name="generic-sv39-ltp-smp4",
    machine=QEMU_VIRT_SMP4,
    boot_flow=UBOOT_BOOTI,
    validation=LTP_SYSCALL_GATE,
)

GENERIC_SV39_DRM_CURSOR_SMP4 = QemuUbootProfile(
    name="generic-sv39-drm-cursor-smp4",
    machine=QEMU_VIRT_SMP4,
    boot_flow=UBOOT_BOOTI,
    validation=DRM_CURSOR_GATE,
)

MEGREZ_SV48_SVADE_FAST = QemuUbootProfile(
    name="megrez-sv48-svade-fast",
    machine=MEGREZ_SVADE_FAST_MACHINE,
    boot_flow=UBOOT_BOOTI,
    validation=MEGREZ_USERSPACE_SMOKE,
)

MEGREZ_SV48_SVADU_FAST = QemuUbootProfile(
    name="megrez-sv48-svadu-fast",
    machine=MEGREZ_SVADU_FAST_MACHINE,
    boot_flow=UBOOT_BOOTI,
    validation=MEGREZ_USERSPACE_SMOKE,
)

MEGREZ_SV48_SLOW = QemuUbootProfile(
    name="megrez-sv48-slow",
    machine=MEGREZ_SVADE_SLOW_MACHINE,
    boot_flow=UBOOT_BOOTI,
    validation=MEGREZ_USERSPACE_SMOKE,
)

SIFIVE_U_ASTERINAS_SMOKE = QemuUbootProfile(
    name="sifive-u-asterinas-smoke",
    machine=SIFIVE_U,
    boot_flow=UBOOT_BOOTI,
    validation=SIFIVE_U_ASTERINAS_USERSPACE_SMOKE,
)

SIFIVE_U_LINUX_REFERENCE = QemuUbootProfile(
    name="sifive-u-linux-reference",
    machine=SIFIVE_U,
    boot_flow=UBOOT_BOOTI,
    validation=SIFIVE_U_LINUX_CONTROL,
)

# A third board validated as a CONTRACT_APPROXIMATION: the kernel payload DTB
# claims a different SoC (e.g. StarFive VisionFive V2) while QEMU still runs
# the virt machine. This proves the kernel's DTB-driven boot path accepts a
# new board without any kernel change. See docs/porting/
# riscv-qemu-board-methodology.md for the methodology.
THIRD_BOARD = MachineContract(
    name="third-board",
    qemu_machine=QemuMachine.VIRT,
    cpu="rv64,sv57=false,svpbmt=true,zkr=true,svadu=false,svade=true",
    memory="1G",
    memory_bytes=0x4000_0000,
    hart_count=1,
    mmu_types=("riscv,sv48",),
    storage_transport=StorageTransport.VIRTIO_EXT4,
    dtb_provider=DtbProvider.GENERATED_PAYLOAD,
    dtb_filename="qemu-third-board.dtb",
    uboot_defconfig="qemu-riscv64_smode_defconfig",
    uboot_binary="u-boot",
    uboot_build_mode=UbootBuildMode.STANDARD_SMODE,
    fidelity=Fidelity.CONTRACT_APPROXIMATION,
    ad_extension="svade",
    remove_rng_seed=False,
    required_random_source=None,
    requires_resource_gate=False,
    provenance=(
        "QEMU virt machine whose payload DTB is rewritten to claim a "
        "third board (StarFive VisionFive V2 compatible); validates the "
        "DTB-driven boot path against a new SoC without new kernel code"
    ),
    root_compatible_override=("starfive,visionfive-v2", "riscv-virtio"),
)

THIRD_BOARD_SMOKE = ValidationScenario(
    name="third-board-asterinas-userspace-smoke",
    bootargs="console=ttyS0 loglevel=info init=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        *_ASTERINAS_COMMON_MILESTONES,
        MilestoneExpectation(
            BootMilestone.KERNEL_READY,
            b"OSTD initialized. Preparing components.",
        ),
        MilestoneExpectation(
            BootMilestone.ROOTFS_READY,
            b"[kernel] rootfs is ready",
        ),
        MilestoneExpectation(
            BootMilestone.USERSPACE_READY,
            ASTERINAS_USERSPACE_SMOKE.completion_line,
        ),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=ASTERINAS_USERSPACE_SMOKE.completion_line,
    forbidden_markers=(b"Uncaught panic", b"unexpected exception"),
    # Registered-milestone audit: the third board is a generic
    # CONTRACT_APPROXIMATION, not a Megrez board, so the Megrez-specific
    # STRICT checks (zkr/svpbmt confirmation, AP markers, timer proof) do
    # not apply.
    audit_policy=AuditPolicy.REGISTERED_MILESTONES,
    startup_timeout=30.0,
    command_timeout=10.0,
    boot_timeout=60.0,
    post_terminal_timeout=0.25,
)

THIRD_BOARD_ASTERINAS_SMOKE = QemuUbootProfile(
    name="third-board-asterinas-smoke",
    machine=THIRD_BOARD,
    boot_flow=UBOOT_BOOTI,
    validation=THIRD_BOARD_SMOKE,
)

_PROFILES: Mapping[str, QemuUbootProfile] = MappingProxyType(
    {
        profile.name: profile
        for profile in (
            GENERIC_SV39,
            GENERIC_SV39_SMP4_TCP_PROBE,
            GENERIC_SV39_LTP_SMP1,
            GENERIC_SV39_LTP_SMP4,
            GENERIC_SV39_DRM_CURSOR_SMP4,
            MEGREZ_SV48_SVADE_FAST,
            MEGREZ_SV48_SVADU_FAST,
            MEGREZ_SV48_SLOW,
            SIFIVE_U_ASTERINAS_SMOKE,
            SIFIVE_U_LINUX_REFERENCE,
            THIRD_BOARD_ASTERINAS_SMOKE,
        )
    }
)


def profile_by_name(name: str) -> QemuUbootProfile:
    """Resolve a registered runnable profile by its stable name."""

    try:
        return _PROFILES[name]
    except KeyError as error:
        raise ValueError(f"unknown QEMU U-Boot profile: {name}") from error


def validate_registered_profile(profile: QemuUbootProfile) -> None:
    """Reject unregistered or locally modified launch profiles."""

    try:
        registered = profile_by_name(profile.name)
    except ValueError as error:
        raise ValueError(f"profile is not registered: {profile.name}") from error
    if profile != registered:
        raise ValueError(f"profile differs from its registered value: {profile.name}")
    for kind, selected, expected in (
        ("machine", profile.machine, registered.machine),
        ("boot flow", profile.boot_flow, registered.boot_flow),
        ("validation scenario", profile.validation, registered.validation),
    ):
        if selected is not expected:
            raise ValueError(f"profile uses an unregistered {kind}: {profile.name}")


def require_profile_launch_allowed(
    profile: QemuUbootProfile,
    *,
    slow_permit: object | None = None,
) -> None:
    """Guard every QEMU launch boundary for a registered profile."""

    validate_registered_profile(profile)
    registered = profile_by_name(profile.name)
    if registered.requires_resource_gate:
        if not has_valid_slow_run_permit(slow_permit):
            raise ValueError(
                f"profile {profile.name} requires a valid slow resource gate permit"
            )
    elif slow_permit is not None:
        raise ValueError("a slow resource gate permit is invalid for a fast profile")


def _cpu_extensions(cpu: str) -> Mapping[str, str]:
    fields = cpu.split(",")
    if not fields or fields[0] != "rv64":
        raise ValueError("QEMU U-Boot profile CPU must start with rv64")
    extensions: dict[str, str] = {}
    for field in fields[1:]:
        name, separator, value = field.partition("=")
        if not separator or not name or value not in {"true", "false"}:
            raise ValueError(f"invalid QEMU CPU extension selector: {field}")
        if name in extensions:
            raise ValueError(f"duplicate QEMU CPU extension selector: {name}")
        extensions[name] = value
    return MappingProxyType(extensions)


def validate_profile_policy(
    contract: MegrezContract,
    profile: QemuUbootProfile,
) -> None:
    """Reject a Megrez profile that drifts from the reviewed board policy."""

    raw_profiles = contract.raw["profiles"]
    policy = raw_profiles.get(profile.name)
    if policy is None:
        raise ValueError(f"profile is not a reviewed Megrez profile: {profile.name}")

    expected_memory_bytes = int(policy["memory"], 16)
    if expected_memory_bytes % (1024**3):
        raise ValueError("Megrez profile memory is not an integral GiB value")
    expected = {
        "memory": f"{expected_memory_bytes // (1024**3)}G",
        "memory_bytes": expected_memory_bytes,
        "hart_count": policy["hart_count"],
        "mmu_type": policy["mmu_type"],
        "ad_extension": policy["ad_extension"],
        "bootargs": policy["bootargs"],
        "remove_rng_seed": policy["remove_rng_seed"],
        "requires_resource_gate": policy["resource_gate"],
    }
    for field, expected_value in expected.items():
        if getattr(profile, field) != expected_value:
            raise ValueError(
                f"profile {profile.name} violates Megrez policy for {field}"
            )

    extensions = _cpu_extensions(profile.cpu)
    expected_extensions = {
        "sv57": False,
        "svpbmt": policy["svpbmt"],
        "zkr": policy["zkr"],
        "svade": policy["ad_extension"] == "svade",
        "svadu": policy["ad_extension"] == "svadu",
    }
    if set(extensions) != set(expected_extensions):
        raise ValueError(
            f"profile {profile.name} has an unreviewed CPU extension selector set"
        )
    for extension, enabled in expected_extensions.items():
        if extensions.get(extension) != str(enabled).lower():
            raise ValueError(
                f"profile {profile.name} violates Megrez policy for {extension}"
            )
    if profile.required_random_source is not None:
        raise ValueError(f"profile {profile.name} requires an unproven random source")
