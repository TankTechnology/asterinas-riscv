#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Single source for the offline physical desktop command line."""

from __future__ import annotations

from collections.abc import Sequence


PHYSICAL_REBOOT_AFTER = 1140

PHYSICAL_TOKENS = (
    "console=tty0",
    "loglevel=off",
    "asterinas.klog_capture=info",
    "init=/init",
    "asterinas.mmc_write_partition2",
    f"asterinas.reboot_after={PHYSICAL_REBOOT_AFTER}",
    "systemd.mask=asterinas-browser-web-evidence.service",
    "systemd.mask=asterinas-desktop-network.service",
    "systemd.mask=serial-getty@ttyS0.service",
    "systemd.mask=console-getty.service",
    "systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1",
    "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy",
    "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=127.0.0.1",
    "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=9",
    "--",
    "--root-init=systemd",
    "--debug-console=isolated-root",
    "--volatile-home",
)
PHYSICAL_SUFFIX = (
    "--root-init=systemd",
    "--debug-console=isolated-root",
    "--volatile-home",
)
NETWORK_INPUT_PREFIXES = (
    "asterinas.net=",
    "asterinas.neighbor=",
    "systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_",
)
STABILITY_EXCLUDED = NETWORK_INPUT_PREFIXES
DESKTOP_START_EXCLUDED = (*NETWORK_INPUT_PREFIXES, "asterinas.reboot_after=")


def physical_bootargs() -> str:
    """Return the fixed offline physical desktop command line."""

    return " ".join(PHYSICAL_TOKENS)


def _canonical_tokens(bootargs: str) -> list[str]:
    if not isinstance(bootargs, str):
        raise ValueError("physical bootargs must be text")
    tokens = bootargs.split()
    separator = tokens.index("--") if tokens.count("--") == 1 else -1
    if separator < 0 or tokens[separator + 1 :] != list(PHYSICAL_SUFFIX):
        raise ValueError("physical bootargs suffix is not canonical")
    if tokens[:separator].count("init=/init") != 1:
        raise ValueError("physical bootargs must contain one init selector")
    return tokens


def _filter(
    bootargs: str, excluded: Sequence[str], *, keep_recovery: bool
) -> str:
    tokens = _canonical_tokens(bootargs)
    result = [
        token
        for token in tokens
        if not any(token.startswith(prefix) for prefix in excluded)
    ]
    if result.count("asterinas.mmc_write_partition2") != 1:
        raise ValueError("physical bootargs lost the partition-write gate")
    if keep_recovery and not any(
        token.startswith("asterinas.reboot_after=") for token in result
    ):
        raise ValueError("physical bootargs lost the recovery timer")
    if not keep_recovery and any(
        token.startswith("asterinas.reboot_after=") for token in result
    ):
        raise ValueError("desktop start may not arm a recovery timer")
    return " ".join(result)


def boot_stability_bootargs(bootargs: str) -> str:
    """Remove network and fixture inputs while retaining recovery."""

    return _filter(bootargs, STABILITY_EXCLUDED, keep_recovery=True)


def desktop_start_bootargs(bootargs: str) -> str:
    """Return a local, read-only boot without an automatic reboot."""

    return _filter(bootargs, DESKTOP_START_EXCLUDED, keep_recovery=False)
