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
MEGREZ_BASIC = QemuDeviceSet(
    "megrez-basic",
    (DeviceKind.BOCHS_DISPLAY,),
    BOCHS_XRGB8888,
)

_DEVICE_SETS = MappingProxyType(
    {
        HEADLESS.name: HEADLESS,
        MEGREZ_BASIC.name: MEGREZ_BASIC,
    }
)


def _validate_device_set_shape(device_set: QemuDeviceSet) -> None:
    if not isinstance(device_set, QemuDeviceSet):
        raise ValueError("device set must use the closed QemuDeviceSet type")
    if any(not isinstance(device, DeviceKind) for device in device_set.devices):
        raise ValueError("device set contains an unregistered device kind")
    if len(device_set.devices) != len(set(device_set.devices)):
        raise ValueError("device set contains duplicate devices")
    if device_set.framebuffer is not None and DeviceKind.BOCHS_DISPLAY not in device_set.devices:
        raise ValueError("framebuffer requires bochs-display")


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
        raise ValueError("framebuffer device set requires capture_root and monitor_socket")
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
        raise ValueError("monitor_socket must be strictly below capture_root") from error
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
            argv.extend(("-device", "bochs-display,xres=1280,yres=1024"))
        elif device is DeviceKind.VIRTIO_KEYBOARD:
            argv.extend(("-device", "virtio-keyboard-device"))
        else:
            raise ValueError(f"device kind is not rendered in this increment: {device}")
    if device_set.framebuffer is not None:
        _, monitor_socket = _validate_capture_paths(paths)
        argv.extend(("-qmp", f"unix:{monitor_socket},server=on,wait=off"))
    elif any(
        path is not None for path in (paths.capture_root, paths.monitor_socket)
    ):
        raise ValueError("non-framebuffer device set does not accept capture paths")
    return tuple(argv)
