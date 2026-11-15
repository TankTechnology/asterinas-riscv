#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The campus portal login: encoder, reply parsing and the address rule."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from tools.riscv.board import megrez_portal_login as portal

PAYLOAD = (
    '{"username":"u","password":"p","ip":"1.2.3.4","acid":"1","enc_ver":"srun_bx1"}'
)


class EncoderTests(unittest.TestCase):
    def test_encoding_matches_the_portal_vector(self) -> None:
        # Captured from a run the portal accepted the shape of. It pins the
        # trailing length word: without it the server answers auth_info_error,
        # which reads like a wrong password and sends you hunting for the wrong
        # problem.
        expected = (
            "{SRBX1}noHsxzIoxzu1in+5N3BJUk9m/dGE0mhVrW8Zy78kUYh8M86OoilQHJqHKEaGQauO5iHM"
            "+N8ybV+AJwRWTsE2RdMcURupQN5KRVo/t0V+/B1pD+FY"
        )
        self.assertEqual(portal.encode(PAYLOAD, "0123456789abcdef"), expected)


class ReplyTests(unittest.TestCase):
    def test_res_is_read_even_when_error_msg_comes_first_and_is_empty(self) -> None:
        reply = (
            'cb1({"client_ip":"1.2.3.4","ecode":0,"error":"auth_info_error",'
            '"error_msg":"","online_ip":"1.2.3.4","res":"auth_info_error"})'
        )
        self.assertEqual(portal._result(reply), "auth_info_error")

    def test_a_successful_reply_reads_as_login_ok(self) -> None:
        self.assertEqual(portal._result('cb1({"res":"login_ok"})'), "login_ok")


class AddressRuleTests(unittest.TestCase):
    def test_a_login_for_another_address_is_refused_before_it_is_sent(self) -> None:
        # The portal signs the exchange with the address the request comes
        # from, so asking it to log in a different address only produces
        # sign_error. Say so instead of sending a request that cannot work.
        challenge = 'cb1({"challenge":"abc","client_ip":"10.0.0.9"})'
        with mock.patch.object(portal, "_fetch", return_value=challenge):
            with self.assertRaises(SystemExit) as raised:
                portal.login("u", "p", "192.0.2.10", "1")
        self.assertIn("10.0.0.9", str(raised.exception))

    def test_the_signature_uses_the_client_address(self) -> None:
        challenge = 'cb1({"challenge":"abc","client_ip":"10.0.0.9"})'
        sent: list[str] = []

        def fake_fetch(url: str) -> str:
            sent.append(url)
            if "get_challenge" in url:
                return challenge
            return 'cb1({"res":"login_ok"})'

        with mock.patch.object(portal, "_fetch", side_effect=fake_fetch):
            self.assertEqual(portal.login("u", "p", None, "1"), "login_ok")
        login_url = sent[-1]
        self.assertIn("ip=10.0.0.9", login_url)
        self.assertNotIn("ip=192.0.2.10", login_url)


if __name__ == "__main__":
    unittest.main()
