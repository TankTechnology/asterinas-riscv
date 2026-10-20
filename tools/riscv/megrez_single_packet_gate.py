# SPDX-License-Identifier: MPL-2.0

"""Fail-closed classifier for one business UDP packet in each direction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping

from tools.riscv.megrez_single_packet_fixture import PacketSummary, nonce_payload


class SinglePacketGateError(ValueError):
    """The guest and fixture did not prove exactly one packet exchange."""


_MARKER = re.compile(
    r"^ASTERINAS_GMAC_SINGLE_PACKET"
    r" nonce=(?P<nonce>[0-9a-f]{32})"
    r" tx=(?P<tx>[0-9]+) rx=(?P<rx>[0-9]+)"
    r" tx_errors=(?P<tx_errors>[0-9]+) rx_errors=(?P<rx_errors>[0-9]+)"
    r" payload_sha256=(?P<sha>[0-9a-f]{64}) status=(?P<status>pass|fail)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class GuestPacketReceipt:
    nonce: str
    tx: int
    rx: int
    tx_errors: int
    rx_errors: int
    payload_sha256: str
    status: str


def parse_guest_receipt(transcript: str, nonce: str) -> GuestPacketReceipt:
    if not isinstance(transcript, str):
        raise SinglePacketGateError("guest transcript must be text")
    normalized = transcript.replace("\r\n", "\n").replace("\r", "\n")
    matches = tuple(_MARKER.finditer(normalized))
    if len(matches) != 1:
        raise SinglePacketGateError("guest transcript must contain one packet marker")
    match = matches[0]
    values = match.groupdict()
    if values["nonce"] != nonce or values["status"] != "pass":
        raise SinglePacketGateError("guest packet marker is stale or failed")
    return GuestPacketReceipt(
        nonce=values["nonce"],
        tx=int(values["tx"]),
        rx=int(values["rx"]),
        tx_errors=int(values["tx_errors"]),
        rx_errors=int(values["rx_errors"]),
        payload_sha256=values["sha"],
        status=values["status"],
    )


def validate_packet_exchange(
    guest_transcript: str,
    fixture: Mapping[str, Any] | PacketSummary,
    nonce: str,
) -> dict[str, Any]:
    """Validate one nonce-bound application packet and zero datapath errors."""

    receipt = parse_guest_receipt(guest_transcript, nonce)
    expected = nonce_payload(nonce)
    expected_sha = hashlib.sha256(expected).hexdigest()
    data = fixture.as_dict() if isinstance(fixture, PacketSummary) else dict(fixture)
    if (
        data.get("request_count") != 1
        or data.get("response_count") != 1
        or data.get("payload_size") != len(expected)
        or data.get("payload_sha256") != expected_sha
        or data.get("status") != "pass"
    ):
        raise SinglePacketGateError("fixture did not prove one matching exchange")
    if (
        receipt.tx != 1
        or receipt.rx != 1
        or receipt.tx_errors != 0
        or receipt.rx_errors != 0
        or receipt.payload_sha256 != expected_sha
    ):
        raise SinglePacketGateError("guest did not prove one error-free exchange")
    return {
        "status": "pass",
        "nonce": nonce,
        "tx": receipt.tx,
        "rx": receipt.rx,
        "fixture_request_count": data["request_count"],
        "fixture_response_count": data["response_count"],
        "payload_size": len(expected),
        "payload_sha256": expected_sha,
    }
