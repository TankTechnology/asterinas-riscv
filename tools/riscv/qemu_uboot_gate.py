"""Private capability shared by guarded large-memory QEMU launchers."""

from __future__ import annotations

from dataclasses import dataclass


_SLOW_RUN_PERMIT_SEAL = object()


@dataclass(frozen=True)
class SlowRunPermit:
    """Capability issued only after the slow-profile resource gate passes."""

    _seal: object


def _issue_slow_run_permit() -> SlowRunPermit:
    """Create the private capability after the caller validates host resources."""

    return SlowRunPermit(_SLOW_RUN_PERMIT_SEAL)


def has_valid_slow_run_permit(permit: object | None) -> bool:
    """Return whether ``permit`` was issued by this module."""

    return (
        isinstance(permit, SlowRunPermit)
        and permit._seal is _SLOW_RUN_PERMIT_SEAL
    )
