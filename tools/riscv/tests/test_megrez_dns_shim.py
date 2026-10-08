#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs import megrez_dns_shim
from tools.riscv.debian.rootfs.megrez_dns_shim import (
    _PROBE,
    serve,
    publish_resolver,
    probe,
    resolve,
    upstream,
)


class _Stream:
    """A socket double that answers with a scripted byte stream."""

    def __init__(self, answer: bytes, chunk: int = 3) -> None:
        self._answer = answer
        self._chunk = chunk
        self.sent = b""
        self.closed = False

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, size: int) -> bytes:
        size = min(size, self._chunk)
        chunk, self._answer = self._answer[:size], self._answer[size:]
        return chunk

    def close(self) -> None:
        self.closed = True


class UpstreamTests(unittest.TestCase):
    def test_unset_host_disables_the_shim(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(upstream())

    def test_default_port_is_used_when_only_the_host_is_given(self) -> None:
        with mock.patch.dict(
            os.environ, {"ASTERINAS_DESKTOP_DNS_HOST": "10.100.19.216"}, clear=True
        ):
            self.assertEqual(
                upstream(),
                ("10.100.19.216", megrez_dns_shim.DEFAULT_UPSTREAM_PORT),
            )

    def test_an_invalid_port_disables_the_shim(self) -> None:
        for port in ("0", "65536", "not-a-port", "-1"):
            with self.subTest(port=port), mock.patch.dict(
                os.environ,
                {
                    "ASTERINAS_DESKTOP_DNS_HOST": "10.100.19.216",
                    "ASTERINAS_DESKTOP_DNS_PORT": port,
                },
                clear=True,
            ):
                self.assertIsNone(upstream())

    def test_a_declared_port_is_honoured(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ASTERINAS_DESKTOP_DNS_HOST": "10.100.19.216",
                "ASTERINAS_DESKTOP_DNS_PORT": " 15354 ",
            },
            clear=True,
        ):
            self.assertEqual(upstream(), ("10.100.19.216", 15354))


class ResolveTests(unittest.TestCase):
    def test_query_is_length_prefixed_and_the_answer_reassembled(self) -> None:
        stream = _Stream(b"\x00\x04abcd")

        with mock.patch.object(
            megrez_dns_shim.socket, "create_connection", return_value=stream
        ) as factory:
            answer = resolve(b"query", ("10.100.19.216", 15354))

        self.assertEqual(answer, b"abcd")
        self.assertEqual(stream.sent, b"\x00\x05query")
        self.assertTrue(stream.closed)
        factory.assert_called_once_with(("10.100.19.216", 15354), timeout=5.0)

    def test_a_truncated_answer_is_an_error(self) -> None:
        stream = _Stream(b"\x00\x08ab")

        with mock.patch.object(
            megrez_dns_shim.socket, "create_connection", return_value=stream
        ):
            with self.assertRaises(OSError):
                resolve(b"query", ("10.100.19.216", 15354))


class ProbeTests(unittest.TestCase):
    def test_a_matching_transaction_id_is_an_answer(self) -> None:
        with mock.patch.object(
            megrez_dns_shim, "resolve", return_value=_PROBE[:2] + b"rest"
        ):
            self.assertTrue(probe(("10.100.19.216", 15354)))

    def test_a_mismatched_transaction_id_is_not_an_answer(self) -> None:
        with mock.patch.object(
            megrez_dns_shim, "resolve", return_value=b"\xff\xffrest"
        ):
            self.assertFalse(probe(("10.100.19.216", 15354)))

    def test_a_failed_tunnel_is_not_an_answer(self) -> None:
        with mock.patch.object(
            megrez_dns_shim, "resolve", side_effect=OSError("refused")
        ), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(probe(("10.100.19.216", 15354)))


class PublishTests(unittest.TestCase):
    def _with_resolv_conf(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "resolv.conf"
        return mock.patch.object(megrez_dns_shim, "RESOLV_CONF", str(path)), path

    def test_the_resolver_is_pointed_at_the_loopback_shim(self) -> None:
        patcher, path = self._with_resolv_conf()
        with patcher:
            publish_resolver()
        self.assertEqual(path.read_text(), "nameserver 127.0.0.1\n")

    def test_an_already_correct_file_is_left_alone(self) -> None:
        patcher, path = self._with_resolv_conf()
        with patcher:
            publish_resolver()
            before = path.stat().st_mtime_ns
            publish_resolver()
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(path.read_text(), "nameserver 127.0.0.1\n")


class _Stop(Exception):
    """Ends the serve loop, which has no exit of its own."""


class ServeTests(unittest.TestCase):
    def _server(self, *receives: object) -> mock.Mock:
        server = mock.Mock()
        server.recvfrom.side_effect = list(receives)
        return server

    def test_a_query_is_answered_to_the_peer_that_sent_it(self) -> None:
        peer = ("127.0.0.1", 4242)
        server = self._server((b"query", peer), _Stop())

        with mock.patch.object(
            megrez_dns_shim, "resolve", return_value=b"answer"
        ) as resolver, self.assertRaises(_Stop):
            serve(server, ("10.100.19.216", 15354))

        resolver.assert_called_once_with(b"query", ("10.100.19.216", 15354))
        server.sendto.assert_called_once_with(b"answer", peer)

    def test_a_failed_query_is_answered_with_nothing(self) -> None:
        server = self._server((b"query", ("127.0.0.1", 4242)), _Stop())

        with mock.patch.object(
            megrez_dns_shim, "resolve", side_effect=OSError("refused")
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(_Stop):
            serve(server, ("10.100.19.216", 15354))

        server.sendto.assert_not_called()


class MainTests(unittest.TestCase):
    def test_main_does_not_touch_the_resolver_when_the_probe_fails(self) -> None:
        """The fail-safe: a dead tunnel leaves the guest as it found it."""

        with mock.patch.dict(
            os.environ,
            {"ASTERINAS_DESKTOP_DNS_HOST": "10.100.19.216"},
            clear=True,
        ), mock.patch.object(
            megrez_dns_shim, "probe", return_value=False
        ), mock.patch.object(
            megrez_dns_shim, "publish_resolver"
        ) as publish:
            self.assertEqual(megrez_dns_shim.main(), 1)

        publish.assert_not_called()

    def test_an_unconfigured_shim_reports_its_own_status(self) -> None:
        """Unconfigured must not look like a clean run.

        Exiting zero here is what left the board with no resolver and nothing
        in the unit's state to say why: systemd recorded "inactive (dead)", so
        neither a retry nor an inspection could find the cause. The unit maps
        this status to a stop rather than a restart, so it is reported once.
        """

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            megrez_dns_shim, "probe"
        ) as probe_mock, mock.patch.object(
            megrez_dns_shim, "publish_resolver"
        ) as publish, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                megrez_dns_shim.main(), megrez_dns_shim.EXIT_UNCONFIGURED
            )

        self.assertNotEqual(megrez_dns_shim.EXIT_UNCONFIGURED, 0)
        self.assertNotEqual(
            megrez_dns_shim.EXIT_UNCONFIGURED, megrez_dns_shim.EXIT_TUNNEL_DOWN
        )
        probe_mock.assert_not_called()
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
