#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Which Debian packages the compatibility matrix exercises.

Every entry names the package to install, how to run it, and what a working
run looks like. The guest probe turns this into `APP <name> PASS|FAIL` lines.
Adding an application here is the whole change; no shell edit is needed.
"""

from __future__ import annotations

from dataclasses import dataclass

CLI = "cli"
GUI = "gui"


@dataclass(frozen=True)
class App:
    name: str
    package: str
    kind: str
    command: str
    expect: str = ""
    window: str = ""


APPS: tuple[App, ...] = (
    # CLI: the Debian userland most packages assume.
    App("git", "git", CLI, "git --version", "git version"),
    App("python3", "python3", CLI, "python3 -c 'print(1 + 1)'", "2"),
    App("tree", "tree", CLI, "tree --version", "tree v"),
    App("jq", "jq", CLI, "echo '{\"a\": 1}' | jq .a", "1"),
    App("file", "file", CLI, "file -L /bin/sh", "ELF"),
    App("htop", "htop", CLI, "htop --version", "htop"),
    App("less", "less", CLI, "less --version | head -1", "less"),
    App(
        "rsync",
        "rsync",
        CLI,
        "rm -rf /tmp/rsync-a /tmp/rsync-b; mkdir -p /tmp/rsync-a; "
        "echo ok >/tmp/rsync-a/f; rsync -a /tmp/rsync-a/ /tmp/rsync-b/; "
        "cat /tmp/rsync-b/f",
        "ok",
    ),
    App(
        "strace",
        "strace",
        CLI,
        "strace -o /dev/null sh -c 'exit 0' && echo traced",
        "traced",
    ),
    App(
        "lsof",
        "lsof",
        CLI,
        'test "$(lsof -p $$ | wc -l)" -ge 2 && echo lsof-ok',
        "lsof-ok",
    ),
    App(
        "zip",
        "zip unzip",
        CLI,
        "rm -rf /tmp/zt; mkdir /tmp/zt; printf x >/tmp/zt/a; "
        "cd /tmp/zt && zip -q z.zip a && rm a && unzip -q z.zip && cat a",
        "x",
    ),
    # GUI: Xlib, GTK 2/3 and a plain image viewer.
    App("xlogo", "x11-apps", GUI, "xlogo", window="xlogo"),
    App("mousepad", "mousepad", GUI, "mousepad", window="Mousepad"),
    App("galculator", "galculator", GUI, "galculator", window="Galculator"),
    App(
        "feh",
        "feh",
        GUI,
        "feh /usr/share/doc/scrot/scrot.png",
        window="feh",
    ),
)

BY_NAME = {app.name: app for app in APPS}


def select(names: list[str]) -> list[App]:
    """Return the requested applications in manifest order."""

    unknown = sorted(set(names) - set(BY_NAME))
    if unknown:
        raise ValueError(f"unknown application: {', '.join(unknown)}")
    wanted = set(names)
    return [app for app in APPS if app.name in wanted]


def packages(apps: list[App]) -> list[str]:
    """Return the unique package names, in first-seen order."""

    seen: list[str] = []
    for app in apps:
        for package in app.package.split():
            if package not in seen:
                seen.append(package)
    return seen
