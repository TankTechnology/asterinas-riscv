#!/usr/bin/env python3
"""Build and validate the E1-E5 gate refactor reference inventory.

The scanner is deliberately conservative.  A literal reference from code,
Makefile, overlays, or tests keeps a candidate alive; documentation-only
references are reported for provenance but do not count as active callers.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROTECTED_PREFIXES = (
    "kernel/",
    "ostd/",
    "docs/porting/evidence/",
    "docs/porting/receipts/",
    "target/",
)
TEXT_SUFFIXES = {
    ".conf",
    ".html",
    ".json",
    ".md",
    ".py",
    ".service",
    ".sh",
    ".svg",
    ".toml",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Reference:
    path: str
    category: str


@dataclass(frozen=True)
class Candidate:
    path: str
    tracked: bool
    references: tuple[Reference, ...]

    @property
    def active_references(self) -> tuple[Reference, ...]:
        return tuple(
            ref
            for ref in self.references
            if ref.category in {"code", "makefile", "overlay", "test"}
        )


def _repo_files(repository: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in repository.rglob("*"):
        relative = path.relative_to(repository).as_posix()
        if any(
            relative.startswith(prefix)
            for prefix in (".git/", "target/", ".worktrees/")
        ):
            continue
        try:
            is_file = path.is_file()
        except OSError:
            continue
        if not is_file:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Makefile", "AGENTS.md"}:
            files.append(path)
    return tuple(files)


def _tracked(repository: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def _category(path: str) -> str:
    if path == "Makefile":
        return "makefile"
    if path.startswith("tools/riscv/tests/") or path.startswith("test/"):
        return "test"
    if path.endswith("_overlay.json") or "/overlay" in path:
        return "overlay"
    if path.startswith("docs/") or path.endswith(".md"):
        return "docs"
    return "code"


def _candidate_paths(repository: Path, root: Path, tests: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for base in (root, tests):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                paths.append(path)
    return tuple(sorted(set(paths)))


def scan(repository: Path, root: Path, tests: Path, makefile: Path) -> tuple[Candidate, ...]:
    repository = repository.resolve()
    root = (repository / root).resolve() if not root.is_absolute() else root.resolve()
    tests = (repository / tests).resolve() if not tests.is_absolute() else tests.resolve()
    makefile = (repository / makefile).resolve() if not makefile.is_absolute() else makefile.resolve()
    all_files = _repo_files(repository)
    tracked = _tracked(repository)
    candidates: list[Candidate] = []
    for candidate_path in _candidate_paths(repository, root, tests):
        relative = candidate_path.relative_to(repository).as_posix()
        names = {relative, candidate_path.name}
        if candidate_path.suffix == ".py":
            names.add(candidate_path.stem)
        refs: list[Reference] = []
        for source in all_files:
            if source == candidate_path:
                continue
            try:
                content = source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not any(name in content for name in names):
                continue
            source_relative = source.relative_to(repository).as_posix()
            refs.append(Reference(source_relative, _category(source_relative)))
        candidates.append(Candidate(relative, relative in tracked, tuple(sorted(refs, key=lambda ref: ref.path))))
    return tuple(candidates)


def _markdown(candidates: Iterable[Candidate]) -> str:
    lines = [
        "# E1–E5 重构引用报告",
        "",
        "扫描规则：代码、Makefile、overlay 和测试引用视为活动引用；文档引用只用于追溯。",
        "",
        "| 路径 | tracked | 活动引用 | 文档引用 | 处理建议 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for candidate in candidates:
        active = [ref.path for ref in candidate.active_references]
        docs = [ref.path for ref in candidate.references if ref.category == "docs"]
        suggestion = "保留" if active else "候选删除，需人工确认动态引用"
        lines.append(
            f"| `{candidate.path}` | {'yes' if candidate.tracked else 'no'} | "
            f"{len(active)} | {len(docs)} | {suggestion} |"
        )
        if active:
            lines.append(f"| ↳ 活动引用 |  | `{', '.join(active[:8])}` |  |  |")
    return "\n".join(lines) + "\n"


def _load_manifest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    return value


def validate_manifest(repository: Path, manifest_path: Path) -> list[str]:
    value = _load_manifest(manifest_path)
    keep = {str(item) for item in value.get("keep", [])}
    raw_delete_items = value.get("delete", [])
    delete_items = [
        item
        for item in raw_delete_items
        if isinstance(item, dict) and "path" in item
    ]
    delete = {
        str(item["path"])
        for item in delete_items
    }
    errors: list[str] = []
    overlap = keep & delete
    if overlap:
        errors.append(f"keep/delete overlap: {sorted(overlap)}")
    for path in sorted(delete):
        if any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES):
            errors.append(f"protected delete path: {path}")
        item = next(item for item in delete_items if str(item["path"]) == path)
        state = item.get("state", "planned")
        if state not in {"planned", "removed"}:
            errors.append(f"invalid delete state for {path}: {state!r}")
        exists = (repository / path).exists()
        if state == "planned" and not exists:
            errors.append(f"missing delete path: {path}")
        if state == "removed" and exists:
            errors.append(f"removed path still exists: {path}")
    for item in raw_delete_items:
        if not isinstance(item, dict) or not item.get("references_checked"):
            errors.append(f"unreviewed delete item: {item!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--makefile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    repository = args.repository.resolve()
    candidates = scan(repository, args.root, args.tests, args.makefile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(_markdown(candidates), encoding="utf-8")
    if args.verify:
        errors = validate_manifest(repository, args.verify)
        if errors:
            for error in errors:
                print(error)
            return 1
    print(f"scanned {len(candidates)} candidates; report={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
