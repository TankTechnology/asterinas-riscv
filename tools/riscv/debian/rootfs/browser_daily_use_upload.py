#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Build and upload one closed Firefox daily-use evidence bundle."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

if Path("/run/asterinas-tools/browser_daily_use_contract.py").is_file():
    sys.path.insert(0, "/run/asterinas-tools")
    from browser_daily_use_contract import (  # type: ignore[import-not-found]
        MAX_ARTIFACT_BYTES,
        DailyUseContractError,
        validate_daily_use_result,
    )
else:
    from tools.riscv.debian.rootfs.browser_daily_use_contract import (
        MAX_ARTIFACT_BYTES,
        DailyUseContractError,
        validate_daily_use_result,
    )


SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = MAX_ARTIFACT_BYTES
COMPONENT_NAMES = (
    "browser-fixture-capture.json",
    "browser-local-capture.json",
    "browser-context-switch.json",
    "browser-composite-capture.json",
    "browser-system-time.json",
    "browser-thread-time.json",
)
RESULT_NAME = "browser-daily-use-result.json"
CHECKPOINT_NAME = "browser-daily-use-checkpoint.json"
SUCCESS_ARTIFACT_NAMES = (*COMPONENT_NAMES, RESULT_NAME)
FAILURE_ARTIFACT_NAMES = (CHECKPOINT_NAME,)
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOP_LEVEL_FIELDS = frozenset(
    {"schemaVersion", "experimentId", "gateRunId", "outcome", "artifacts"}
)
_ARTIFACT_FIELDS = frozenset({"name", "bytes", "sha256", "base64"})
_CHECKPOINT_FIELDS = frozenset(
    {"schemaVersion", "runId", "completedPhases", "functionGroups", "failure"}
)
_FAILURE_FIELDS = frozenset({"type", "reason"})
_FUNCTION_GROUP_FIELDS = frozenset({"name", "state", "reason"})
_PHASES = (
    "session",
    "samplers-ready",
    "fixture",
    "local-timing",
    "context-switch",
    "composite",
    "samplers-stopped",
    "cleanup",
    "identities",
    "coverage",
    "artifacts",
    "validated",
)
_FUNCTION_GROUPS = (
    "document",
    "storage",
    "execution",
    "rendering-media",
    "navigation",
    "download",
    "contexts",
)


class EvidenceBundleError(ValueError):
    """A daily-use upload bundle or source artifact violates its contract."""


@dataclass(frozen=True)
class EvidenceBundle:
    """Validated bytes from one experiment-bound daily-use upload."""

    experiment_id: str
    gate_run_id: str
    outcome: str
    artifacts: dict[str, bytes]


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _validate_run_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise EvidenceBundleError(f"{label} is invalid")
    return value


def _open_evidence_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as error:
        raise EvidenceBundleError("cannot open evidence directory") from error


def _read_private_regular(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise EvidenceBundleError(f"cannot open evidence artifact {name}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceBundleError(f"evidence artifact {name} is not regular")
        if before.st_mode & 0o077:
            raise EvidenceBundleError(f"evidence artifact {name} is not private")
        if not 0 < before.st_size <= MAX_FILE_BYTES:
            raise EvidenceBundleError(f"evidence artifact {name} size is invalid")
        payload = bytearray()
        while len(payload) <= MAX_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_FILE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(payload) != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in stable
        ):
            raise EvidenceBundleError(f"evidence artifact {name} changed")
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise EvidenceBundleError(f"evidence artifact {name} was replaced")
        return bytes(payload)
    except OSError as error:
        raise EvidenceBundleError(f"cannot read evidence artifact {name}") from error
    finally:
        os.close(descriptor)


def _load_json(payload: bytes, label: str) -> object:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceBundleError(f"{label} is not valid JSON") from error


def _success_gate_run_id(artifacts: dict[str, bytes]) -> str:
    try:
        result = validate_daily_use_result(
            _load_json(artifacts[RESULT_NAME], "daily-use result")
        )
    except DailyUseContractError as error:
        raise EvidenceBundleError("daily-use result contract is invalid") from error
    if result["state"] != "pass":
        raise EvidenceBundleError("daily-use result is not a pass")
    manifest = result["artifacts"]
    expected = [
        {
            "name": name,
            "bytes": len(artifacts[name]),
            "sha256": hashlib.sha256(artifacts[name]).hexdigest(),
        }
        for name in COMPONENT_NAMES
    ]
    if manifest != expected:
        raise EvidenceBundleError("daily-use result artifact manifest disagrees")
    return _validate_run_id(result["runId"], "gate run identity")


def _failure_gate_run_id(artifacts: dict[str, bytes]) -> str:
    checkpoint = _load_json(artifacts[CHECKPOINT_NAME], "daily-use checkpoint")
    if type(checkpoint) is not dict or set(checkpoint) != _CHECKPOINT_FIELDS:
        raise EvidenceBundleError("daily-use checkpoint schema is invalid")
    completed = checkpoint["completedPhases"]
    groups = checkpoint["functionGroups"]
    if (
        type(checkpoint["schemaVersion"]) is not int
        or checkpoint["schemaVersion"] != 1
        or type(completed) is not list
        or tuple(completed) != _PHASES[: len(completed)]
        or type(groups) is not list
    ):
        raise EvidenceBundleError("daily-use checkpoint content is invalid")
    group_positions: list[int] = []
    for group in groups:
        if type(group) is not dict or set(group) != _FUNCTION_GROUP_FIELDS:
            raise EvidenceBundleError("daily-use checkpoint function group is invalid")
        name = group["name"]
        reason = group["reason"]
        if (
            name not in _FUNCTION_GROUPS
            or group["state"] not in ("pass", "fail")
            or (
                reason is not None
                and (
                    not isinstance(reason, str)
                    or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", reason) is None
                )
            )
        ):
            raise EvidenceBundleError("daily-use checkpoint function group is invalid")
        group_positions.append(_FUNCTION_GROUPS.index(name))
    if group_positions != sorted(set(group_positions)):
        raise EvidenceBundleError("daily-use checkpoint function groups are unordered")
    failure = checkpoint["failure"]
    if (
        type(failure) is not dict
        or set(failure) != _FAILURE_FIELDS
        or failure["type"] != "daily-use-gate"
        or not isinstance(failure["reason"], str)
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", failure["reason"]) is None
    ):
        raise EvidenceBundleError("daily-use checkpoint failure is invalid")
    return _validate_run_id(checkpoint["runId"], "gate run identity")


def build_bundle(evidence_dir: Path, experiment_id: str, outcome: str) -> bytes:
    """Build canonical JSON from one closed success artifact set."""

    _validate_run_id(experiment_id, "experiment identity")
    if outcome not in ("pass", "fail"):
        raise EvidenceBundleError("daily-use outcome is invalid")
    if not isinstance(evidence_dir, Path):
        raise EvidenceBundleError("evidence directory is invalid")
    names = SUCCESS_ARTIFACT_NAMES if outcome == "pass" else FAILURE_ARTIFACT_NAMES
    directory_fd = _open_evidence_directory(evidence_dir)
    try:
        artifacts = {
            name: _read_private_regular(directory_fd, name) for name in names
        }
    finally:
        os.close(directory_fd)
    gate_run_id = (
        _success_gate_run_id(artifacts)
        if outcome == "pass"
        else _failure_gate_run_id(artifacts)
    )
    rows = [
        {
            "name": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "base64": base64.b64encode(payload).decode("ascii"),
        }
        for name, payload in artifacts.items()
    ]
    raw = _canonical_json(
        {
            "schemaVersion": SCHEMA_VERSION,
            "experimentId": experiment_id,
            "gateRunId": gate_run_id,
            "outcome": outcome,
            "artifacts": rows,
        }
    )
    if len(raw) > MAX_BUNDLE_BYTES:
        raise EvidenceBundleError("daily-use evidence bundle is oversized")
    return raw


def parse_bundle(raw: bytes, expected_experiment_id: str) -> EvidenceBundle:
    """Validate and decode one canonical success evidence bundle."""

    _validate_run_id(expected_experiment_id, "expected experiment identity")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BUNDLE_BYTES:
        raise EvidenceBundleError("daily-use evidence bundle size is invalid")
    value = _load_json(raw, "daily-use evidence bundle")
    if type(value) is not dict or set(value) != _TOP_LEVEL_FIELDS:
        raise EvidenceBundleError("daily-use evidence bundle schema is invalid")
    if _canonical_json(value) != raw:
        raise EvidenceBundleError("daily-use evidence bundle is not canonical")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise EvidenceBundleError("daily-use evidence schema version is invalid")
    experiment_id = _validate_run_id(value["experimentId"], "experiment identity")
    if experiment_id != expected_experiment_id:
        raise EvidenceBundleError("daily-use experiment identity disagrees")
    gate_run_id = _validate_run_id(value["gateRunId"], "gate run identity")
    outcome = value["outcome"]
    if outcome not in ("pass", "fail"):
        raise EvidenceBundleError("daily-use outcome is invalid")
    expected_names = (
        SUCCESS_ARTIFACT_NAMES if outcome == "pass" else FAILURE_ARTIFACT_NAMES
    )
    rows = value["artifacts"]
    if type(rows) is not list or len(rows) != len(expected_names):
        raise EvidenceBundleError("daily-use artifact list is invalid")
    artifacts: dict[str, bytes] = {}
    for expected_name, row in zip(expected_names, rows):
        if type(row) is not dict or set(row) != _ARTIFACT_FIELDS:
            raise EvidenceBundleError("daily-use artifact schema is invalid")
        if row["name"] != expected_name:
            raise EvidenceBundleError("daily-use artifact order is invalid")
        encoded = row["base64"]
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as error:
            raise EvidenceBundleError("daily-use artifact base64 is invalid") from error
        if type(row["bytes"]) is not int or row["bytes"] != len(payload):
            raise EvidenceBundleError("daily-use artifact size disagrees")
        digest = row["sha256"]
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or digest != hashlib.sha256(payload).hexdigest()
        ):
            raise EvidenceBundleError("daily-use artifact digest disagrees")
        artifacts[expected_name] = payload
    result_run_id = (
        _success_gate_run_id(artifacts)
        if outcome == "pass"
        else _failure_gate_run_id(artifacts)
    )
    if result_run_id != gate_run_id:
        raise EvidenceBundleError("daily-use gate run identity disagrees")
    return EvidenceBundle(experiment_id, gate_run_id, outcome, artifacts)


def upload_bundle(raw: bytes, url: str, timeout_seconds: float) -> None:
    """Post one fixed-length JSON body and require an empty HTTP 204 response."""

    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BUNDLE_BYTES:
        raise EvidenceBundleError("daily-use evidence bundle size is invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 120
    ):
        raise EvidenceBundleError("daily-use upload timeout is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise EvidenceBundleError("daily-use upload URL is invalid") from error
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise EvidenceBundleError("daily-use upload URL is invalid")
    connection = http.client.HTTPConnection(
        parsed.hostname, port, timeout=float(timeout_seconds)
    )
    try:
        connection.request(
            "POST",
            parsed.path,
            body=raw,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_BUNDLE_BYTES + 1)
        if response.status != 204 or body:
            raise EvidenceBundleError("daily-use upload response is invalid")
    except (OSError, http.client.HTTPException) as error:
        raise EvidenceBundleError("daily-use upload failed") from error
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    """Build and upload one experiment-bound daily-use evidence bundle."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("experiment_id")
    parser.add_argument("outcome", choices=("pass", "fail"))
    parser.add_argument("url")
    parser.add_argument("--timeout", type=float, required=True)
    options = parser.parse_args(argv)
    try:
        raw = build_bundle(
            options.evidence_dir, options.experiment_id, options.outcome
        )
        upload_bundle(raw, options.url, options.timeout)
    except EvidenceBundleError:
        print("ASTERINAS_BROWSER_DAILY_USE_UPLOAD_FAIL", file=sys.stderr)
        return 1
    print(
        "ASTERINAS_BROWSER_DAILY_USE_UPLOAD_PASS "
        f"experiment_id={options.experiment_id} outcome={options.outcome}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
