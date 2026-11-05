#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs.megrez_clock_sync import (
    ClockSource,
    ClockSourceError,
    _gateway_from_cmdline,
    default_sources,
    main,
    synchronize_clock,
    synchronize_from_sources,
)


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
    def test_desktop_service_gates_the_clock_sync_on_the_kernel_command_line(
        self,
    ) -> None:
        # A unit pulled in by basic.target with DefaultDependencies=no never
        # ran on the board, so the sync hangs off the desktop service, which
        # does start.
        builder = (
            Path(__file__).parents[1] / "debian/rootfs/build_rootfs.sh"
        ).read_text()
        gate = [
            line
            for line in builder.splitlines()
            if line.startswith("ExecStartPre=-/bin/sh -c 'case")
        ]

        self.assertEqual(len(gate), 1, "the desktop unit must carry exactly one gate")
        self.assertIn("cat /proc/cmdline", gate[0])
        self.assertIn("ASTERINAS_DESKTOP_OFFLINE=1", gate[0])
        self.assertIn("/usr/lib/asterinas/megrez-clock-sync", gate[0])

    def test_offline_desktop_skips_network_clock_sync(self) -> None:
        with mock.patch.dict("os.environ", {"ASTERINAS_DESKTOP_OFFLINE": "1"}):
            with mock.patch(
                "tools.riscv.debian.rootfs.megrez_clock_sync.synchronize_clock"
            ) as sync:
                with mock.patch("builtins.print") as print_:
                    self.assertEqual(main([]), 0)

        sync.assert_not_called()
        print_.assert_called_once_with(
            '{"marker":"ASTERINAS_CLOCK_SYNC_SKIPPED","reason":"offline"}'
        )

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

    def test_gateway_is_read_from_the_kernel_command_line(self) -> None:
        cmdline = (
            "console=ttyS0 init=/init asterinas.mmc_write_partition2 "
            "asterinas.net=eic7700-rj45,192.0.2.10/24,192.0.2.1 "
            "-- --root-init=systemd"
        )

        self.assertEqual(_gateway_from_cmdline(cmdline), "192.0.2.1")
        self.assertIsNone(_gateway_from_cmdline("console=ttyS0"))
        self.assertIsNone(_gateway_from_cmdline("asterinas.net=eic7700,bad,not-an-ip"))

    def test_the_gateway_is_asked_before_the_build_host_proxy(self) -> None:
        sources = default_sources(
            "http://10.100.19.216:17893",
            "asterinas.net=eic7700-rj45,192.0.2.10/24,192.0.2.1",
        )

        self.assertEqual(sources[0].host, "192.0.2.1")
        self.assertEqual(sources[0].path, "/")
        self.assertEqual(sources[1].host, "10.100.19.216")

    def test_a_silent_source_falls_through_to_the_next(self) -> None:
        connections = []

        class _Dead:
            def request(self, *_args: object, **_kwargs: object) -> None:
                raise OSError("Network is unreachable")

            def close(self) -> None:
                pass

        class _Live(_Connection):
            def __init__(self) -> None:
                super().__init__()

        def factory(host: str, port: int, **_kwargs: object) -> object:
            connections.append((host, port))
            return _Dead() if host == "192.0.2.1" else _Live()

        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(("date",), 0, "", "")
        )

        evidence = synchronize_from_sources(
            [ClockSource.direct("192.0.2.1"), ClockSource.direct("www.baidu.com")],
            timeout=5,
            connection_factory=factory,
            runner=runner,
        )

        self.assertEqual(connections, [("192.0.2.1", 80), ("www.baidu.com", 80)])
        runner.assert_called_once()
        self.assertEqual(evidence["source"], "www.baidu.com via www.baidu.com:80")
        self.assertEqual(evidence["unix_seconds"], 1789099506)

    def test_an_error_status_still_carries_a_usable_date(self) -> None:
        # The campus gateway answers 404 to a HEAD of its root.
        connection = _Connection()
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(("date",), 0, "", "")
        )
        with mock.patch.object(_Response, "status", 404):
            evidence = synchronize_from_sources(
                [ClockSource.direct("192.0.2.1")],
                timeout=5,
                connection_factory=mock.Mock(return_value=connection),
                runner=runner,
            )

        runner.assert_called_once()
        self.assertEqual(evidence["unix_seconds"], 1789099506)

    def test_no_source_answering_is_an_error_and_never_sets_the_clock(self) -> None:
        class _Dead:
            def request(self, *_args: object, **_kwargs: object) -> None:
                raise OSError("Network is unreachable")

            def close(self) -> None:
                pass

        runner = mock.Mock()

        with self.assertRaisesRegex(ClockSourceError, "Network is unreachable"):
            synchronize_from_sources(
                [ClockSource.direct("192.0.2.1")],
                timeout=5,
                connection_factory=mock.Mock(return_value=_Dead()),
                runner=runner,
            )
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
