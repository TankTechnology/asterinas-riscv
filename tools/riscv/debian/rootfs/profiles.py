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
    root_size_bytes: int = 1024 * 1024 * 1024
    filesystem_type: str = "ext2"
    journal: bool = False


_BASE_IDENTITY_PACKAGES = (
    "base-files",
    "libc6",
    "bash",
    "coreutils",
    "util-linux",
)

_PROFILES = {
    "minimal": RootfsProfile(
        name="minimal",
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
        identity_packages=_BASE_IDENTITY_PACKAGES,
    ),
}

BROWSER_WEB_DESKTOP_PACKAGES = (
    "mousepad",
    "ristretto",
    "atril",
    "xarchiver",
    "zip",
    "unzip",
    "dbus-x11",
)

# browser-web keeps its Firefox/network package set explicit so a change in one
# profile cannot silently alter the current daily image.
_BROWSER_WEB_BASE_REQUESTED_PACKAGES = (
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
    "openbox",
    "pcmanfm",
    "polkitd",
    "procps",
    "systemd-sysv",
    "udev",
    "util-linux",
    "x11-apps",
    "x11-utils",
    "xauth",
    "xfonts-base",
    "xinit",
    "xserver-xorg-core",
    "xserver-xorg-input-evdev",
    "xserver-xorg-video-fbdev",
    "xterm",
    "firefox-esr",
    "python3-minimal",
)
_BROWSER_WEB_BASE_IDENTITY_PACKAGES = _BASE_IDENTITY_PACKAGES + (
    "systemd",
    "systemd-sysv",
    "dbus",
    "udev",
    "libpam-systemd",
    "xserver-xorg-core",
    "xterm",
    "librsvg2-common",
    "lxpanel",
    "openbox",
    "pcmanfm",
    "curl",
    "iproute2",
    "iputils-ping",
    "x11-apps",
    "firefox-esr",
    "python3-minimal",
)


_PROFILES["browser-web"] = RootfsProfile(
    name="browser-web",
    schema_version=7,
    root_label="ASTER_BROWSERWEB",
    root_uuid="c2ce5134-afcc-4d7c-b71e-7e6d4a8f2b10",
    # Firefox decodes H.264 and HEVC only through libavcodec, and Debian keeps
    # those in firefox-esr's Recommends rather than its Depends. The build
    # installs with --no-install-recommends, so every site that serves H.264,
    # which is most of them including bilibili, fails to play while the page
    # itself loads. Name the decoder explicitly instead of relying on a
    # recommendation the build discards.
    requested_packages=_BROWSER_WEB_BASE_REQUESTED_PACKAGES
    + ("xdotool", "libavcodec-extra61", "vim", "build-essential")
    + BROWSER_WEB_DESKTOP_PACKAGES,
    identity_packages=_BROWSER_WEB_BASE_IDENTITY_PACKAGES
    + ("ca-certificates", "xdotool", "libavcodec-extra61", "vim", "build-essential")
    + BROWSER_WEB_DESKTOP_PACKAGES,
    # Firefox's installed files consume most of a 1 GiB image.  A persistent
    # profile and even a small controlled download then hit ENOSPC during the
    # normal bookmark/places maintenance path.  Keep the smaller milestone
    # images unchanged, but give the long-lived online browser a real working
    # margin.
    root_size_bytes=3 * 1024 * 1024 * 1024,
)


PROFILE_NAMES = tuple(_PROFILES)


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
    print(profile.root_size_bytes)
    print(profile.filesystem_type)
    print("journal" if profile.journal else "nojournal")
    print(*profile.requested_packages, sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
