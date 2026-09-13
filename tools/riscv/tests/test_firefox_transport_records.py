# SPDX-License-Identifier: MPL-2.0

import json
import unittest

from tools.riscv.diagnostics.firefox_transport_records import (
    FirefoxTransportRecords,
    parse_record,
)


def marker(stage="input.ready", sequence=1, request=0):
    value = {
        "version": 1,
        "stage": stage,
        "pid": 67,
        "sequence": sequence,
        "available": 1176,
        "header": 5,
        "expected": 1171,
        "received": 1171,
        "request": request,
    }
    return b"A_FF_TRANSPORT " + json.dumps(value).encode() + b"\r\n"


class FirefoxTransportRecordsTests(unittest.TestCase):
    def test_accepts_systemd_prefix_and_preserves_scalar_fields(self):
        records = FirefoxTransportRecords()
        row = records.consume(b"browser-web-firefox[95]: " + marker())[0]
        self.assertEqual(row["available"], 1176)
        self.assertEqual(row["pid"], 67)

    def test_rejects_unknown_stage_fields_and_non_integer_scalars(self):
        base = json.loads(marker()[len(b"A_FF_TRANSPORT ") :])
        for value in (
            dict(base, stage="secret"),
            dict(base, available=True),
            dict(base, payload="secret"),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_record("A_FF_TRANSPORT " + json.dumps(value))

    def test_accepts_wait_registration_and_non_consuming_probe_stages(self):
        self.assertEqual(
            parse_record(marker("wait.arm").decode().rstrip())["stage"], "wait.arm"
        )
        self.assertEqual(
            parse_record(marker("probe.available").decode().rstrip())["available"], 1176
        )
        self.assertEqual(
            parse_record(marker("probe.error").decode().rstrip())["stage"],
            "probe.error",
        )

    def test_scans_each_complete_line_once(self):
        records = FirefoxTransportRecords()
        first = marker()
        partial = marker("packet.dispatch", 2, 4)[:-2]
        self.assertEqual(len(records.consume(first + partial)), 1)
        self.assertEqual(records.consume(first + partial), [])
        self.assertEqual(records.consume(first + partial + b"\r\n")[0]["request"], 4)


if __name__ == "__main__":
    unittest.main()
