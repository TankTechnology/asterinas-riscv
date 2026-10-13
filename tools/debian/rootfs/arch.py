# SPDX-License-Identifier: MPL-2.0

"""The architecture table for the Debian rootfs tooling.

The rootfs pipeline is the same on every architecture. What differs is a small
set of names and one structural fact: a rootfs for a foreign architecture has to
be built through an emulated execution boundary, while a rootfs for the host
architecture runs natively. This table is the single place that records both, so
the shell builder never hardcodes either.

`--print-fields` emits one field per line for the shell builder, in the same
style as `profiles.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import argparse


@dataclass(frozen=True)
class Architecture:
    """Everything the builder needs to know about one target architecture."""

    #: The Debian architecture name, as `debootstrap --arch` and the apt
    #: `Packages` index paths spell it.
    debian_arch: str
    #: The short name used in `target/debian-<slug>/...` output directories.
    arch_slug: str
    #: The GNU target triple the Nix cross packages and the lmbench runtime use.
    platform: str
    #: The dynamic loader the produced rootfs must contain.
    loader_path: str
    #: The user-mode emulator required to execute this architecture's binaries,
    #: or empty when the host can run them directly.
    emulator: str
    #: Whether the build host can execute this architecture natively.
    native: bool
    #: The default compiler for small guest probes.
    cross_cc: str


#: The host architecture, spelled as `uname -m` reports it.
_HOST_TO_DEBIAN_ARCH = {
    "x86_64": "amd64",
    "aarch64": "arm64",
    "riscv64": "riscv64",
    "loongarch64": "loong64",
}


_ARCHITECTURES = {
    "riscv64": Architecture(
        debian_arch="riscv64",
        arch_slug="riscv",
        platform="riscv64-unknown-linux-gnu",
        loader_path="/lib/ld-linux-riscv64-lp64d.so.1",
        emulator="qemu-riscv64-static",
        native=False,
        cross_cc="riscv64-linux-gnu-gcc",
    ),
    "amd64": Architecture(
        debian_arch="amd64",
        arch_slug="x86",
        platform="x86_64-unknown-linux-gnu",
        loader_path="/lib64/ld-linux-x86-64.so.2",
        emulator="",
        native=True,
        cross_cc="cc",
    ),
}


def get_architecture(name: str) -> Architecture:
    """Returns a frozen architecture or rejects an unknown identity."""

    try:
        return _ARCHITECTURES[name]
    except KeyError as error:
        known = ", ".join(sorted(_ARCHITECTURES))
        raise ValueError(f"unknown rootfs architecture: {name} (known: {known})") from error


def host_debian_arch() -> str:
    """Returns the host's Debian architecture name, or empty when unknown."""

    import platform as host_platform

    return _HOST_TO_DEBIAN_ARCH.get(host_platform.machine(), "")


def is_native(architecture: Architecture) -> bool:
    """Returns whether `architecture` can be built on this host without an emulator."""

    host = host_debian_arch()
    return bool(host) and host == architecture.debian_arch


def main(arguments: Sequence[str] | None = None) -> int:
    """Prints one architecture as newline-delimited fields for the shell builder."""

    parser = argparse.ArgumentParser(prog="arch")
    parser.add_argument("--architecture", required=True)
    values = parser.parse_args(arguments)
    try:
        architecture = get_architecture(values.architecture)
    except ValueError as error:
        parser.error(str(error))
    print(architecture.debian_arch)
    print(architecture.arch_slug)
    print(architecture.platform)
    print(architecture.loader_path)
    print(architecture.emulator)
    print("native" if is_native(architecture) else "emulated")
    print(architecture.cross_cc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
