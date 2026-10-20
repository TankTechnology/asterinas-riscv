#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed host state for Megrez physical recovery attempts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from typing import Any


MAX_STATE_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
STATE_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 2
_BOOT_ID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_NONCE = re.compile(r"\A[0-9a-f]{32}\Z")


class SafetyLatchError(RuntimeError):
    """A physical session cannot safely proceed from the retained state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_regular(path: Path, maximum: int, label: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SafetyLatchError(f"{label} is unavailable: {error}") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise SafetyLatchError(f"{label} is not a bounded regular file")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(fd, maximum + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size:
            raise SafetyLatchError(f"{label} changed while being read")
        return bytes(payload), metadata
    finally:
        os.close(fd)


def _load_json(
    path: Path, maximum: int, label: str
) -> tuple[dict[str, Any], os.stat_result]:
    try:
        payload, metadata = _read_regular(path, maximum, label)
    except FileNotFoundError:
        raise SafetyLatchError(f"{label} is missing") from None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SafetyLatchError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise SafetyLatchError(f"{label} must contain a JSON object")
    return value, metadata


def _validate_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SafetyLatchError("safety state is not an object")
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise SafetyLatchError("safety state schema is invalid")
    if value.get("status") not in {"recovery-required", "cleared"}:
        raise SafetyLatchError("safety state status is invalid")
    if value.get("phase") not in {"preflight", "loading", "guest", "recovery"}:
        raise SafetyLatchError("safety state phase is invalid")
    if not isinstance(value.get("reason"), str) or not value["reason"]:
        raise SafetyLatchError("safety state reason is invalid")
    if (
        not isinstance(value.get("run_id"), str)
        or _NONCE.fullmatch(value["run_id"]) is None
    ):
        raise SafetyLatchError("safety state run_id is invalid")
    return value


def read_state(path: Path) -> dict[str, Any] | None:
    """Read and validate a latch, returning ``None`` when it does not exist."""

    try:
        value, _ = _load_json(path, MAX_STATE_BYTES, "safety state")
    except SafetyLatchError as error:
        if str(error) == "safety state is missing":
            return None
        raise
    return _validate_state(value)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_STATE_BYTES:
        raise SafetyLatchError("safety state is too large")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def mark_recovery_required(
    path: Path,
    reason: str,
    *,
    phase: str = "recovery",
    run_id: str | None = None,
) -> None:
    """Atomically retain the fact that a physical reset is now required."""

    if not isinstance(reason, str) or not reason:
        raise ValueError("recovery latch reason must be non-empty")
    if phase not in {"preflight", "loading", "guest", "recovery"}:
        raise ValueError("recovery latch phase is invalid")
    selected_run_id = secrets.token_hex(16) if run_id is None else run_id
    if _NONCE.fullmatch(selected_run_id) is None:
        raise ValueError("recovery latch run_id must be 32 lowercase hex digits")
    _atomic_write(
        path,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": "recovery-required",
            "phase": phase,
            "reason": reason,
            "run_id": selected_run_id,
            "created_at": _now(),
        },
    )


def _validate_receipt(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SafetyLatchError("RockOS recovery receipt is not an object")
    if value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise SafetyLatchError("RockOS recovery receipt schema is invalid")
    if value.get("observer") != "rockos-sha256sum":
        raise SafetyLatchError("RockOS recovery receipt observer is invalid")
    boot_id = value.get("rockos_boot_id")
    nonce = value.get("measurement_nonce")
    if not isinstance(boot_id, str) or _BOOT_ID.fullmatch(boot_id) is None:
        raise SafetyLatchError("RockOS recovery receipt boot_id is invalid")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise SafetyLatchError("RockOS recovery receipt nonce is invalid")
    if value.get("rockos_recovered") is not True:
        raise SafetyLatchError("RockOS recovery receipt is not recovered")
    return value


def assert_start_allowed(
    state_path: Path, receipt_path: Path | None = None
) -> dict[str, Any] | None:
    """Permit a session, or clear a latch using a newer valid RockOS receipt."""

    state = read_state(state_path)
    if state is None or state["status"] == "cleared":
        return None
    if receipt_path is None:
        raise SafetyLatchError("physical reset receipt is required before starting")
    receipt, receipt_metadata = _load_json(
        receipt_path, MAX_RECEIPT_BYTES, "RockOS recovery receipt"
    )
    state_metadata = state_path.stat()
    if receipt_metadata.st_mtime_ns <= state_metadata.st_mtime_ns:
        raise SafetyLatchError("RockOS recovery receipt must be newer than the latch")
    validated = _validate_receipt(receipt)
    _atomic_write(
        state_path,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": "cleared",
            "phase": "preflight",
            "reason": "physical-reset-attested",
            "run_id": state["run_id"],
            "cleared_at": _now(),
            "receipt_sha256": hashlib.sha256(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "rockos_boot_id": validated["rockos_boot_id"],
            "measurement_nonce": validated["measurement_nonce"],
        },
    )
    return validated
