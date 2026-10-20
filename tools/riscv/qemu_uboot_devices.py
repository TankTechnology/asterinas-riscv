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
    # The 3D-capable variant. It is kept separate so a plain 2D gate cannot
    # accidentally claim virgl just because the guest driver supports it.
    VIRTIO_GPU_GL = "virtio-gpu-gl"
    VIRTIO_GPU_DRM = "virtio-gpu-drm"
    QEMU_XHCI = "qemu-xhci"
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
    # A virtual scanout screenshot contract, kept separate from firmware
    # framebuffer ownership so DRM runs never inject simple-framebuffer DT.
    capture_contract: FramebufferContract | None = None
    machine: str | None = None
    network_backend: str | None = None
    # QEMU display backend. A GL device requires a backend that can create a
    # host GL context; the default remains the existing headless path.
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
QEMU_XHCI = QemuDeviceSet(
    "qemu-xhci",
    (DeviceKind.QEMU_XHCI,),
)
SIFIVE_U_GEM_SLIRP = QemuDeviceSet(
    "sifive-u-gem-slirp",
    (),
    machine="sifive_u",
    network_backend="user",
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
DRM_GEM = QemuDeviceSet(
    "drm-gem",
    (DeviceKind.VIRTIO_GPU,),
)
DRM_RENDER_NODE = QemuDeviceSet(
    "drm-render-node",
    (DeviceKind.VIRTIO_GPU,),
)
DRM_SCANOUT = QemuDeviceSet(
    "drm-scanout",
    (DeviceKind.VIRTIO_GPU_DRM,),
    capture_contract=FramebufferContract(
        address=0,
        size=0,
        width=1280,
        height=800,
        stride=1280 * 4,
        pixel_format="x8r8g8b8",
    ),
)
# GTK is an explicit host scanout backend for the same 2D DRM contract.  It is
# kept separate from the default headless profile so a screenshot receipt can
# name the host presentation path instead of silently changing existing runs.
DRM_SCANOUT_GTK = QemuDeviceSet(
    "drm-scanout-gtk",
    (DeviceKind.VIRTIO_GPU_DRM,),
    # GTK reports the guest's selected 640x480 scanout surface.  The
    # headless QMP backend keeps its 1280x800 virtual canvas, so this is a
    # deliberately separate capture contract rather than a silent resize.
    capture_contract=FramebufferContract(
        address=0,
        size=0,
        width=640,
        height=480,
        stride=640 * 4,
        pixel_format="x8r8g8b8",
    ),
    display="gtk",
)
# Fault legs stop before a successful frame and therefore do not need a QMP
# screenshot.  Keep that absence explicit so the runner does not manufacture a
# capture requirement for a fail-closed recovery run.
DRM_SCANOUT_FAULT = QemuDeviceSet(
    "drm-scanout-fault",
    (DeviceKind.VIRTIO_GPU_DRM,),
)
DRM_VIRGL = QemuDeviceSet(
    "drm-virgl",
    (DeviceKind.VIRTIO_GPU_GL,),
    # egl-headless enables the GL context itself; QEMU 10.2 does not accept a
    # `gl=on` suboption for this backend.
    display="egl-headless",
)

_DEVICE_SETS = MappingProxyType(
    {
        HEADLESS.name: HEADLESS,
        VIRTIO_NET_SLIRP.name: VIRTIO_NET_SLIRP,
        QEMU_XHCI.name: QEMU_XHCI,
        SIFIVE_U_GEM_SLIRP.name: SIFIVE_U_GEM_SLIRP,
        MEGREZ_BASIC.name: MEGREZ_BASIC,
        DRM_CURSOR.name: DRM_CURSOR,
        DRM_GEM.name: DRM_GEM,
        DRM_RENDER_NODE.name: DRM_RENDER_NODE,
        DRM_SCANOUT.name: DRM_SCANOUT,
        DRM_SCANOUT_GTK.name: DRM_SCANOUT_GTK,
        DRM_SCANOUT_FAULT.name: DRM_SCANOUT_FAULT,
        DRM_VIRGL.name: DRM_VIRGL,
    }
)

DISPLAY_BACKENDS = ("none", "egl-headless", "gtk")


def _validate_device_set_shape(device_set: QemuDeviceSet) -> None:
    if not isinstance(device_set, QemuDeviceSet):
        raise ValueError("device set must use the closed QemuDeviceSet type")
    if any(not isinstance(device, DeviceKind) for device in device_set.devices):
        raise ValueError("device set contains an unregistered device kind")
    if len(device_set.devices) != len(set(device_set.devices)):
        raise ValueError("device set contains duplicate devices")
    if device_set.machine is not None and not device_set.machine:
        raise ValueError("device set machine must be non-empty")
    if device_set.network_backend not in (None, "user"):
        raise ValueError("device set contains an unregistered network backend")
    if device_set.network_backend is not None and DeviceKind.VIRTIO_NET in device_set.devices:
        raise ValueError("machine network backend cannot be combined with virtio-net")
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
    if device_set.capture_contract is not None:
        capture = device_set.capture_contract
        if capture.width <= 0 or capture.height <= 0 or capture.stride < capture.width * 4:
            raise ValueError("capture contract has invalid geometry")
    if device_set.display not in DISPLAY_BACKENDS:
        raise ValueError(f"unregistered display backend: {device_set.display}")
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
        elif device is DeviceKind.VIRTIO_GPU_DRM:
            argv.extend(
                (
                    "-nic",
                    "none",
                    "-device",
                    "virtio-gpu-device",
                    "-trace",
                    "enable=virtio_gpu_cmd_res_create_2d",
                    "-trace",
                    "enable=virtio_gpu_cmd_res_back_attach",
                    "-trace",
                    "enable=virtio_gpu_cmd_set_scanout",
                    "-trace",
                    "enable=virtio_gpu_cmd_res_xfer_toh_2d",
                    "-trace",
                    "enable=virtio_gpu_cmd_res_flush",
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
        elif device is DeviceKind.QEMU_XHCI:
            argv.extend(
                (
                    "-device",
                    "qemu-xhci,id=xhci",
                    "-device",
                    "usb-kbd,bus=xhci.0",
                )
            )
        else:
            raise ValueError(f"device kind is not rendered in this increment: {device}")
    if device_set.network_backend is not None:
        argv.extend(("-nic", device_set.network_backend))
    if device_set.framebuffer is not None:
        _, monitor_socket = _validate_capture_paths(paths)
        argv.extend(("-qmp", f"unix:{monitor_socket},server=on,wait=off"))
    elif device_set.capture_contract is not None and (
        paths.capture_root is not None or paths.monitor_socket is not None
    ):
        _, monitor_socket = _validate_capture_paths(paths)
        argv.extend(("-qmp", f"unix:{monitor_socket},server=on,wait=off"))
    elif any(path is not None for path in (paths.capture_root, paths.monitor_socket)):
        raise ValueError("device set without display capture does not accept capture paths")
    return tuple(argv)
