# SPDX-License-Identifier: MPL-2.0
"""Validate bounded scalar Firefox transport markers from a serial stream."""

import json
import re


PREFIX = "A_FF_TRANSPORT "
SERIAL_PREFIX = re.compile(rb"(?:browser-web-firefox\[[0-9]+\]: )?A_FF_TRANSPORT ")
MAX_LINE_BYTES = 512
FIELDS = frozenset(
    (
        "version",
        "stage",
        "pid",
        "sequence",
        "available",
        "header",
        "expected",
        "received",
        "request",
    )
)
STAGES = frozenset(
    (
        "wait.arm",
        "input.ready",
        "probe.available",
        "probe.error",
        "process.enter",
        "header.parsed",
        "process.read",
        "packet.complete",
        "json.ready",
        "packet.dispatch",
    )
)


def parse_record(line):
    line = re.sub(r"^browser-web-firefox\[[0-9]+\]: ", "", line, count=1)
    if not line.startswith(PREFIX) or len(line.encode()) > MAX_LINE_BYTES:
        raise ValueError("invalid Firefox transport marker prefix or size")
    try:
        record = json.loads(line[len(PREFIX) :])
    except (ValueError, RecursionError) as error:
        raise ValueError("invalid Firefox transport marker JSON") from error
    if not isinstance(record, dict) or set(record) != FIELDS:
        raise ValueError("unexpected Firefox transport fields")
    scalar_fields = FIELDS - {"stage"}
    if (
        any(type(record[field]) is not int for field in scalar_fields)
        or record["version"] != 1
        or not isinstance(record["stage"], str)
        or record["stage"] not in STAGES
    ):
        raise ValueError("unexpected Firefox transport scalar types or stage")
    return record


class FirefoxTransportRecords:
    def __init__(self, *, max_records=1024, max_errors=16):
        self.cursor = 0
        self.records = []
        self.errors = []
        self.max_records = max_records
        self.max_errors = max_errors

    def consume(self, transcript):
        new = []
        while (end := transcript.find(b"\n", self.cursor)) != -1:
            start = self.cursor
            self.cursor = end + 1
            if SERIAL_PREFIX.match(transcript, start) is None:
                continue
            try:
                if (
                    end - start > MAX_LINE_BYTES
                    or len(self.records) >= self.max_records
                ):
                    raise ValueError("Firefox transport marker budget exceeded")
                record = parse_record(
                    transcript[start:end].rstrip(b"\r").decode("utf-8")
                )
            except (UnicodeDecodeError, ValueError) as error:
                if len(self.errors) < self.max_errors:
                    self.errors.append(str(error))
                continue
            self.records.append(record)
            new.append(record)
        return new
