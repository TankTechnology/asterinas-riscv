# SPDX-License-Identifier: MPL-2.0
"""Validate scalar actor markers and scan a growing serial transcript once."""

import json
import re


PREFIX = "A_FF_ACTOR "
# systemd identifies the tail forwarder here, not the Firefox source process.
# Only the record's pid is the Firefox identity.
SERIAL_PREFIX = re.compile(rb"(?:browser-web-firefox\[[0-9]+\]: )?A_FF_ACTOR ")
MAX_LINE_BYTES = 512
STAGES = frozenset(
    (
        "server.loaded",
        "driver.loaded",
        "parent.loaded",
        "child.loaded",
        "transport.loaded",
        "child.actor_created",
        "server.command",
        "server.driver_complete",
        "server.response_queue",
        "server.response_queued",
        "driver.enter",
        "driver.prompt_enter",
        "driver.prompt_complete",
        "driver.actor_enter",
        "parent.actor_select",
        "parent.query_enter",
        "parent.query_sent",
        "parent.query_complete",
        "parent.reply_ready",
        "child.receive",
        "child.script_enter",
        "child.script_complete",
        "child.tick_enter",
        "child.tick_complete",
        "child.reply_ready",
        "child.error",
    )
)


def parse_record(line):
    """Reject malformed markers without retaining arbitrary payload fields."""
    line = re.sub(r"^browser-web-firefox\[[0-9]+\]: ", "", line, count=1)
    if not line.startswith(PREFIX) or len(line.encode()) > MAX_LINE_BYTES:
        raise ValueError("invalid actor marker prefix or size")
    try:
        record = json.loads(line[len(PREFIX) :])
    except (ValueError, RecursionError) as error:
        raise ValueError("invalid actor marker JSON") from error
    if not isinstance(record, dict) or set(record) != {
        "version",
        "stage",
        "pid",
        "request",
        "context",
        "target_pid",
    }:
        raise ValueError("unexpected actor fields")
    if (
        any(
            type(record[k]) is not int
            for k in ("version", "pid", "request", "context", "target_pid")
        )
        or record["version"] != 1
        or not isinstance(record["stage"], str)
        or record["stage"] not in STAGES
    ):
        raise ValueError("unexpected actor scalar types or stage")
    return record


class ActorRecords:
    """Use a separate cursor so protocol checkpoints cannot hide markers.

    Call with the complete growing transcript during protocol reads and once
    after serial drain. Records recovered at final drain have no live timestamp.
    """

    def __init__(self, *, max_records=4096, max_errors=16):
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
                    raise ValueError("actor marker budget exceeded")
                record = parse_record(
                    transcript[start:end].rstrip(b"\r").decode("utf-8")
                )
            except ValueError as error:
                if len(self.errors) < self.max_errors:
                    self.errors.append(str(error))
                continue
            self.records.append(record)
            new.append(record)
        return new
