# SPDX-License-Identifier: MPL-2.0
"""Strict Firefox dmesg framing, parsing, and bounded correlation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import zlib


DMESG_META_PREFIX = "A_FF_DMESG_META "
DMESG_FRAME_PREFIX = "A_FF_DMESG "
MAX_DMESG_BYTES = 4 * 1024 * 1024
MAX_COMPRESSED_TEXT = 6 * 1024 * 1024
MAX_PARTS = 16384
TARGETED_RETURN_CODES = frozenset((0, -9, -15))
META_FIELDS = frozenset(
    (
        "version",
        "raw_bytes",
        "retained_bytes",
        "discarded_bytes",
        "discarded_lines",
        "early_exit",
        "returncode",
    )
)
FRAME = re.compile(
    r"A_FF_DMESG part=(0|[1-9][0-9]*)/([1-9][0-9]*) "
    r"sha256=([0-9a-f]{64}) data=([A-Za-z0-9+/=]+)"
)
RAW_RECORD = re.compile(r"<([0-9]{1,3})>\[\s*([0-9]+(?:\.[0-9]+)?)\]\s?(.*)")
INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
MARKER_PHASES = frozenset(
    (
        "collector_started",
        "request_selected",
        "request_enter",
        "snapshot_before_start",
        "snapshot_before_end",
        "snapshot_during_start",
        "snapshot_during_end",
        "request_return",
        "request_error",
        "snapshot_after_start",
        "snapshot_after_end",
        "collector_stopping",
    )
)
LIFECYCLE_KINDS = frozenset(
    (
        "clone",
        "exec",
        "wait",
        "normal_exit",
        "signal_exit",
        "cap_exhausted",
    )
)


def _canonical_int(value, *, minimum=0):
    return type(value) is int and value >= minimum


class DmesgFrames:
    """Receive exactly one bounded metadata/payload sequence."""

    def __init__(self):
        self.metadata = None
        self.payload = None
        self._parts = []
        self._total = None
        self._digest = None
        self._encoded_bytes = 0

    def accept(self, line: str) -> bytes | None:
        if line.startswith(DMESG_META_PREFIX):
            self._accept_metadata(line)
            return None
        if not line.startswith(DMESG_FRAME_PREFIX):
            return None
        if self.metadata is None:
            raise ValueError("dmesg payload preceded metadata")
        if self.payload is not None:
            raise ValueError("duplicate dmesg payload")
        if len(line.encode("utf-8")) > 8192:
            raise ValueError("oversized dmesg frame")
        match = FRAME.fullmatch(line)
        if match is None:
            raise ValueError("malformed dmesg frame")
        index, total = (int(match.group(1)), int(match.group(2)))
        digest, data = match.group(3), match.group(4)
        if not 0 <= index < total <= MAX_PARTS:
            raise ValueError("invalid dmesg frame index")
        if index == 0:
            if self._parts:
                raise ValueError("duplicate dmesg sequence start")
            self._total, self._digest = total, digest
        if self._total != total or self._digest != digest or index != len(self._parts):
            raise ValueError("noncontiguous dmesg frame sequence")
        self._encoded_bytes += len(data)
        if self._encoded_bytes > MAX_COMPRESSED_TEXT:
            raise ValueError("compressed dmesg payload exceeds budget")
        self._parts.append(data)
        if len(self._parts) != total:
            return None
        self.payload = self._decode_payload()
        return self.payload

    def _accept_metadata(self, line):
        if self.metadata is not None:
            raise ValueError("duplicate dmesg metadata")
        if len(line.encode("utf-8")) > 1024:
            raise ValueError("oversized dmesg metadata")
        try:
            value = json.loads(line[len(DMESG_META_PREFIX) :])
        except (ValueError, RecursionError) as error:
            raise ValueError("invalid dmesg metadata JSON") from error
        if not isinstance(value, dict) or set(value) != META_FIELDS:
            raise ValueError("unexpected dmesg metadata fields")
        if (
            not _canonical_int(value["version"], minimum=1)
            or value["version"] != 1
            or any(
                not _canonical_int(value[key])
                for key in (
                    "raw_bytes",
                    "retained_bytes",
                    "discarded_bytes",
                    "discarded_lines",
                )
            )
            or type(value["early_exit"]) is not bool
            or type(value["returncode"]) is not int
            or value["retained_bytes"] > MAX_DMESG_BYTES
            or value["raw_bytes"] != value["retained_bytes"] + value["discarded_bytes"]
        ):
            raise ValueError("invalid dmesg metadata scalars")
        self.metadata = value

    def _decode_payload(self):
        encoded = "".join(self._parts)
        try:
            compressed = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("invalid dmesg base64") from error
        if base64.b64encode(compressed).decode("ascii") != encoded:
            raise ValueError("noncanonical dmesg base64")
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(compressed, MAX_DMESG_BYTES + 1)
            if len(raw) > MAX_DMESG_BYTES or decoder.unconsumed_tail:
                raise ValueError("decoded dmesg payload exceeds budget")
            raw += decoder.flush(MAX_DMESG_BYTES - len(raw) + 1)
        except zlib.error as error:
            raise ValueError("invalid dmesg zlib stream") from error
        if (
            len(raw) > MAX_DMESG_BYTES
            or not decoder.eof
            or decoder.unused_data
            or hashlib.sha256(raw).hexdigest() != self._digest
            or len(raw) != self.metadata["retained_bytes"]
        ):
            raise ValueError("dmesg payload integrity mismatch")
        return raw

    def finish(self) -> None:
        if self.metadata is None or self.payload is None:
            raise ValueError("incomplete dmesg frame sequence")
        if self.metadata["discarded_bytes"] or self.metadata["discarded_lines"]:
            raise ValueError("dmesg evidence was discarded")
        if self.metadata["early_exit"]:
            raise ValueError("dmesg exited before targeted shutdown")
        if self.metadata["returncode"] not in TARGETED_RETURN_CODES:
            raise ValueError("unexpected dmesg shutdown status")


def _key_values(message):
    fields = {}
    for token in message.split():
        if token.count("=") != 1:
            raise ValueError("malformed dmesg key/value token")
        key, value = token.split("=", 1)
        if not key or key in fields or not value:
            raise ValueError("duplicate or empty dmesg field")
        fields[key] = int(value) if INTEGER.fullmatch(value) else value
    return fields


def _marker(priority, timestamp, message, sequence):
    fields = _key_values(message.removeprefix("ASTERINAS_FF_KLOG "))
    expected = {"version", "phase", "request_id", "firefox_pid", "client_pid"}
    if set(fields) != expected:
        raise ValueError("unexpected Firefox klog marker fields")
    if (
        fields["version"] != 1
        or fields["phase"] not in MARKER_PHASES
        or not _canonical_int(fields["request_id"])
        or (
            fields["request_id"] == 0
            and fields["phase"] not in ("collector_started", "collector_stopping")
        )
        or not _canonical_int(fields["firefox_pid"], minimum=1)
        or not _canonical_int(fields["client_pid"], minimum=1)
    ):
        raise ValueError("invalid Firefox klog marker scalars")
    return {
        "kind": "marker",
        "sequence": sequence,
        "priority": priority,
        "timestamp_seconds": timestamp,
        **fields,
    }


def _lifecycle(priority, timestamp, message, sequence):
    fields = _key_values(message.removeprefix("syscall_diag "))
    if fields.get("lifecycle") not in LIFECYCLE_KINDS:
        raise ValueError("invalid lifecycle kind")
    return {
        "kind": "lifecycle",
        "sequence": sequence,
        "priority": priority,
        "timestamp_seconds": timestamp,
        "fields": fields,
    }


def parse_dmesg(raw: bytes) -> list[dict]:
    """Parse color-free util-linux ``dmesg --raw`` output without clock mixing."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("dmesg is not UTF-8") from error
    records = []
    for line in text.splitlines():
        if not line:
            continue
        match = RAW_RECORD.fullmatch(line)
        if match is None:
            raise ValueError("malformed raw dmesg record")
        priority = int(match.group(1))
        timestamp = float(match.group(2))
        if not 0 <= priority <= 191 or not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("invalid raw dmesg metadata")
        message = match.group(3)
        sequence = len(records)
        if message.startswith("ASTERINAS_FF_KLOG "):
            record = _marker(priority, timestamp, message, sequence)
        elif message.startswith("syscall_diag "):
            record = _lifecycle(priority, timestamp, message, sequence)
        else:
            record = {
                "kind": "message",
                "sequence": sequence,
                "priority": priority,
                "timestamp_seconds": timestamp,
                "message": message,
            }
        records.append(record)
    return records


def _snapshot_identity(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValueError("invalid snapshot")
    phase = snapshot.get("phase")
    tree = snapshot.get("tree")
    if phase not in ("before", "during", "after") or not isinstance(tree, dict):
        raise ValueError("invalid snapshot phase or tree")
    identity = tree.get("root_identity")
    if not isinstance(identity, dict) or not _canonical_int(
        tree.get("root_pid"), minimum=1
    ):
        raise ValueError("snapshot root identity is unavailable")
    if any("identity" in value for value in tree.get("limitations", ())):
        raise ValueError("snapshot process identity is incomplete")
    return phase, identity


def _current_syscalls(snapshots):
    observations = []
    for snapshot in snapshots:
        for process in snapshot["tree"].get("processes", ()):
            for thread in process.get("threads", ()):
                syscall = thread.get("syscall", {})
                value = syscall.get("value") if syscall.get("status") == "ok" else None
                current = value.get("current") if isinstance(value, dict) else None
                if isinstance(current, dict) and type(current.get("number")) is int:
                    observations.append(
                        {
                            "phase": snapshot["phase"],
                            "pid": process.get("pid"),
                            "tid": thread.get("tid"),
                            "number": current["number"],
                        }
                    )
    return observations


def correlate(
    dmesg: bytes,
    actor_records: list[dict],
    snapshots: list[dict],
    transport_records: list[dict],
) -> dict:
    """Identify the first missing actor boundary without guessing a wakeup bug."""
    dmesg_records = parse_dmesg(dmesg)
    phases = {}
    identities = []
    for snapshot in snapshots:
        phase, identity = _snapshot_identity(snapshot)
        if phase in phases:
            raise ValueError("duplicate snapshot phase")
        phases[phase] = snapshot
        identities.append(identity)
    if set(phases) != {"before", "during", "after"}:
        raise ValueError("three snapshot phases are required")
    if identities[1:] != identities[:-1]:
        raise ValueError("Firefox identity changed across snapshots")

    relevant_transport = [
        record
        for record in transport_records
        if isinstance(record, dict)
        and record.get("command") == "WebDriver:ExecuteScript"
    ]
    request_ids = {
        record.get("request_id")
        for record in relevant_transport
        if _canonical_int(record.get("request_id"), minimum=1)
    }
    request_ids.update(
        record.get("request")
        for record in actor_records
        if isinstance(record, dict) and _canonical_int(record.get("request"), minimum=1)
    )
    request_ids.update(
        record["request_id"]
        for record in dmesg_records
        if record["kind"] == "marker" and record["request_id"] > 0
    )
    if len(request_ids) != 1:
        raise ValueError("selected request identity is absent or ambiguous")
    request_id = next(iter(request_ids))
    selected_actors = []
    for record in actor_records:
        if not isinstance(record, dict) or type(record.get("request")) is not int:
            raise ValueError("invalid actor record")
        if record["request"] not in (0, request_id):
            raise ValueError("actor request identity changed")
        if record["request"] == request_id:
            selected_actors.append(record)
    stages = {record.get("stage") for record in selected_actors}
    send_complete = any(
        record.get("event") == "send_complete"
        and record.get("request_id") == request_id
        and record.get("send_complete") is True
        for record in relevant_transport
    )
    transport_complete = any(
        record.get("event") == "complete" and record.get("request_id") == request_id
        for record in relevant_transport
    )
    marker_phases = {
        record["phase"]
        for record in dmesg_records
        if record["kind"] == "marker" and record["request_id"] == request_id
    }

    boundaries = (
        ("marionette_send_complete", send_complete, None),
        ("actor_driver_entry", "driver.enter" in stages, None),
        (
            "parent_query_sent",
            "parent.query_sent" in stages,
            "firefox_parent_actor_dispatch",
        ),
        ("child_receive", "child.receive" in stages, "firefox_parent_content_ipc"),
        (
            "child_script_complete",
            "child.script_complete" in stages,
            "firefox_content_execution",
        ),
        (
            "parent_query_complete",
            "parent.query_complete" in stages,
            "firefox_reply_delivery",
        ),
        (
            "server_response_queued",
            "server.response_queued" in stages,
            "firefox_server_response_queue",
        ),
        (
            "marionette_response",
            transport_complete or "request_return" in marker_phases,
            None,
        ),
    )
    first_missing = None
    selected_subsystem = None
    seen_missing = False
    for name, present, subsystem in boundaries:
        if seen_missing and present:
            raise ValueError("contradictory actor boundary order")
        if not present and first_missing is None:
            first_missing, selected_subsystem, seen_missing = name, subsystem, True

    timeline = []
    for record in dmesg_records:
        if record["kind"] in ("marker", "lifecycle"):
            timeline.append(
                {
                    "source": "dmesg",
                    "clock": "kernel_dmesg_seconds",
                    "timestamp": record["timestamp_seconds"],
                    "record": record,
                }
            )
    for record in selected_actors:
        if type(record.get("host_received_monotonic_ns")) is int:
            timeline.append(
                {
                    "source": "actor",
                    "clock": "host_monotonic_ns",
                    "timestamp": record["host_received_monotonic_ns"],
                    "record": record,
                }
            )
    for record in relevant_transport:
        clock = (
            "host_monotonic_ns"
            if type(record.get("host_received_monotonic_ns")) is int
            else "guest_monotonic_ns"
        )
        timestamp = record.get("host_received_monotonic_ns", record.get("monotonic_ns"))
        if type(timestamp) is int:
            timeline.append(
                {
                    "source": "transport",
                    "clock": clock,
                    "timestamp": timestamp,
                    "record": record,
                }
            )
    for snapshot in snapshots:
        if type(snapshot.get("host_received_monotonic_ns")) is int:
            timeline.append(
                {
                    "source": "snapshot",
                    "clock": "host_monotonic_ns",
                    "timestamp": snapshot["host_received_monotonic_ns"],
                    "record": {"phase": snapshot["phase"]},
                }
            )

    outcome = "failure_not_reproduced" if first_missing is None else "boundary_missing"
    lifecycle = [record for record in dmesg_records if record["kind"] == "lifecycle"]
    return {
        "version": 1,
        "physical": False,
        "browser_acceptance": False,
        "evidence_complete": True,
        "request_id": request_id,
        "outcome": outcome,
        "first_missing_boundary": first_missing,
        "selected_subsystem": selected_subsystem,
        "observations": {
            "boundaries": {name: present for name, present, _ in boundaries},
            "actor_stages": sorted(stage for stage in stages if isinstance(stage, str)),
            "marker_phases": sorted(marker_phases),
            "lifecycle_records": len(lifecycle),
            "current_syscalls": _current_syscalls(snapshots),
        },
        "timeline": timeline,
        "claim_limits": [
            "QEMU diagnostic evidence; not physical or browser acceptance",
            "clock domains are preserved and are not numerically compared",
            "an isolated syscall wait does not establish a missed wakeup",
            "a selected Firefox boundary does not by itself prove a kernel defect",
        ],
    }
