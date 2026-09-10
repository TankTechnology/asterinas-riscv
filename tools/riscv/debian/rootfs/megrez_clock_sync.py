#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Set the Megrez guest clock from one bounded HTTP proxy Date header."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
import http.client
import json
import math
import subprocess
from typing import Any
from urllib.parse import urlsplit


CLOCK_URL = "http://www.baidu.com/"
DEFAULT_PROXY_URL = "http://10.100.19.216:17893"


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


def synchronize_clock(
    proxy_url: str,
    *,
    timeout: float,
    connection_factory: Callable[..., Any] = http.client.HTTPConnection,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Validate one RFC 7231 Date header, set UTC, and return evidence."""

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 60
    ):
        raise ValueError("timeout must be in (0, 60]")
    host, port = _proxy_endpoint(proxy_url)
    connection = connection_factory(host, port, timeout=timeout)
    try:
        connection.request(
            "HEAD",
            CLOCK_URL,
            None,
            {"Host": "www.baidu.com", "Connection": "close"},
        )
        response = connection.getresponse()
        response.read(1)
        if not 200 <= response.status < 400:
            raise ValueError(f"HTTP clock request failed with status {response.status}")
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
        unix_seconds = int(parsed_date.timestamp())
        runner(
            ("date", "--utc", "--set", f"@{unix_seconds}"),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "marker": "ASTERINAS_CLOCK_SYNC_READY",
            "proxy": proxy_url,
            "source": "http-date",
            "unix_seconds": unix_seconds,
        }
    finally:
        connection.close()


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="megrez-clock-sync")
    parser.add_argument("--proxy", default=DEFAULT_PROXY_URL)
    parser.add_argument("--timeout", type=float, default=15.0)
    values = parser.parse_args(arguments)
    try:
        evidence = synchronize_clock(values.proxy, timeout=values.timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
