# SPDX-License-Identifier: MPL-2.0

"""Strict parsing for bounded GMAC data-path receipts.

The kernel emits one ``ASTERINAS_GMAC_DATAPATH`` line after a poll budget is
serviced.  Keeping the parser separate from the physical runner lets QEMU and
serial evidence use the same packet/error contract without opening a board.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


class GmacEvidenceError(ValueError):
    """The transcript cannot prove the requested GMAC data-path step."""


@dataclass(frozen=True)
class GmacDatapath:
    rx: int
    tx_submitted: int
    tx_reclaimed: int
    tx_outstanding: int
    tx_errors: int
    rx_errors: int
    dma_status: int


_LINE = re.compile(
    r"^ASTERINAS_GMAC_DATAPATH"
    r" rx=(?P<rx>[0-9]+)"
    r" rx_budget=[0-9]+"
    r" rx_reschedules=[0-9]+"
    r" plic_rearms=[0-9]+"
    r" rx_buffer_unavailable=[0-9]+"
    r" tx_submitted=(?P<tx_submitted>[0-9]+)"
    r" tx_reclaimed=(?P<tx_reclaimed>[0-9]+)"
    r" tx_outstanding=(?P<tx_outstanding>[0-9]+)"
    r" tx_errors=(?P<tx_errors>[0-9]+)"
    r" rx_errors=(?P<rx_errors>[0-9]+)"
    r" rx_head=0x[0-9a-fA-F]+"
    r" rx_tail=0x[0-9a-fA-F]+"
    r" dma_status=0x(?P<dma_status>[0-9a-fA-F]+)$",
    re.MULTILINE,
)


def parse_datapath(text: str) -> tuple[GmacDatapath, ...]:
    """Parse all datapath samples, rejecting malformed numeric fields."""

    if not isinstance(text, str):
        raise GmacEvidenceError("GMAC transcript must be text")
    samples = tuple(
        GmacDatapath(
            rx=int(match["rx"]),
            tx_submitted=int(match["tx_submitted"]),
            tx_reclaimed=int(match["tx_reclaimed"]),
            tx_outstanding=int(match["tx_outstanding"]),
            tx_errors=int(match["tx_errors"]),
            rx_errors=int(match["rx_errors"]),
            dma_status=int(match["dma_status"], 16),
        )
        for match in _LINE.finditer(text.replace("\r\n", "\n"))
    )
    if not samples:
        raise GmacEvidenceError("missing GMAC datapath sample")
    return samples


def validate_datapath(
    text: str,
    *,
    minimum_rx: int = 0,
    minimum_tx: int = 0,
    maximum_errors: int = 0,
) -> GmacDatapath:
    """Return the latest sample when packet and error bounds are satisfied."""

    if any(value < 0 for value in (minimum_rx, minimum_tx, maximum_errors)):
        raise GmacEvidenceError("datapath bounds must be non-negative")
    sample = parse_datapath(text)[-1]
    if sample.rx < minimum_rx or sample.tx_submitted < minimum_tx:
        raise GmacEvidenceError("GMAC packet count is below the requested bound")
    if sample.tx_errors > maximum_errors or sample.rx_errors > maximum_errors:
        raise GmacEvidenceError("GMAC error count exceeds the requested bound")
    if sample.dma_status & (1 << 12):
        raise GmacEvidenceError("GMAC reports a fatal DMA bus status")
    return sample
