"""Closed QEMU device contracts for the RISC-V U-Boot runner."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType


class DeviceKind(str, Enum):
    """The device kinds that this runner may render."""

    BOCHS_DISPLAY = "bochs-display"
    VIRTIO_KEYBOARD = "virtio-keyboard"
    VIRTIO_RNG = "virtio-rng"
    VIRTIO_NET = "virtio-net"
    VIRTIO_GPU = "virtio-gpu"
    # The 3D-capable variant. It is a distinct device rather than a property of
    # `virtio-gpu`, and it only offers virgl when the display backend can give
    # it a host GL context.
    VIRTIO_GPU_GL = "virtio-gpu-gl"
    SCRATCH_VIRTIO_BLOCK = "scratch-virtio-block"
    NVME = "nvme"


@dataclass(frozen=True)
class FramebufferContract:
    address: int
    size: int
    width: int
    height: int
    stride: int
    pixel_format: str


@dataclass(frozen=True)
class QemuDeviceSet:
    name: str
    devices: tuple[DeviceKind, ...]
    framebuffer: FramebufferContract | None = None
    #: The QEMU display backend. Headless by default; a GL device needs a
    #: backend that can hand it a host GL context, which `none` cannot.
    display: str = "none"


@dataclass(frozen=True)
class RuntimeDevicePaths:
    capture_root: Path | None = None
    monitor_socket: Path | None = None
    scratch_disk: Path | None = None
    nvme_disk: Path | None = None


BOCHS_XRGB8888 = FramebufferContract(
    address=0x4000_0000,
    size=0x0100_0000,
    width=1280,
    height=1024,
    stride=5120,
    pixel_format="x8r8g8b8",
)
HEADLESS = QemuDeviceSet("headless", ())
VIRTIO_NET_SLIRP = QemuDeviceSet(
    "virtio-net-slirp",
    (DeviceKind.VIRTIO_NET,),
)
MEGREZ_BASIC = QemuDeviceSet(
    "megrez-basic",
    (DeviceKind.BOCHS_DISPLAY,),
    BOCHS_XRGB8888,
)
DRM_CURSOR = QemuDeviceSet(
    "drm-cursor",
    (DeviceKind.VIRTIO_GPU,),
)
# The GEM gate needs the same single-GPU device contract as the cursor gate, but
# it names its own set so that changing one gate's devices cannot silently move
# the other's.
DRM_GEM = QemuDeviceSet(
    "drm-gem",
    (DeviceKind.VIRTIO_GPU,),
)
DRM_RENDER_NODE = QemuDeviceSet(
    "drm-render-node",
    (DeviceKind.VIRTIO_GPU,),
)
#: A display and **no GPU at all**. The bochs device is what gives the firmware
#: framebuffer contract a real region of memory to point at, and its absence of
#: a virtio-gpu is the point: with no GPU to present through, the only way this
#: machine can put a picture up is by copying into that region. A set that kept
#: the GPU would let the driver select it and test nothing.
DRM_FIRMWARE = QemuDeviceSet(
    "drm-firmware",
    (DeviceKind.BOCHS_DISPLAY,),
    BOCHS_XRGB8888,
)
#: The board's scanout **geometry** (1920x1080, stride 7680), which is what
#: `BOCHS_XRGB8888` never exercised: 1280x1024 is the only layout any run has
#: ever used, and it is U-Boot's compiled-in default rather than a choice --
#: see `QEMU_UBOOT_BOCHS_SIZE` in `prepare_qemu_uboot_booti.sh`.
#:
#: The geometry is `MEGREZ_FRAMEBUFFER` in `megrez_board_session.py` -- what
#: the kernel has logged on hardware as `Registered firmware framebuffer:
#: base=0xfd800000 ... resolution=1920x1080`. A test holds the two together,
#: so this is a second spelling rather than a second source. The **address** is
#: deliberately not the board's, for two independent reasons, and both are
#: worth stating because each cost a failed run to find.
#:
#: 1. **0xfd800000 is not simulable at 2 GiB.** U-Boot carves its stack and
#:    code out of the top of RAM -- `[0xfde96000, 0xffffffff]` -- and the
#:    board's scanout `[0xfd800000, 0xfdfe9000)` overlaps it. U-Boot relocates
#:    the device tree to 0xfde8f000, *inside the scanout*; the kernel boots on
#:    that tree, `aster-framebuffer` initialises and writes the screen, and
#:    destroys the tree it booted from (`Uncaught panic: /cpus is a required
#:    node`, 91 ms in). Declaring it in `/reserved-memory` does not rescue it:
#:    U-Boot's `boot_fdt_reserve_region()` swallows the `-EEXIST` that an
#:    overlapping region returns (u-boot boot/image-fdt.c:72-89), so the
#:    declaration is dropped silently. The board is not affected -- 16 GiB puts
#:    its U-Boot ~15 GB away -- but a run claiming to exercise that address
#:    would be claiming something false.
#:
#: 2. **A scanout that is not the display cannot be screenshotted.** With the
#:    node pointing into DRAM instead (0x90000000, also tried), every guest
#:    check passed and the allocator carve-out was visible in the boot log
#:    (`Adding free frames ... 87081000..90000000`), but the QEMU capture then
#:    holds only U-Boot's console text: two distinct colours, against the PPM
#:    audit's `distinct_colors >= 3`. That is structural, not a bug -- QEMU
#:    has no display where that buffer is -- so the screenshot claim is simply
#:    unavailable for that arrangement and the gate reports the run as failed.
#:
#: What this set therefore covers is the geometry end to end, with the
#: screenshot real, because the node points at the bochs BAR exactly as
#: `BOCHS_XRGB8888` does. It does **not** cover a scanout inside DRAM.
MEGREZ_BOARD_GEOMETRY = QemuDeviceSet(
    "megrez-board-geometry",
    (DeviceKind.BOCHS_DISPLAY,),
    FramebufferContract(
        address=0x4000_0000,
        size=0x0100_0000,
        width=1920,
        height=1080,
        stride=1920 * 4,
        pixel_format="x8r8g8b8",
    ),
)

# The only set that is not `-display none`: `egl-headless,gl=on` is what gives
# the GL device a host context, and without one QEMU withholds the virgl
# feature bit entirely.
DRM_VIRGL = QemuDeviceSet(
    "drm-virgl",
    (DeviceKind.VIRTIO_GPU_GL,),
    display="egl-headless,gl=on",
)

_DEVICE_SETS = MappingProxyType(
    {
        HEADLESS.name: HEADLESS,
        VIRTIO_NET_SLIRP.name: VIRTIO_NET_SLIRP,
        MEGREZ_BASIC.name: MEGREZ_BASIC,
        DRM_CURSOR.name: DRM_CURSOR,
        DRM_GEM.name: DRM_GEM,
        DRM_RENDER_NODE.name: DRM_RENDER_NODE,
        DRM_FIRMWARE.name: DRM_FIRMWARE,
        MEGREZ_BOARD_GEOMETRY.name: MEGREZ_BOARD_GEOMETRY,
        DRM_VIRGL.name: DRM_VIRGL,
    }
)


#: The display backends a device set may name. Closed so that a typo becomes a
#: refusal rather than a QEMU that silently drops to a different backend.
DISPLAY_BACKENDS = ("none", "egl-headless,gl=on")


def _validate_device_set_shape(device_set: QemuDeviceSet) -> None:
    if not isinstance(device_set, QemuDeviceSet):
        raise ValueError("device set must use the closed QemuDeviceSet type")
    if any(not isinstance(device, DeviceKind) for device in device_set.devices):
        raise ValueError("device set contains an unregistered device kind")
    if len(device_set.devices) != len(set(device_set.devices)):
        raise ValueError("device set contains duplicate devices")
    if (
        device_set.framebuffer is not None
        and DeviceKind.BOCHS_DISPLAY not in device_set.devices
    ):
        raise ValueError("framebuffer requires bochs-display")
    if (
        DeviceKind.BOCHS_DISPLAY in device_set.devices
        and device_set.framebuffer is None
    ):
        raise ValueError("bochs-display requires framebuffer")
    if device_set.display not in DISPLAY_BACKENDS:
        raise ValueError(f"unregistered display backend: {device_set.display}")
    # The GL device gets its virgl feature bit from a host GL context, so a
    # headless backend would silently produce a device without 3D.
    if (
        DeviceKind.VIRTIO_GPU_GL in device_set.devices
        and device_set.display == "none"
    ):
        raise ValueError("virtio-gpu-gl requires a GL-capable display backend")


def validate_registered_device_set(device_set: QemuDeviceSet) -> None:
    """Require a well-formed device set to be the registered object itself."""

    _validate_device_set_shape(device_set)
    if _DEVICE_SETS.get(device_set.name) is not device_set:
        raise ValueError("device set is not a registered device set")


def device_set_by_name(name: str) -> QemuDeviceSet:
    """Return one of the fixed device-set contracts by name."""

    try:
        return _DEVICE_SETS[name]
    except KeyError as error:
        raise ValueError(f"unknown registered device set: {name}") from error


def _validate_capture_paths(paths: RuntimeDevicePaths) -> tuple[Path, Path]:
    capture_root = paths.capture_root
    monitor_socket = paths.monitor_socket
    if capture_root is None or monitor_socket is None:
        raise ValueError(
            "framebuffer device set requires capture_root and monitor_socket"
        )
    if not capture_root.is_absolute():
        raise ValueError("capture_root must be absolute")
    if capture_root.is_symlink() or not capture_root.is_dir():
        raise ValueError("capture_root must be a non-symlinked directory")
    if stat.S_IMODE(capture_root.stat().st_mode) != 0o700:
        raise ValueError("capture_root must have mode 0700")
    if not monitor_socket.is_absolute():
        raise ValueError("monitor_socket must be absolute")
    if monitor_socket.is_symlink():
        raise ValueError("monitor_socket must not be a symlink")
    if "," in str(monitor_socket):
        raise ValueError("monitor_socket must not contain a comma")
    try:
        monitor_socket.resolve(strict=False).relative_to(capture_root.resolve())
    except ValueError as error:
        raise ValueError(
            "monitor_socket must be strictly below capture_root"
        ) from error
    if monitor_socket.resolve(strict=False) == capture_root.resolve():
        raise ValueError("monitor_socket must be strictly below capture_root")
    return capture_root, monitor_socket


def render_device_argv(
    device_set: QemuDeviceSet,
    device_paths: RuntimeDevicePaths | None,
) -> tuple[str, ...]:
    """Render the fixed argv fragments for a registered device contract."""

    validate_registered_device_set(device_set)
    paths = RuntimeDevicePaths() if device_paths is None else device_paths
    if device_set is HEADLESS:
        if any(
            path is not None
            for path in (
                paths.capture_root,
                paths.monitor_socket,
                paths.scratch_disk,
                paths.nvme_disk,
            )
        ):
            raise ValueError("headless device set does not accept runtime paths")
        return ()
    if paths.scratch_disk is not None or paths.nvme_disk is not None:
        raise ValueError("scratch and NVMe paths are unused by this device set")

    argv: list[str] = []
    for device in device_set.devices:
        if device is DeviceKind.BOCHS_DISPLAY:
            framebuffer = device_set.framebuffer
            if framebuffer is None:
                raise AssertionError("validated bochs-display must have a framebuffer")
            argv.extend(
                (
                    "-device",
                    f"bochs-display,xres={framebuffer.width},yres={framebuffer.height}",
                )
            )
        elif device is DeviceKind.VIRTIO_KEYBOARD:
            argv.extend(("-device", "virtio-keyboard-device"))
        elif device is DeviceKind.VIRTIO_NET:
            argv.extend(
                (
                    "-netdev",
                    "user,id=net0",
                    "-device",
                    "virtio-net-device,netdev=net0",
                )
            )
        elif device is DeviceKind.VIRTIO_GPU:
            argv.extend(
                (
                    "-nic",
                    "none",
                    "-device",
                    "virtio-gpu-device",
                    "-trace",
                    "enable=virtio_gpu_update_cursor",
                )
            )
        elif device is DeviceKind.VIRTIO_GPU_GL:
            argv.extend(
                (
                    "-nic",
                    "none",
                    "-device",
                    "virtio-gpu-gl-device",
                )
            )
        else:
            raise ValueError(f"device kind is not rendered in this increment: {device}")
    if device_set.framebuffer is not None:
        _, monitor_socket = _validate_capture_paths(paths)
        argv.extend(("-qmp", f"unix:{monitor_socket},server=on,wait=off"))
    elif any(path is not None for path in (paths.capture_root, paths.monitor_socket)):
        raise ValueError("non-framebuffer device set does not accept capture paths")
    return tuple(argv)
