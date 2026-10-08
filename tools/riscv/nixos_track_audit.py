#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

"""Audit commit admission from a NixOS development track."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path


ALLOWED_DISPOSITIONS = frozenset(
    {
        "already-main",
        "existing-pr",
        "portable",
        "rewrite",
        "retire",
        "unclassified",
    }
)
_AUTOMATIC_DISPOSITIONS = frozenset({"already-main", "unclassified"})
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_CHERRY_PATTERN = re.compile(r"([+-]) ([0-9a-f]{40}) (.+)")
_OVERRIDE_FIELDS = frozenset(
    {
        "disposition",
        "reason",
        "destination",
        "subsystem",
        "main_equivalent_or_pr",
        "verification",
    }
)
_METADATA_FIELDS = (
    "reason",
    "destination",
    "subsystem",
    "main_equivalent_or_pr",
    "verification",
)


@dataclass(frozen=True)
class AdmissionRecord:
    """One commit and its automatic and reviewed admission dispositions."""

    source_commit: str
    subject: str
    automatic_disposition: str
    disposition: str
    reason: str = ""
    destination: str = ""
    subsystem: str = ""
    main_equivalent_or_pr: str = ""
    verification: str = ""


def _is_commit_id(value: object) -> bool:
    return isinstance(value, str) and _COMMIT_PATTERN.fullmatch(value) is not None


def parse_cherry(text: str) -> list[AdmissionRecord]:
    """Parse strict ``git cherry -v`` output in its original order."""

    records: list[AdmissionRecord] = []
    seen_commits: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        match = _CHERRY_PATTERN.fullmatch(line)
        if match is None or not match.group(3).strip():
            raise ValueError(
                f"invalid git cherry record on line {line_number}: {line!r}"
            )

        sign, source_commit, subject = match.groups()
        if source_commit in seen_commits:
            raise ValueError(f"duplicate commit in git cherry output: {source_commit}")
        seen_commits.add(source_commit)

        automatic_disposition = (
            "already-main" if sign == "-" else "unclassified"
        )
        records.append(
            AdmissionRecord(
                source_commit=source_commit,
                subject=subject,
                automatic_disposition=automatic_disposition,
                disposition=automatic_disposition,
            )
        )
    return records


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _validate_overrides(
    overrides: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    validated: dict[str, dict[str, str]] = {}
    for source_commit, raw_fields in overrides.items():
        if not _is_commit_id(source_commit):
            raise ValueError(
                f"invalid override commit {source_commit!r}; expected lowercase 40-hex"
            )
        if not isinstance(raw_fields, Mapping):
            raise ValueError(
                f"override for {source_commit} must be a JSON object"
            )

        unknown_fields = set(raw_fields) - _OVERRIDE_FIELDS
        if unknown_fields:
            names = ", ".join(sorted(repr(name) for name in unknown_fields))
            raise ValueError(
                f"unknown override field for {source_commit}: {names}"
            )

        fields: dict[str, str] = {}
        for name, value in raw_fields.items():
            if not isinstance(name, str):
                raise ValueError(
                    f"override field name for {source_commit} must be a string"
                )
            if not isinstance(value, str):
                raise ValueError(
                    f"override field {name!r} for {source_commit} must be a string"
                )
            fields[name] = value

        disposition = fields.get("disposition")
        if disposition is not None:
            if disposition not in ALLOWED_DISPOSITIONS:
                raise ValueError(
                    f"invalid disposition for {source_commit}: {disposition!r}"
                )
            if not fields.get("reason", "").strip():
                raise ValueError(
                    f"explicit classification for {source_commit} "
                    "requires a nonempty reason"
                )
            if not fields.get("destination", "").strip():
                raise ValueError(
                    f"explicit classification for {source_commit} "
                    "requires a nonempty destination"
                )
        validated[source_commit] = fields
    return validated


def load_overrides(path: Path) -> dict[str, dict[str, str]]:
    """Load and strictly validate a JSON commit override mapping."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read overrides {path}: {error}") from error

    try:
        document = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid overrides JSON in {path}: {error}") from error

    if not isinstance(document, Mapping):
        raise ValueError("overrides document must be a JSON object")
    return _validate_overrides(document)


def apply_overrides(
    records: Sequence[AdmissionRecord],
    overrides: Mapping[str, Mapping[str, str]],
) -> list[AdmissionRecord]:
    """Return records with reviewed classifications and metadata applied."""

    validated_overrides = _validate_overrides(overrides)
    record_commits = {record.source_commit for record in records}
    unknown_commits = set(validated_overrides) - record_commits
    if unknown_commits:
        commits = ", ".join(sorted(unknown_commits))
        raise ValueError(f"override references unknown commit: {commits}")

    classified: list[AdmissionRecord] = []
    for record in records:
        fields = validated_overrides.get(record.source_commit)
        if fields is None:
            classified.append(replace(record))
            continue

        disposition = fields.get("disposition", record.disposition)
        if (
            record.automatic_disposition == "already-main"
            and disposition != "already-main"
        ):
            raise ValueError(
                "override contradicts patch-equivalent already-main commit "
                f"{record.source_commit}"
            )

        changes = {
            name: fields.get(name, getattr(record, name))
            for name in _METADATA_FIELDS
        }
        classified.append(
            replace(record, disposition=disposition, **changes)
        )
    return classified


def _validate_record(record: AdmissionRecord) -> None:
    if not _is_commit_id(record.source_commit):
        raise ValueError(
            f"record commit must be a lowercase 40-character hex ID: "
            f"{record.source_commit!r}"
        )
    if not record.subject.strip():
        raise ValueError(f"record subject must be nonempty: {record.source_commit}")
    if record.automatic_disposition not in _AUTOMATIC_DISPOSITIONS:
        raise ValueError(
            f"invalid automatic disposition for {record.source_commit}: "
            f"{record.automatic_disposition!r}"
        )
    if record.disposition not in ALLOWED_DISPOSITIONS:
        raise ValueError(
            f"invalid disposition for {record.source_commit}: {record.disposition!r}"
        )


def render_manifest(
    base: str,
    track: str,
    records: Sequence[AdmissionRecord],
) -> dict[str, object]:
    """Render a deterministic schema-version-1 admission manifest."""

    for name, commit in (("base", base), ("track", track)):
        if not _is_commit_id(commit):
            raise ValueError(
                f"{name} must be a lowercase 40-character hex commit ID: {commit!r}"
            )

    seen_commits: set[str] = set()
    rendered_records: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for record in records:
        _validate_record(record)
        if record.source_commit in seen_commits:
            raise ValueError(f"duplicate commit in records: {record.source_commit}")
        seen_commits.add(record.source_commit)
        counts[record.disposition] += 1
        rendered_records.append(
            {
                "source_commit": record.source_commit,
                "subject": record.subject,
                "automatic_disposition": record.automatic_disposition,
                "classification": record.disposition,
                "reason": record.reason,
                "destination": record.destination,
                "subsystem": record.subsystem,
                "main_equivalent_or_pr": record.main_equivalent_or_pr,
                "verification": record.verification,
            }
        )

    return {
        "schema_version": 1,
        "base": base,
        "track": track,
        "counts": dict(sorted(counts.items())),
        "records": rendered_records,
    }


def _run_git(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        command = "git " + arguments[0]
        raise RuntimeError(f"{command} failed: {detail}") from error
    except OSError as error:
        raise RuntimeError(f"cannot execute git: {error}") from error
    return result.stdout


def _resolve_commit(revision: str) -> None:
    _run_git(
        ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"]
    )


def _write_json_atomically(path: Path, document: Mapping[str, object]) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(0o644)
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _parse_args(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit commit admission from a NixOS development track."
    )
    parser.add_argument("--base", required=True, help="exact base commit ID")
    parser.add_argument("--track", required=True, help="exact track commit ID")
    parser.add_argument(
        "--overrides", required=True, type=Path, help="reviewed override JSON"
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="manifest output path"
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command-line admission audit."""

    options = _parse_args(arguments)
    try:
        for name, commit in (("base", options.base), ("track", options.track)):
            if not _is_commit_id(commit):
                raise ValueError(
                    f"{name} must be a lowercase 40-character hex commit ID: "
                    f"{commit!r}"
                )
        base = options.base
        track = options.track
        _resolve_commit(base)
        _resolve_commit(track)
        cherry_output = _run_git(["cherry", "-v", base, track])
        records = parse_cherry(cherry_output)
        overrides = load_overrides(options.overrides)
        classified = apply_overrides(records, overrides)
        manifest = render_manifest(base, track, classified)
        _write_json_atomically(options.output, manifest)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    counts = manifest["counts"]
    assert isinstance(counts, dict)
    for disposition, count in counts.items():
        print(f"{disposition}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
