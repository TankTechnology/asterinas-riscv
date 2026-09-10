#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from tools.riscv.debian.rootfs.megrez_clock_sync import synchronize_clock


class _Response:
    status = 200

    @staticmethod
    def getheader(name: str) -> str | None:
        if name == "Date":
            return "Fri, 11 Sep 2026 04:05:06 GMT"
        return None

    @staticmethod
    def read(size: int) -> bytes:
        assert size == 1
        return b""


class _Connection:
    def __init__(self) -> None:
        self.request_args: tuple[object, ...] | None = None
        self.closed = False

    def request(self, *args: object, **_kwargs: object) -> None:
        self.request_args = args

    @staticmethod
    def getresponse() -> _Response:
        return _Response()

    def close(self) -> None:
        self.closed = True


class MegrezClockSyncTests(unittest.TestCase):
    def test_http_proxy_date_is_validated_and_applied_without_https(self) -> None:
        connection = _Connection()
        factory = mock.Mock(return_value=connection)
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(("date",), 0, "", "")
        )

        evidence = synchronize_clock(
            "http://10.100.19.216:17893",
            timeout=15,
            connection_factory=factory,
            runner=runner,
        )

        factory.assert_called_once_with("10.100.19.216", 17893, timeout=15)
        self.assertEqual(
            connection.request_args,
            (
                "HEAD",
                "http://www.baidu.com/",
                None,
                {"Host": "www.baidu.com", "Connection": "close"},
            ),
        )
        runner.assert_called_once_with(
            ("date", "--utc", "--set", "@1789099506"),
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertTrue(connection.closed)
        self.assertEqual(evidence["marker"], "ASTERINAS_CLOCK_SYNC_READY")
        self.assertEqual(evidence["source"], "http-date")
        self.assertEqual(evidence["unix_seconds"], 1789099506)

    def test_invalid_or_missing_date_never_changes_the_clock(self) -> None:
        connection = _Connection()
        runner = mock.Mock()
        with mock.patch.object(_Response, "getheader", return_value="not-a-date"):
            with self.assertRaisesRegex(ValueError, "Date"):
                synchronize_clock(
                    "http://10.100.19.216:17893",
                    timeout=15,
                    connection_factory=mock.Mock(return_value=connection),
                    runner=runner,
                )
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
