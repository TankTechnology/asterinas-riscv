#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Subsystem catalog for the guest selftests.

The catalog is the Linux-selftests equivalent of `tools/testing/selftests/`:
each subsystem owns an ordered list of cases that run against one booted
guest. The host runner boots once, selects the target from this catalog, and
runs the requested subsystems in the order below.
"""

from __future__ import annotations

from dataclasses import dataclass

MULTI_USER = "multi-user"
GRAPHICAL = "graphical"


@dataclass(frozen=True)
class Case:
    name: str
    summary: str


@dataclass(frozen=True)
class Subsystem:
    name: str
    target: str
    cases: tuple[Case, ...]

    @property
    def graphical(self) -> bool:
        return self.target == GRAPHICAL


# Order matters: the runner boots once and executes these in sequence.
SUBSYSTEMS: tuple[Subsystem, ...] = (
    Subsystem(
        "console",
        MULTI_USER,
        (
            Case("identity", "hostname and os-release name the test image"),
            Case("clock", "system time advances and can be set from the host"),
            Case("tty", "serial getty accepts a login and a command"),
            Case("sudo", "passwordless privilege escalation works"),
        ),
    ),
    Subsystem(
        "block",
        MULTI_USER,
        (
            Case("rootfs-rw", "the root filesystem is mounted read-write"),
            Case("fsck", "a clean unmount leaves no fsck errors"),
            Case("space", "available space matches the image contract"),
        ),
    ),
    Subsystem(
        "net",
        MULTI_USER,
        (
            Case("loopback", "loopback TCP and UDP work"),
            Case("route", "a default route exists"),
            Case("dns", "resolver lookup succeeds"),
            Case("http", "the selected HTTP endpoint answers"),
        ),
    ),
    Subsystem(
        "power",
        MULTI_USER,
        (
            Case("reboot", "systemd reboots and the guest boots again"),
            Case("poweroff", "SIGPWR reaches target and SBI powers off"),
        ),
    ),
    Subsystem(
        "display",
        GRAPHICAL,
        (
            Case("xorg", "Xorg owns the framebuffer on :0"),
            Case("session", "window manager, panel and file manager are up"),
            Case("screenshot", "a root screendump has non-black pixels"),
        ),
    ),
    Subsystem(
        "input",
        GRAPHICAL,
        (
            Case("keyboard", "evdev keyboard delivers a key to a client"),
            Case("pointer", "evdev pointer moves and clicks"),
        ),
    ),
    Subsystem(
        "browser",
        GRAPHICAL,
        (
            Case("navigate", "Firefox opens a remote page"),
            Case("render", "the page renders with a non-blank screenshot"),
            Case("search", "a typed query produces a result page"),
        ),
    ),
    Subsystem(
        "apps",
        GRAPHICAL,
        (
            Case("terminal", "terminal starts and runs a command"),
            Case("editor", "editor opens and saves a file"),
            Case("files", "file manager opens a directory"),
            Case("archive", "zip/unzip roundtrip works"),
        ),
    ),
    Subsystem(
        "drm",
        GRAPHICAL,
        (
            Case("nodes", "card and render nodes exist under /dev/dri"),
            Case("render", "a render-node client allocates and draws"),
            Case("kms", "a KMS mode is set without falling back to fbdev"),
        ),
    ),
)

BY_NAME = {subsystem.name: subsystem for subsystem in SUBSYSTEMS}


def select(names: list[str]) -> list[Subsystem]:
    """Return the requested subsystems in catalog order."""

    unknown = sorted(set(names) - set(BY_NAME))
    if unknown:
        raise ValueError(f"unknown subsystem: {', '.join(unknown)}")
    wanted = set(names)
    return [subsystem for subsystem in SUBSYSTEMS if subsystem.name in wanted]


def boot_target(subsystems: list[Subsystem]) -> str:
    """Return the highest target required by the selection."""

    if any(subsystem.graphical for subsystem in subsystems):
        return GRAPHICAL
    return MULTI_USER
