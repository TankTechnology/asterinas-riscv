#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_CORE_MILESTONES,
    DESKTOP_M4_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import _UBOOT_COMMAND_SAFE_LIMIT
from tools.riscv.debian.rootfs import desktop_network_gate as network_gate
from tools.riscv.debian.rootfs.desktop_network_gate import (
    DESKTOP_NETWORK_MILESTONES,
    DESKTOP_NETWORK_QEMU_MILESTONES,
    classify_desktop_network,
    classify_desktop_network_qemu,
    classify_network_qemu,
)
from tools.riscv.debian.rootfs.desktop_network_qemu_gate import (
    DESKTOP_NETWORK_QEMU_BOOTARGS,
    DesktopNetworkQemuOperations,
    NetworkQemuOperations,
    QemuExpectedFailure,
    QemuGateTarget,
    _parse_target,
    classify_qemu_web_network,
    desktop_network_qemu_argv,
    qemu_web_network_bootargs,
)
from tools.riscv.debian.rootfs.contract import ContractError, load_manifest
from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig as RootfsGateConfig
from tools.riscv.megrez_network_fixture import (
    BROWSER_DOWNLOAD,
    BROWSER_DOWNLOAD_PATH,
    BROWSER_DOWNLOAD_SHA256,
    FIXTURE_PATH,
    PAYLOAD,
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    FixtureConfig,
    FixtureServer,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
MAKEFILE = REPOSITORY_ROOT / "Makefile"
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_network_evidence.sh"
)
DESKTOP_M4_SESSION_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_session.sh"
)
DESKTOP_M4_EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_evidence.sh"
)
SAFE_REBOOT_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_safe_reboot.sh"
CLOCK_BOOTSTRAP_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_clock_bootstrap.sh"
MEGREZ_TCP_PROBE_SOURCE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_tcp_probe_init.c"
)
EXPECTED_MEGREZ_MILESTONES = (
    "DEBIAN_NETWORK_LINK interface=eth0 address=10.100.19.200/21 state=lower-up",
    "DEBIAN_NETWORK_MEGREZ_PROXY endpoint=10.100.19.216:17893",
    "DEBIAN_NETWORK_STRESS requests=20 bytes=1310720 "
    f"sha256={PAYLOAD_SHA256} endpoint=10.100.19.216:17894",
    "DEBIAN_NETWORK_CLOCK source=http-date proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_MEGREZ_HTTPS host=www.baidu.com status=200 address=10.100.19.200 proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_MEGREZ_ASSET host=www.baidu.com resource=logo-png proxy=10.100.19.216:17893",
    "DEBIAN_NETWORK_MEGREZ_READY mode=static-rj45-host-proxy",
)
EXPECTED_MEGREZ_CONSOLE = (
    EXPECTED_MEGREZ_MILESTONES[:2]
    + ("DEBIAN_NETWORK_STRESS_START requests=20 endpoint=10.100.19.216:17894",)
    + EXPECTED_MEGREZ_MILESTONES[2:4]
    + ("DEBIAN_NETWORK_DIAGNOSTIC phase=megrez-https attempt=1 stage=begin",)
    + EXPECTED_MEGREZ_MILESTONES[4:5]
    + ("DEBIAN_NETWORK_DIAGNOSTIC phase=megrez-asset stage=begin",)
    + EXPECTED_MEGREZ_MILESTONES[5:]
)
EXPECTED_QEMU_STRESS_MILESTONE = (
    "DEBIAN_NETWORK_STRESS requests=20 bytes=1310720 "
    f"sha256={PAYLOAD_SHA256} endpoint=10.0.2.2:17894"
)

class _DebianNetworkFixtures:
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def _web_network_transcript(self, mode: object) -> bytes:
        layers = getattr(network_gate, "NETWORK_LAYERS", ())
        records = [
            f"DEBIAN_WEB_NETWORK_LAYER mode={mode.value} layer={layer} status=pass"
            for layer in layers
        ]
        records.append(
            f"DEBIAN_WEB_NETWORK_READY mode={mode.value} layers={len(layers)}"
        )
        return ("\n".join(records) + "\n").encode()

    def _fake_network_tools(
        self, *, address: str, directory: Path | None = None
    ) -> tuple[Path, Path]:
        directory = self.directory if directory is None else directory
        bin_directory = directory / "bin"
        bin_directory.mkdir(exist_ok=True)
        ping_log = directory / "ping.log"
        ip = bin_directory / "ip"
        ip.write_text(
            f"""#!/bin/sh
if [ "$1 $2 $3 $4 $5" = "-o link show dev eth0" ]; then
    printf '%s\n' '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP'
    exit 0
fi
if [ "$1 $2 $3 $4 $5 $6 $7 $8" = "-o -4 addr show dev eth0 scope global" ]; then
    printf '%s\n' '2: eth0    inet {address} brd 10.100.23.255 scope global eth0'
    exit 0
fi
exit 2
""",
            encoding="utf-8",
        )
        ping = bin_directory / "ping"
        ping.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >"$ASTERINAS_NETWORK_PING_LOG"
exit 0
""",
            encoding="utf-8",
        )
        ip.chmod(0o755)
        ping.chmod(0o755)
        return bin_directory, ping_log

    def _physical_evidence_environment(
        self, directory: Path, *, cmdline: str
    ) -> tuple[dict[str, str], Path, Path, Path, Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        console = directory / "console"
        console.write_text("", encoding="utf-8")
        cmdline_path = directory / "cmdline"
        cmdline_path.write_text(cmdline + "\n", encoding="utf-8")
        resolv_conf = directory / "resolv.conf"
        resolv_conf.write_text("nameserver 192.0.2.53\n", encoding="utf-8")
        url_file = directory / "desktop-url"
        url_file.write_text("https://old.invalid/\n", encoding="utf-8")
        fake_bin, ping_log = self._fake_network_tools(
            address="10.100.19.200/21", directory=directory
        )
        getent_log = directory / "getent.log"
        curl_log = directory / "curl.log"
        date_log = directory / "date.log"
        fixture_payload = directory / "fixture.bin"
        fixture_payload.write_bytes(PAYLOAD)
        getent = fake_bin / "getent"
        getent.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_NETWORK_GETENT_LOG"
exit "${ASTERINAS_NETWORK_GETENT_STATUS:-0}"
""",
            encoding="utf-8",
        )
        curl = fake_bin / "curl"
        curl.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_NETWORK_CURL_LOG"
case "$*" in
    *asterinas-network-probe.bin*)
        previous=
        output_count=0
        for argument in "$@"; do
            if [ "$previous" = --output ]; then
                output_count=$((output_count + 1))
                if [ "${ASTERINAS_NETWORK_FIXTURE_SHORT:-0}" = 1 ]; then
                    head -c 65535 "$ASTERINAS_NETWORK_FIXTURE_PAYLOAD" >"$argument"
                elif [ "${ASTERINAS_NETWORK_FIXTURE_CORRUPT:-0}" = 1 ]; then
                    printf X >"$argument"
                    tail -c +2 "$ASTERINAS_NETWORK_FIXTURE_PAYLOAD" >>"$argument"
                else
                    cp "$ASTERINAS_NETWORK_FIXTURE_PAYLOAD" "$argument"
                fi
            fi
            previous="$argument"
        done
        [ "$output_count" -gt 0 ] || exit 95
        [ "${ASTERINAS_NETWORK_FIXTURE_STATUS:-0}" = 0 ] || exit "$ASTERINAS_NETWORK_FIXTURE_STATUS"
        ;;
    *http://www.baidu.com/*)
        [ "${ASTERINAS_NETWORK_CLOCK_STATUS:-0}" = 0 ] || exit "$ASTERINAS_NETWORK_CLOCK_STATUS"
        printf 'HTTP/1.1 200 OK\r\n'
        if [ "${ASTERINAS_NETWORK_CLOCK_DATE:-set}" != missing ]; then
            printf 'Date: %s\r\n' "${ASTERINAS_NETWORK_CLOCK_DATE:-Sat, 29 Aug 2026 02:02:25 GMT}"
        fi
        printf '\r\n'
        ;;
    *result.png*)
        output=
        previous=
        for argument in "$@"; do
            if [ "$previous" = --output ]; then output="$argument"; break; fi
            previous="$argument"
        done
        [ -n "$output" ] || exit 96
        if [ "${ASTERINAS_NETWORK_EMPTY_ASSET:-0}" = 1 ]; then : >"$output"; else printf PNG >"$output"; fi
        exit "${ASTERINAS_NETWORK_ASSET_STATUS:-0}"
        ;;
    *)
        if [ -n "${ASTERINAS_NETWORK_HTTPS_SLEEP:-}" ]; then
            sleep "$ASTERINAS_NETWORK_HTTPS_SLEEP"
        fi
        if [ "${ASTERINAS_NETWORK_HTTPS_STATUS:-0}" != 0 ]; then
            printf '%s\n' "${ASTERINAS_NETWORK_HTTPS_ERROR:-curl failed}" >&2
            exit "$ASTERINAS_NETWORK_HTTPS_STATUS"
        fi
        printf '%s\t%s' "${ASTERINAS_NETWORK_HTTP_CODE:-200}" "${ASTERINAS_NETWORK_LOCAL_IP:-10.100.19.200}"
        ;;
esac
""",
            encoding="utf-8",
        )
        date = fake_bin / "date"
        date.write_text(
            """#!/bin/sh
printf '%s\n' "$*" >>"$ASTERINAS_NETWORK_DATE_LOG"
exit "${ASTERINAS_NETWORK_DATE_STATUS:-0}"
""",
            encoding="utf-8",
        )
        getent.chmod(0o755)
        curl.chmod(0o755)
        date.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            PATH=f"{fake_bin}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_NETWORK_CONSOLE=str(console),
            ASTERINAS_DESKTOP_NETWORK_TIMEOUT_SECONDS="5",
            ASTERINAS_DESKTOP_NETWORK_CMDLINE_PATH=str(cmdline_path),
            ASTERINAS_DESKTOP_NETWORK_RESOLV_CONF=str(resolv_conf),
            ASTERINAS_DESKTOP_NETWORK_URL_FILE=str(url_file),
            ASTERINAS_NETWORK_PING_LOG=str(ping_log),
            ASTERINAS_NETWORK_GETENT_LOG=str(getent_log),
            ASTERINAS_NETWORK_CURL_LOG=str(curl_log),
            ASTERINAS_NETWORK_DATE_LOG=str(date_log),
            ASTERINAS_NETWORK_FIXTURE_PAYLOAD=str(fixture_payload),
            ASTERINAS_DESKTOP_PROXY_URL="http://10.100.19.216:17893",
            ASTERINAS_DESKTOP_PROXY_HOST="10.100.19.216",
            ASTERINAS_DESKTOP_PROXY_PORT="17893",
            ASTERINAS_DESKTOP_FIXTURE_URL=(f"http://10.100.19.216:17894{FIXTURE_PATH}"),
            ASTERINAS_DESKTOP_FIXTURE_SIZE=str(PAYLOAD_SIZE),
            ASTERINAS_DESKTOP_FIXTURE_SHA256=PAYLOAD_SHA256,
            ASTERINAS_DESKTOP_FIXTURE_REQUESTS="20",
        )
        return environment, console, resolv_conf, url_file, ping_log, curl_log

    def _web_network_environment(
        self,
        directory: Path,
        *,
        mode: str,
        fail_stage: str = "",
        explicit_medium: bool = True,
    ) -> tuple[dict[str, str], Path, Path, Path]:
        directory.mkdir(parents=True)
        fake_bin = directory / "bin"
        fake_bin.mkdir()
        console = directory / "console"
        console.write_text("", encoding="utf-8")
        resolv_conf = directory / "resolv.conf"
        resolv_conf.write_text("nameserver 192.0.2.53\n", encoding="utf-8")
        command_log = directory / "commands.log"
        uptime = directory / "uptime"
        uptime.write_text("10.00 8.00\n", encoding="utf-8")
        fixture_payload = directory / "fixture.bin"
        fixture_payload.write_bytes(PAYLOAD)
        medium_payload = directory / "medium.bin"
        medium_payload.write_bytes(BROWSER_DOWNLOAD)
        fixture_count = directory / "fixture-count"

        tools = {
            "ip": r"""#!/bin/sh
printf 'ip %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
case "$*" in
    "-o link show dev eth0")
        [ "$ASTERINAS_WEB_FAIL_STAGE" = link ] && exit 1
        printf '%s\n' '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP'
        ;;
    "-o -4 addr show dev eth0 scope global")
        [ "$ASTERINAS_WEB_FAIL_STAGE" = address ] && exit 1
        printf '%s\n' '2: eth0 inet 10.100.19.200/21 brd 10.100.23.255 scope global eth0'
        ;;
    neigh*)
        if [ "$ASTERINAS_WEB_FAIL_STAGE" = neighbor ]; then
            printf '%s\n' '10.100.16.1 dev eth0 INCOMPLETE'
        elif [ "$ASTERINAS_WEB_FAIL_STAGE" = neighbor-unobservable ]; then
            printf '%s\n' 'RTNETLINK answers: Operation not supported' >&2
            exit 1
        elif [ "$ASTERINAS_WEB_FAIL_STAGE" = neighbor-command ]; then
            exit 2
        else
            printf '%s\n' "$4 dev eth0 lladdr 4c:d6:29:18:93:43 REACHABLE"
        fi
        ;;
    *) exit 2 ;;
esac
""",
            "ping": r"""#!/bin/sh
printf 'ping %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
[ "$ASTERINAS_WEB_FAIL_STAGE" = reachability ] && exit 1
exit 0
""",
            "getent": r"""#!/bin/sh
printf 'getent %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
[ "$ASTERINAS_WEB_FAIL_STAGE" = dns ] && exit 2
printf '%s\n' '110.242.68.3 STREAM www.baidu.com'
""",
            "date": r"""#!/bin/sh
printf 'date %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
exit 0
""",
            "curl": r"""#!/bin/sh
printf 'curl %s\n' "$*" >>"$ASTERINAS_WEB_COMMAND_LOG"
output=
outputs=
headers=
previous=
for argument in "$@"; do
    if [ "$previous" = --output ]; then
        output="$argument"
        outputs="$outputs $argument"
    fi
    if [ "$previous" = --dump-header ]; then headers="$argument"; fi
    previous="$argument"
done
[ -z "$headers" ] || printf 'HTTP/1.1 200 OK\r\nDate: Sat, 29 Aug 2026 02:02:25 GMT\r\n\r\n' >"$headers"
case "$*" in
    *http://www.baidu.com/*)
        [ "$ASTERINAS_WEB_FAIL_STAGE" = http ] && exit 7
        printf 'HTTP/1.1 200 OK\r\nDate: Sat, 29 Aug 2026 02:02:25 GMT\r\n\r\n'
        ;;
    *result.png*)
        [ "$ASTERINAS_WEB_FAIL_STAGE" = baidu-asset-curl ] && exit 22
        [ -n "$output" ] || exit 96
        if [ "$ASTERINAS_WEB_FAIL_STAGE" = baidu-asset ]; then
            printf 'not-png' >"$output"
        else
            printf '\211PNG\r\n\032\nfixture' >"$output"
        fi
        ;;
    *https://www.baidu.com/*|*https://10.0.2.2:8446/*)
        count_file="$ASTERINAS_WEB_HTTPS_COUNT"
        count=0
        [ ! -f "$count_file" ] || count="$(cat "$count_file")"
        count=$((count + 1))
        printf '%s\n' "$count" >"$count_file"
        case "$ASTERINAS_WEB_FAIL_STAGE" in
            https-connect) exit 7 ;;
            https-tls) exit 60 ;;
            https-once) [ "$count" = 1 ] && exit 124 ;;
        esac
        printf '200\t10.100.19.200\t0.010\t0.020'
        ;;
    *medium.bin*|*browser-quality/download.bin*)
        [ -n "$output" ] || exit 97
        if [ "$ASTERINAS_WEB_FAIL_STAGE" = medium ]; then
            head -c 262143 "$ASTERINAS_WEB_MEDIUM_PAYLOAD" >"$output"
        else
            cp "$ASTERINAS_WEB_MEDIUM_PAYLOAD" "$output"
        fi
        ;;
    *asterinas-network-probe.bin*)
        [ -n "$output" ] || exit 98
        for output in $outputs; do
            count=0
            [ ! -f "$ASTERINAS_WEB_FIXTURE_COUNT" ] || count="$(cat "$ASTERINAS_WEB_FIXTURE_COUNT")"
            count=$((count + 1))
            printf '%s\n' "$count" >"$ASTERINAS_WEB_FIXTURE_COUNT"
            if [ "$ASTERINAS_WEB_FAIL_STAGE" = http ] && [ "$count" = 1 ]; then
                exit 7
            elif [ "$ASTERINAS_WEB_FAIL_STAGE" = repeat-timeout ] && [ "$count" -gt 1 ]; then
                exit 124
            elif [ "$ASTERINAS_WEB_FAIL_STAGE" = repeat ] && [ "$count" -gt 1 ]; then
                head -c 65535 "$ASTERINAS_WEB_FIXTURE_PAYLOAD" >"$output"
            else
                cp "$ASTERINAS_WEB_FIXTURE_PAYLOAD" "$output"
            fi
        done
        ;;
    *) exit 99 ;;
esac
""",
        }
        for name, source in tools.items():
            executable = fake_bin / name
            executable.write_text(source, encoding="utf-8")
            executable.chmod(0o755)

        proxy = {
            "ASTERINAS_DESKTOP_PROXY_URL": "http://10.100.19.216:17893",
            "ASTERINAS_DESKTOP_PROXY_HOST": "10.100.19.216",
            "ASTERINAS_DESKTOP_PROXY_PORT": "17893",
        }
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "ASTERINAS_DESKTOP_NETWORK_CONSOLE": str(console),
            "ASTERINAS_DESKTOP_NETWORK_TIMEOUT_SECONDS": "5",
            "ASTERINAS_DESKTOP_NETWORK_COMMAND_TIMEOUT_SECONDS": "2",
            "ASTERINAS_DESKTOP_NETWORK_RESOLV_CONF": str(resolv_conf),
            "ASTERINAS_DESKTOP_NETWORK_URL_FILE": str(directory / "desktop-url"),
            "ASTERINAS_DESKTOP_NETWORK_UPTIME_PATH": str(uptime),
            "ASTERINAS_WEB_NETWORK_MODE": mode,
            "ASTERINAS_WEB_NETWORK_NEIGHBOR_QUERY": "1",
            "ASTERINAS_WEB_NETWORK_ADDRESS": "10.100.19.200/21",
            "ASTERINAS_WEB_NETWORK_GATEWAY": "10.100.16.1",
            "ASTERINAS_WEB_NETWORK_RESOLVER": "10.100.16.1",
            "ASTERINAS_WEB_HTTPS_COUNT": str(directory / "https-count"),
            "ASTERINAS_DESKTOP_FIXTURE_URL": (
                f"http://10.100.19.216:17894{FIXTURE_PATH}"
            ),
            "ASTERINAS_DESKTOP_FIXTURE_SIZE": str(PAYLOAD_SIZE),
            "ASTERINAS_DESKTOP_FIXTURE_SHA256": PAYLOAD_SHA256,
            "ASTERINAS_DESKTOP_FIXTURE_REQUESTS": "20",
            "ASTERINAS_WEB_NETWORK_MEDIUM_URL": (
                "http://10.100.19.216:17894/medium.bin"
            ),
            "ASTERINAS_WEB_NETWORK_MEDIUM_SIZE": str(len(BROWSER_DOWNLOAD)),
            "ASTERINAS_WEB_NETWORK_MEDIUM_SHA256": BROWSER_DOWNLOAD_SHA256,
            "ASTERINAS_WEB_COMMAND_LOG": str(command_log),
            "ASTERINAS_WEB_FIXTURE_PAYLOAD": str(fixture_payload),
            "ASTERINAS_WEB_MEDIUM_PAYLOAD": str(medium_payload),
            "ASTERINAS_WEB_FIXTURE_COUNT": str(fixture_count),
            "ASTERINAS_WEB_FAIL_STAGE": fail_stage,
        }
        if mode == "proxy":
            environment.update(proxy)
        if not explicit_medium:
            for name in (
                "ASTERINAS_WEB_NETWORK_MEDIUM_URL",
                "ASTERINAS_WEB_NETWORK_MEDIUM_SIZE",
                "ASTERINAS_WEB_NETWORK_MEDIUM_SHA256",
            ):
                environment.pop(name)
        return environment, console, resolv_conf, command_log

    def _qemu_fixture_environment(self, directory: Path) -> dict[str, str]:
        payload = directory / "qemu-fixture.bin"
        payload.write_bytes(PAYLOAD)
        return {
            "ASTERINAS_NETWORK_FIXTURE_PAYLOAD": str(payload),
            "ASTERINAS_DESKTOP_FIXTURE_URL": (f"http://10.0.2.2:17894{FIXTURE_PATH}"),
            "ASTERINAS_DESKTOP_FIXTURE_SIZE": str(PAYLOAD_SIZE),
            "ASTERINAS_DESKTOP_FIXTURE_SHA256": PAYLOAD_SHA256,
            "ASTERINAS_DESKTOP_FIXTURE_REQUESTS": "20",
        }


__all__ = tuple(
    name
    for name in globals()
    if name not in {"annotations"} and not name.startswith("__")
)
