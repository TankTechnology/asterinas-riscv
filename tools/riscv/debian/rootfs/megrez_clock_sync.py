#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Set the Megrez guest clock from one bounded HTTP Date header.

The board has no battery-backed clock: it powers up on 2026-04-13 every time
and then every real certificate is "not yet valid". Firefox says "Your Computer
Clock is Wrong" and curl rejects every HTTPS site, while plain HTTP keeps
working -- which is why this looks like a network fault for a long time.

Several sources can answer that header and none of them is always available:

* the campus gateway answers plain HTTP even before the portal login (it serves
  its own redirect page), and its address is on the kernel command line;
* the build host's CONNECT proxy, on the older lab network;
* the sites themselves, once the portal session is up.

They are tried in that order.  Everything is bounded: a source that does not
answer within the timeout is simply skipped, because a wrong clock is bad but
a desktop that never comes up is worse.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
import http.client
import json
import math
import os
import subprocess
from typing import Any
from urllib.parse import urlsplit

CLOCK_URL = "http://www.baidu.com/"
DEFAULT_PROXY_URL = "http://10.100.19.216:17893"
CMDLINE = "/proc/cmdline"

DateFactory = Callable[..., Any]
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ClockSource:
    """One place to ask for a Date header."""

    host: str
    port: int
    path: str
    host_header: str

    @classmethod
    def direct(cls, host: str, port: int = 80) -> "ClockSource":
        return cls(host=host, port=port, path="/", host_header=host)

    @classmethod
    def proxy(cls, proxy_url: str) -> "ClockSource":
        host, port = _proxy_endpoint(proxy_url)
        parsed = urlsplit(CLOCK_URL)
        return cls(
            host=host,
            port=port,
            path=CLOCK_URL,
            host_header=parsed.hostname or "www.baidu.com",
        )

    def describe(self) -> str:
        return f"{self.host_header} via {self.host}:{self.port}"


class ClockSourceError(ValueError):
    """No source produced a usable Date header."""


def _proxy_endpoint(proxy_url: str) -> tuple[str, int]:
    parsed = urlsplit(proxy_url)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise ValueError("proxy URL must be an explicit HTTP host and port")
    return parsed.hostname, parsed.port


def _gateway_from_cmdline(cmdline: str) -> str | None:
    """Read the gateway out of `asterinas.net=<key>,<address>/<prefix>,<gateway>`."""

    for field in cmdline.split():
        if not field.startswith("asterinas.net="):
            continue
        parts = field.split("=", 1)[1].split(",")
        if len(parts) == 3 and parts[2].count(".") == 3:
            return parts[2]
    return None


def default_sources(
    proxy_url: str, cmdline: str | None = None
) -> list[ClockSource]:
    """The sources this board can use, in the order worth trying."""

    if cmdline is None:
        try:
            with open(CMDLINE, encoding="utf-8", errors="replace") as handle:
                cmdline = handle.read()
        except OSError:
            cmdline = ""
    sources: list[ClockSource] = []
    gateway = _gateway_from_cmdline(cmdline)
    if gateway is not None:
        sources.append(ClockSource.direct(gateway))
    sources.append(ClockSource.proxy(proxy_url))
    sources.append(ClockSource.direct("www.baidu.com"))
    return sources


def _read_date(
    source: ClockSource,
    *,
    timeout: float,
    connection_factory: DateFactory,
) -> int:
    """Return the Unix time in one source's Date header, or raise."""

    connection = connection_factory(source.host, source.port, timeout=timeout)
    try:
        connection.request(
            "HEAD",
            source.path,
            None,
            {"Host": source.host_header, "Connection": "close"},
        )
        response = connection.getresponse()
        response.read(1)
        # The status code is irrelevant here: the campus gateway answers 404
        # for a HEAD of its own root, and every HTTP/1.1 server still has to
        # send a Date header on its responses. An error status is not a reason
        # to throw away the one piece of information being collected.
        date_header = response.getheader("Date")
        if not isinstance(date_header, str):
            raise ValueError("HTTP Date header is missing")
        try:
            parsed_date = parsedate_to_datetime(date_header)
        except (TypeError, ValueError) as error:
            raise ValueError("HTTP Date header is invalid") from error
        if (
            parsed_date.tzinfo is None
            or parsed_date.utcoffset() != timezone.utc.utcoffset(parsed_date)
            or format_datetime(parsed_date, usegmt=True) != date_header
            or not 2024 <= parsed_date.year <= 2100
        ):
            raise ValueError("HTTP Date header is not canonical GMT")
        return int(parsed_date.timestamp())
    finally:
        connection.close()


def _set_clock(unix_seconds: int, *, timeout: float, runner: Runner) -> None:
    runner(
        ("date", "--utc", "--set", f"@{unix_seconds}"),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def synchronize_clock(
    proxy_url: str,
    *,
    timeout: float,
    connection_factory: DateFactory = http.client.HTTPConnection,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    """Validate one RFC 7231 Date header from the proxy, set UTC, return evidence."""

    _validate_timeout(timeout)
    source = ClockSource.proxy(proxy_url)
    unix_seconds = _read_date(source, timeout=timeout, connection_factory=connection_factory)
    _set_clock(unix_seconds, timeout=timeout, runner=runner)
    return {
        "marker": "ASTERINAS_CLOCK_SYNC_READY",
        "proxy": proxy_url,
        "source": "http-date",
        "unix_seconds": unix_seconds,
    }


def synchronize_from_sources(
    sources: Iterable[ClockSource],
    *,
    timeout: float,
    connection_factory: DateFactory = http.client.HTTPConnection,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    """Try each source in turn and set UTC from the first Date header that works."""

    _validate_timeout(timeout)
    failures: list[str] = []
    for source in sources:
        try:
            unix_seconds = _read_date(
                source, timeout=timeout, connection_factory=connection_factory
            )
        except (OSError, ValueError) as error:
            failures.append(f"{source.describe()}: {error}")
            continue
        _set_clock(unix_seconds, timeout=timeout, runner=runner)
        return {
            "marker": "ASTERINAS_CLOCK_SYNC_READY",
            "source": source.describe(),
            "unix_seconds": unix_seconds,
        }
    raise ClockSourceError("; ".join(failures) or "no clock source configured")


def _validate_timeout(timeout: float) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 60
    ):
        raise ValueError("timeout must be in (0, 60]")


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="megrez-clock-sync")
    parser.add_argument("--proxy", default=DEFAULT_PROXY_URL)
    # The unit runs before the desktop starts, so the whole search has to fit
    # in a few seconds; the gateway answers in milliseconds when it answers.
    parser.add_argument("--timeout", type=float, default=5.0)
    values = parser.parse_args(arguments)
    if os.environ.get("ASTERINAS_DESKTOP_OFFLINE") == "1":
        print(
            json.dumps(
                {"marker": "ASTERINAS_CLOCK_SYNC_SKIPPED", "reason": "offline"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    try:
        sources = default_sources(values.proxy)
        evidence = synchronize_from_sources(sources, timeout=values.timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
