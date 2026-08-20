#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Parse and publish normalized results from the RISC-V LTP guest runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ltp_suite import suite_names


VERDICT_RE = re.compile(r"^\[(PASS|FAIL|CONF|CRASH|TIMEOUT)\] ([^\s]+)$")
SUMMARY_RE = re.compile(
    r"^\[summary\] total=(\d+) pass=(\d+) fail=(\d+) "
    r"conf=(\d+) crash=(\d+) timeout=(\d+)$"
)
GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class LtpVerdict:
    """One mutually exclusive verdict for one manifest name."""

    name: str
    verdict: str


@dataclass(frozen=True)
class LtpCounts:
    """Normalized counters plus the historical aggregate-failure value."""

    total: int
    pass_count: int
    fail_count: int
    conf_count: int
    crash_count: int
    timeout_count: int
    legacy_fail_total: int


@dataclass(frozen=True)
class ParsedLtpResult:
    """Validated guest-runner output independent of boot infrastructure."""

    counts: LtpCounts
    verdicts: tuple[LtpVerdict, ...]
    ltp_passed: bool


def parse_ltp_serial(
    serial_text: str,
    expected_names: Sequence[str] | None = None,
) -> ParsedLtpResult:
    """Parse one complete legacy LTP serial summary into exclusive counters."""

    lines = serial_text.replace("\r", "").splitlines()
    if lines.count("__LTP_GATE_DONE__") != 1:
        raise ValueError("expected exactly one LTP DONE marker")
    terminal_count = (
        lines.count("__LTP_GATE_PASS__") + lines.count("__LTP_GATE_FAIL__")
    )
    if terminal_count != 1:
        raise ValueError("expected exactly one LTP PASS/FAIL marker")

    summaries = [
        match for line in lines if (match := SUMMARY_RE.fullmatch(line)) is not None
    ]
    if len(summaries) != 1:
        raise ValueError("expected exactly one LTP summary")
    total, passed, aggregate_fail, conf, crash, timeout = (
        int(value) for value in summaries[0].groups()
    )
    plain_fail = aggregate_fail - crash - timeout
    if plain_fail < 0:
        raise ValueError("aggregate fail is smaller than crash + timeout")
    if total != passed + plain_fail + conf + crash + timeout:
        raise ValueError("summary total is inconsistent")

    verdicts = tuple(
        LtpVerdict(name=match.group(2), verdict=match.group(1))
        for line in lines
        if (match := VERDICT_RE.fullmatch(line)) is not None
    )
    observed = Counter(item.verdict for item in verdicts)
    expected = {
        "PASS": passed,
        "FAIL": plain_fail,
        "CONF": conf,
        "CRASH": crash,
        "TIMEOUT": timeout,
    }
    if len(verdicts) != total or any(
        observed[name] != count for name, count in expected.items()
    ):
        raise ValueError("verdict lines do not match summary")
    if len({item.name for item in verdicts}) != len(verdicts):
        raise ValueError("duplicate LTP verdict name")
    if expected_names is not None and tuple(item.name for item in verdicts) != tuple(
        expected_names
    ):
        raise ValueError("verdict sequence does not match selected manifest")

    ltp_passed = aggregate_fail == 0
    if ltp_passed != ("__LTP_GATE_PASS__" in lines):
        raise ValueError("terminal marker disagrees with summary")
    return ParsedLtpResult(
        counts=LtpCounts(
            total=total,
            pass_count=passed,
            fail_count=plain_fail,
            conf_count=conf,
            crash_count=crash,
            timeout_count=timeout,
            legacy_fail_total=aggregate_fail,
        ),
        verdicts=verdicts,
        ltp_passed=ltp_passed,
    )


def selected_manifest_names(manifest_text: str) -> tuple[str, ...]:
    """Return the ordered tags from a validated LTP runtest manifest."""

    names: list[str] = []
    for line_number, line in enumerate(manifest_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            raise ValueError(f"malformed selected manifest line {line_number}")
        names.append(fields[0])
    if len(set(names)) != len(names):
        raise ValueError("duplicate selected manifest tag")
    return tuple(names)


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def build_result_document(
    parsed: ParsedLtpResult,
    *,
    boot_result: Mapping[str, object],
    git_commit: str,
    smp: int,
    suite: str,
) -> dict[str, object]:
    """Join LTP verdicts with the immutable prepared-boot evidence."""

    if GIT_COMMIT_RE.fullmatch(git_commit) is None:
        raise ValueError("git commit must be a 40-character lowercase object id")
    if smp not in (1, 4):
        raise ValueError("SMP must be 1 or 4")
    if suite not in suite_names():
        raise ValueError(f"unknown LTP suite: {suite}")
    infrastructure_passed = boot_result.get("passed")
    if not isinstance(infrastructure_passed, bool):
        raise ValueError("boot result passed field must be boolean")
    profile = boot_result.get("profile")
    if profile != f"generic-sv39-ltp-smp{smp}":
        raise ValueError("boot result profile does not match SMP")

    boot_artifacts = _require_mapping(boot_result.get("artifacts"), "artifacts")
    artifacts = {
        "kernel_sha256": _require_sha256(
            boot_artifacts.get("kernel_sha256"), "kernel_sha256"
        ),
        "dtb_sha256": _require_sha256(
            boot_artifacts.get("dtb_sha256"), "dtb_sha256"
        ),
        "initrd_sha256": _require_sha256(
            boot_artifacts.get("initrd_sha256"), "initrd_sha256"
        ),
        "boot_disk_sha256": _require_sha256(
            boot_result.get("boot_disk_sha256_before"), "boot_disk_sha256"
        ),
    }
    counts = {
        "total": parsed.counts.total,
        "pass": parsed.counts.pass_count,
        "fail": parsed.counts.fail_count,
        "conf": parsed.counts.conf_count,
        "crash": parsed.counts.crash_count,
        "timeout": parsed.counts.timeout_count,
        "legacy_fail_total": parsed.counts.legacy_fail_total,
    }
    return {
        "schema_version": 2,
        "git_commit": git_commit,
        "profile": profile,
        "smp": smp,
        "suite": suite,
        "infrastructure_passed": infrastructure_passed,
        "ltp_passed": parsed.ltp_passed,
        "counts": counts,
        "verdicts": [asdict(item) for item in parsed.verdicts],
        "artifacts": artifacts,
    }


def summary_text(document: Mapping[str, object]) -> str:
    """Render one stable two-line summary from a result document."""

    counts = _require_mapping(document.get("counts"), "counts")
    infrastructure = "PASS" if document.get("infrastructure_passed") is True else "FAIL"
    ltp = "PASS" if document.get("ltp_passed") is True else "FAIL"
    return (
        f"suite={document['suite']} infrastructure={infrastructure} ltp={ltp}\n"
        f"total={counts['total']} pass={counts['pass']} fail={counts['fail']} "
        f"conf={counts['conf']} crash={counts['crash']} "
        f"timeout={counts['timeout']} "
        f"legacy_fail_total={counts['legacy_fail_total']}\n"
    )


def _publish_text(path: Path, payload: str) -> None:
    """Publish a new regular file atomically without replacing prior evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
            os.fchmod(output.fileno(), 0o644)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write = subparsers.add_parser("write")
    write.add_argument("--serial", type=Path, required=True)
    write.add_argument("--boot-result", type=Path, required=True)
    write.add_argument("--manifest", type=Path, required=True)
    write.add_argument("--result", type=Path, required=True)
    write.add_argument("--summary", type=Path, required=True)
    write.add_argument("--git-commit", required=True)
    write.add_argument("--suite", choices=suite_names(), required=True)
    write.add_argument("--smp", type=int, choices=(1, 4), required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command != "write":
        raise AssertionError(f"unhandled command: {args.command}")
    serial_text = args.serial.read_text(errors="replace")
    loaded_boot_result = json.loads(args.boot_result.read_text())
    boot_result = _require_mapping(loaded_boot_result, "boot result")
    document = build_result_document(
        parse_ltp_serial(
            serial_text,
            selected_manifest_names(args.manifest.read_text()),
        ),
        boot_result=boot_result,
        git_commit=args.git_commit,
        smp=args.smp,
        suite=args.suite,
    )
    _publish_text(args.result, json.dumps(document, indent=2, sort_keys=True) + "\n")
    _publish_text(args.summary, summary_text(document))
    print(summary_text(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
