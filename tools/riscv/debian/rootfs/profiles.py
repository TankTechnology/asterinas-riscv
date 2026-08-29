#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Immutable package and filesystem identities for Debian rootfs profiles."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
from collections.abc import Sequence


@dataclass(frozen=True)
class RootfsProfile:
    """The exact requested packages and filesystem identity of one profile."""

    name: str
    schema_version: int
    root_label: str
    root_uuid: str
    requested_packages: tuple[str, ...]
    identity_packages: tuple[str, ...]


_M1_IDENTITY_PACKAGES = (
    "base-files",
    "libc6",
    "bash",
    "coreutils",
    "util-linux",
)

_PROFILES = {
    "minimal-m1": RootfsProfile(
        name="minimal-m1",
        schema_version=1,
        root_label="ASTER_DEBIANROOT",
        root_uuid="7b7ad749-77d0-4e59-89e4-e117244a70aa",
        requested_packages=(
            "bash",
            "ca-certificates",
            "coreutils",
            "procps",
            "util-linux",
        ),
        identity_packages=_M1_IDENTITY_PACKAGES,
    ),
    "systemd-m2": RootfsProfile(
        name="systemd-m2",
        schema_version=2,
        root_label="ASTER_DEBIANM2",
        root_uuid="4a5d8b91-2189-44fa-a908-ae88dc76f2a1",
        requested_packages=(
            "bash",
            "ca-certificates",
            "coreutils",
            "dbus",
            "procps",
            "systemd-sysv",
            "util-linux",
        ),
        identity_packages=_M1_IDENTITY_PACKAGES
        + (
            "systemd",
            "systemd-sysv",
            "dbus",
        ),
    ),
    "desktop-m3": RootfsProfile(
        name="desktop-m3",
        schema_version=3,
        root_label="ASTER_DEBIANM3",
        root_uuid="87dc62d5-cd0b-47a3-a82b-32b24f2ed9d3",
        requested_packages=(
            "bash",
            "ca-certificates",
            "coreutils",
            "dbus",
            "libpam-systemd",
            "matchbox-window-manager",
            "procps",
            "systemd-sysv",
            "udev",
            "util-linux",
            "xauth",
            "x11-utils",
            "xfonts-base",
            "xinit",
            "xserver-xorg-core",
            "xserver-xorg-input-evdev",
            "xserver-xorg-video-fbdev",
            "xterm",
        ),
        identity_packages=_M1_IDENTITY_PACKAGES
        + (
            "systemd",
            "systemd-sysv",
            "dbus",
            "udev",
            "libpam-systemd",
            "xserver-xorg-core",
            "xterm",
        ),
    ),
    "desktop-m4": RootfsProfile(
        name="desktop-m4",
        schema_version=4,
        root_label="ASTER_DEBIANM4",
        root_uuid="e13bd1e8-8719-539f-b5e7-5c7b5f5df3c8",
        requested_packages=(
            "bash",
            "ca-certificates",
            "coreutils",
            "dbus",
            "libpam-systemd",
            "librsvg2-common",
            "lxpanel",
            "netsurf-gtk",
            "openbox",
            "pcmanfm",
            "procps",
            "systemd-sysv",
            "udev",
            "util-linux",
            "x11-utils",
            "xauth",
            "xfonts-base",
            "xinit",
            "xserver-xorg-core",
            "xserver-xorg-input-evdev",
            "xserver-xorg-video-fbdev",
            "xterm",
        ),
        identity_packages=_M1_IDENTITY_PACKAGES
        + (
            "systemd",
            "systemd-sysv",
            "dbus",
            "udev",
            "libpam-systemd",
            "xserver-xorg-core",
            "xterm",
            "librsvg2-common",
            "lxpanel",
            "netsurf-gtk",
            "openbox",
            "pcmanfm",
        ),
    ),
    "desktop-m5-network": RootfsProfile(
        name="desktop-m5-network",
        schema_version=5,
        root_label="ASTER_DEBIANM5",
        root_uuid="182e1ea4-296d-5383-8bcb-ea67e40db074",
        requested_packages=(
            "bash",
            "ca-certificates",
            "coreutils",
            "curl",
            "dbus",
            "fonts-wqy-microhei",
            "iproute2",
            "iputils-ping",
            "libpam-systemd",
            "librsvg2-common",
            "lxpanel",
            "netsurf-gtk",
            "openbox",
            "pcmanfm",
            "procps",
            "systemd-sysv",
            "udev",
            "util-linux",
            "x11-utils",
            "xauth",
            "xdotool",
            "xfonts-base",
            "xinit",
            "xserver-xorg-core",
            "xserver-xorg-input-evdev",
            "xserver-xorg-video-fbdev",
            "xterm",
        ),
        identity_packages=_M1_IDENTITY_PACKAGES
        + (
            "systemd",
            "systemd-sysv",
            "dbus",
            "udev",
            "libpam-systemd",
            "xserver-xorg-core",
            "xterm",
            "librsvg2-common",
            "lxpanel",
            "netsurf-gtk",
            "openbox",
            "pcmanfm",
            "curl",
            "iproute2",
            "iputils-ping",
            "xdotool",
        ),
    ),
    "desktop-drm": RootfsProfile(
        name="desktop-drm",
        schema_version=8,
        root_label="ASTER_DEBIANDRM",
        root_uuid="5b9b1d5f-0a8a-4d7d-8d8d-6c3f9d9f3a80",
        requested_packages=(
            "bash",
            "ca-certificates",
            "coreutils",
            "dbus",
            "libdrm2",
            "libgl1-mesa-dri",
            "libglx-mesa0",
            "libpam-systemd",
            "librsvg2-common",
            "lxpanel",
            "mesa-utils",
            "openbox",
            "pcmanfm",
            "procps",
            "systemd-sysv",
            "udev",
            "util-linux",
            "x11-utils",
            "xauth",
            "xfonts-base",
            "xinit",
            "xserver-xorg-core",
            "xserver-xorg-input-evdev",
            "xterm",
        ),
        identity_packages=(
            *_M1_IDENTITY_PACKAGES,
            "systemd",
            "systemd-sysv",
            "dbus",
            "udev",
            "libpam-systemd",
            "xserver-xorg-core",
            "xterm",
            "libdrm2",
            "libgl1-mesa-dri",
            "libglx-mesa0",
            "mesa-utils",
            "librsvg2-common",
            "lxpanel",
            "openbox",
            "pcmanfm",
        ),
    ),
}

_PROFILES["browser-m5"] = RootfsProfile(
    name="browser-m5",
    schema_version=6,
    root_label="ASTER_BROWSERM5",
    root_uuid="41be8ca6-8168-5ef0-84b1-25824d8f87f5",
    requested_packages=tuple(
        package
        for package in _PROFILES["desktop-m5-network"].requested_packages
        if package not in {"netsurf-gtk", "xdotool"}
    )
    + ("firefox-esr", "python3-minimal"),
    identity_packages=tuple(
        package
        for package in _PROFILES["desktop-m5-network"].identity_packages
        if package not in {"netsurf-gtk", "xdotool"}
    )
    + ("firefox-esr", "python3-minimal"),
)

_PROFILES["browser-web"] = RootfsProfile(
    name="browser-web",
    schema_version=7,
    root_label="ASTER_BROWSERWEB",
    root_uuid="c2ce5134-afcc-4d7c-b71e-7e6d4a8f2b10",
    requested_packages=_PROFILES["browser-m5"].requested_packages,
    identity_packages=_PROFILES["browser-m5"].identity_packages
    + ("ca-certificates",),
)


def get_profile(name: str) -> RootfsProfile:
    """Returns a frozen profile or rejects an unknown identity."""

    try:
        return _PROFILES[name]
    except KeyError as error:
        raise ValueError(f"unknown rootfs profile: {name}") from error


def main(arguments: Sequence[str] | None = None) -> int:
    """Prints one profile as newline-delimited fields for the shell builder."""

    parser = argparse.ArgumentParser(prog="profiles")
    parser.add_argument("--profile", required=True)
    values = parser.parse_args(arguments)
    try:
        profile = get_profile(values.profile)
    except ValueError as error:
        parser.error(str(error))
    print(profile.root_label)
    print(profile.root_uuid)
    print(*profile.requested_packages, sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
