#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Select enabled LTP syscall entries without silently dropping tests."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class UnavailableTest:
    """One enabled name that cannot be placed in the runtime manifest."""

    name: str
    reason: str


@dataclass(frozen=True)
class ManifestSelection:
    """Selected runtest lines and complete provenance for unavailable names."""

    lines: tuple[str, ...]
    unavailable: tuple[UnavailableTest, ...]
    requested: tuple[str, ...]


def _enabled_names(enabled_text: str) -> tuple[str, ...]:
    names = tuple(
        stripped
        for line in enabled_text.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )
    seen: set[str] = set()
    for name in names:
        if any(character.isspace() for character in name):
            raise ValueError(f"enabled test must be one name: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate enabled test: {name}")
        seen.add(name)
    return names


def _runtest_entries(runtest_text: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for line_number, line in enumerate(runtest_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            raise ValueError(f"malformed runtest line {line_number}")
        tag, binary = fields[:2]
        if tag in entries:
            raise ValueError(f"duplicate runtest tag: {tag}")
        entries[tag] = (binary, stripped)
    return entries


def _unique_subset(subset: Iterable[str]) -> tuple[str, ...]:
    names = tuple(subset)
    if len(set(names)) != len(names):
        raise ValueError("duplicate subset tag")
    return names


def select_manifest(
    enabled_text: str,
    runtest_text: str,
    available: set[str],
    subset: Iterable[str] = (),
) -> ManifestSelection:
    """Match enabled names to runnable entries and classify every omission."""

    enabled = _enabled_names(enabled_text)
    entries = _runtest_entries(runtest_text)
    selected_subset = _unique_subset(subset)
    if selected_subset:
        enabled_set = set(enabled)
        unknown = tuple(name for name in selected_subset if name not in enabled_set)
        if unknown:
            raise ValueError(f"unknown subset tag: {unknown[0]}")
        requested = selected_subset
    else:
        requested = enabled

    lines: list[str] = []
    unavailable: list[UnavailableTest] = []
    for name in requested:
        entry = entries.get(name)
        if entry is None:
            unavailable.append(UnavailableTest(name, "not-in-runtest"))
            continue
        binary, line = entry
        if binary not in available:
            unavailable.append(UnavailableTest(name, "missing-binary"))
            continue
        lines.append(line)

    if selected_subset and unavailable:
        raise ValueError(f"subset tag is unavailable: {unavailable[0].name}")
    return ManifestSelection(
        lines=tuple(lines),
        unavailable=tuple(unavailable),
        requested=requested,
    )


def _publish_text(path: Path, payload: str) -> None:
    """Atomically publish a new file without replacing existing evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
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


def _nonnegative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    select = subparsers.add_parser("select")
    select.add_argument("--enabled", type=Path, required=True)
    select.add_argument("--runtest", type=Path, required=True)
    select.add_argument("--bin-dir", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--unavailable-output", type=Path, required=True)
    select.add_argument("--expected-count", type=_nonnegative_integer)
    select.add_argument("--tag", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command != "select":
        raise AssertionError(f"unhandled command: {args.command}")
    if not args.bin_dir.is_dir():
        raise ValueError(f"binary directory does not exist: {args.bin_dir}")
    available = {
        entry.name
        for entry in args.bin_dir.iterdir()
        if entry.is_file()
    }
    selection = select_manifest(
        args.enabled.read_text(),
        args.runtest.read_text(),
        available,
        subset=args.tag,
    )
    if args.expected_count is not None and len(selection.lines) != args.expected_count:
        raise ValueError(
            f"expected {args.expected_count} selected tests, got {len(selection.lines)}"
        )
    if args.output == args.unavailable_output:
        raise ValueError("manifest and unavailable outputs must differ")
    for output in (args.output, args.unavailable_output):
        if output.exists() or output.is_symlink():
            raise FileExistsError(output)
    manifest_payload = "".join(f"{line}\n" for line in selection.lines)
    unavailable_payload = (
        json.dumps(
            [asdict(item) for item in selection.unavailable],
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    _publish_text(args.output, manifest_payload)
    _publish_text(args.unavailable_output, unavailable_payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
