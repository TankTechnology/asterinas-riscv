#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded libdrm discovery probe used by the DRM desktop gate."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys


class DrmVersion(ctypes.Structure):
    # libdrm's public drmVersion uses signed int lengths even though the
    # kernel's struct drm_version uses __kernel_size_t.  Keeping these as
    # c_size_t shifts every pointer by four bytes on 64-bit guests and makes
    # merely reading `name` jump into the length field, which manifested as a
    # guest SIGSEGV during Xorg's DRM readiness probe.
    _fields_ = [
        ("version_major", ctypes.c_int),
        ("version_minor", ctypes.c_int),
        ("version_patchlevel", ctypes.c_int),
        ("name_len", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("date_len", ctypes.c_int),
        ("date", ctypes.c_char_p),
        ("desc_len", ctypes.c_int),
        ("desc", ctypes.c_char_p),
    ]


class DrmDevice(ctypes.Structure):
    # This is the stable prefix of libdrm's drmDevice. The two union members
    # are opaque pointers here; only the bus type is needed for the gate.
    _fields_ = [
        ("nodes", ctypes.POINTER(ctypes.c_char_p)),
        ("available_nodes", ctypes.c_int),
        ("bustype", ctypes.c_int),
        ("businfo", ctypes.c_void_p),
        ("deviceinfo", ctypes.c_void_p),
    ]


def emit(line: str) -> None:
    print(line, flush=True)
    console_fd = -1
    try:
        console_fd = os.open(
            "/dev/console", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC
        )
        os.write(console_fd, (line + "\n").encode("ascii", "replace"))
    except OSError:
        pass
    finally:
        if console_fd >= 0:
            os.close(console_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/dri/renderD128")
    args = parser.parse_args()

    try:
        drm = ctypes.CDLL("libdrm.so.2", use_errno=True)
        drm.drmGetVersion.argtypes = [ctypes.c_int]
        drm.drmGetVersion.restype = ctypes.POINTER(DrmVersion)
        drm.drmFreeVersion.argtypes = [ctypes.POINTER(DrmVersion)]
        drm.drmGetDevice2.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        drm.drmGetDevice2.restype = ctypes.c_int
        drm.drmFreeDevice.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        fd = os.open(args.device, os.O_RDWR | os.O_CLOEXEC)
    except OSError as error:
        emit(f"DRM_LIBDRM_FAIL stage=open errno={error.errno}")
        return 1
    except Exception as error:
        emit(f"DRM_LIBDRM_FAIL stage=load error={type(error).__name__}:{error}")
        return 1

    try:
        version = drm.drmGetVersion(fd)
        if not version:
            emit(f"DRM_LIBDRM_FAIL stage=version errno={ctypes.get_errno()}")
            return 1
        try:
            name = (version.contents.name or b"").decode("ascii", "replace")
        finally:
            drm.drmFreeVersion(version)
        if name != "virtio_gpu":
            emit(f"DRM_LIBDRM_FAIL stage=driver-name value={name}")
            return 1
        emit(f"DRM_LIBDRM_VERSION name={name}")

        device = ctypes.c_void_p()
        result = drm.drmGetDevice2(fd, 0, ctypes.byref(device))
        if result != 0 or not device.value:
            emit(f"DRM_LIBDRM_FAIL stage=device result={result} errno={ctypes.get_errno()}")
            return 1
        try:
            bustype = ctypes.cast(device, ctypes.POINTER(DrmDevice)).contents.bustype
            # libdrm recognises the `/virtio` sysfs transport, then reuses
            # its PCI parser for the PCI-shaped compatibility attributes. In
            # that path drmGetDevice2() reports DRM_BUS_PCI (0), even though
            # the kernel transport is virtio-mmio. Newer libdrm versions may
            # preserve DRM_BUS_VIRTIO (0x10), so accept both while keeping the
            # transport proof in the separate sysfs gate.
            if bustype == 0:
                emit("DRM_LIBDRM_DEVICE PASS bustype=pci-compat transport=virtio")
            elif bustype == 0x10:
                emit("DRM_LIBDRM_DEVICE PASS bustype=virtio")
            else:
                emit(f"DRM_LIBDRM_FAIL stage=bustype value={bustype}")
                return 1
        finally:
            drm.drmFreeDevice(ctypes.byref(device))
        return 0
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
