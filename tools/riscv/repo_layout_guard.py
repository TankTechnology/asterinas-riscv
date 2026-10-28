#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Keep new RISC-V tooling files from becoming unreadable monoliths."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = Path("tools/riscv")
ALLOWLIST = Path(__file__).with_name("layout_allowlist.json")
MAX_LINES = 2_000


def count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        return sum(1 for _ in stream)


def load_allowlist(path: Path = ALLOWLIST) -> dict[str, int]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise ValueError("unsupported layout allowlist schema")
    if value.get("max_lines") != MAX_LINES:
        raise ValueError("layout allowlist max_lines does not match the guard")
    files = value.get("files")
    if not isinstance(files, dict):
        raise ValueError("layout allowlist files must be an object")
    result: dict[str, int] = {}
    for name, limit in files.items():
        if not isinstance(name, str) or not isinstance(limit, int):
            raise ValueError("layout allowlist entries must be string -> integer")
        result[name] = limit
    return result


def scan(
    root: Path = REPO_ROOT,
    allowlist: Mapping[str, int] | None = None,
) -> list[str]:
    """Return current oversized-file violations.

    The archive is deliberately outside the guard: retired research material
    may be large and is not part of the active tooling surface.
    """

    limits = load_allowlist() if allowlist is None else dict(allowlist)
    violations: list[str] = []
    tools = root / TOOLS_ROOT
    if not tools.is_dir():
        return [f"missing tools directory: {tools}"]
    for path in sorted(tools.rglob("*")):
        if (
            not path.is_file()
            or path.suffix not in (".py", ".sh")
            or "archive" in path.parts
            or "__pycache__" in path.parts
        ):
            continue
        relative = path.relative_to(root).as_posix()
        lines = count_lines(path)
        if relative not in limits:
            if lines > MAX_LINES:
                violations.append(
                    f"{relative}: {lines} lines exceeds {MAX_LINES} (add a reviewed split, not an allowlist entry)"
                )
            continue
        if lines > limits[relative]:
            violations.append(
                f"{relative}: grew from {limits[relative]} to {lines} lines; only shrink the allowlist"
            )
    for name in sorted(limits):
        if not (root / name).is_file():
            violations.append(f"allowlisted file disappeared: {name}")
    return violations


def main() -> int:
    violations = scan()
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
