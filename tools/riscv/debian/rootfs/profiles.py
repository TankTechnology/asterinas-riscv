#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Immutable package and filesystem identities for Debian rootfs profiles."""

from __future__ import annotations

from dataclasses import dataclass


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
}


def get_profile(name: str) -> RootfsProfile:
    """Returns a frozen profile or rejects an unknown identity."""

    try:
        return _PROFILES[name]
    except KeyError as error:
        raise ValueError(f"unknown rootfs profile: {name}") from error
