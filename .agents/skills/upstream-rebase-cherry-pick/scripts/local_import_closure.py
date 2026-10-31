#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Print the tools.riscv import closure of modules on a source branch."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MODULE_ROOT = "tools/riscv/"
IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+(tools\.riscv\.[A-Za-z0-9_.]+)", re.MULTILINE
)


def run_git(*arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], text=True)


def module_map(branch: str) -> dict[str, str]:
    files = run_git("ls-tree", "-r", "--name-only", branch).splitlines()
    return {
        path[:-3].replace("/", "."): path
        for path in files
        if path.startswith(MODULE_ROOT) and path.endswith(".py")
    }


def normalize_target(value: str) -> str:
    if value.endswith(".py") or "/" in value:
        path = value.removeprefix("./")
        return path[:-3].replace("/", ".")
    return value


def local_imports(branch: str, modules: dict[str, str], module: str) -> set[str]:
    text = run_git("show", f"{branch}:{modules[module]}")
    return {
        imported
        for imported in IMPORT_RE.findall(text)
        if imported in modules
    }


def closure(
    branch: str, modules: dict[str, str], targets: list[str]
) -> dict[str, object]:
    unknown = [target for target in targets if target not in modules]
    pending = [target for target in targets if target in modules]
    seen: set[str] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(sorted(local_imports(branch, modules, module) - seen))
    current = Path.cwd()
    files = []
    missing = []
    for module in sorted(seen):
        path = modules[module]
        present = (current / path).is_file()
        files.append({"module": module, "path": path, "present": present})
        if not present:
            missing.append(module)
    return {
        "branch": branch,
        "targets": targets,
        "unknown_targets": unknown,
        "closure": files,
        "missing_in_current_tree": missing,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--current-tree", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)

    if not args.current_tree.is_dir():
        parser.error(f"current tree is not a directory: {args.current_tree}")
    old_cwd = Path.cwd()
    try:
        os.chdir(args.current_tree)
        modules = module_map(args.branch)
        result = closure(
            args.branch,
            modules,
            [normalize_target(target) for target in args.target],
        )
    finally:
        os.chdir(old_cwd)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"branch: {result['branch']}")
        print(f"targets: {', '.join(result['targets'])}")
        if result["unknown_targets"]:
            print(f"unknown targets: {', '.join(result['unknown_targets'])}")
        for item in result["closure"]:
            status = "present" if item["present"] else "MISSING"
            print(f"{status:8} {item['path']}")
        missing = result["missing_in_current_tree"]
        print(f"closure={len(result['closure'])} missing={len(missing)}")
        if missing:
            print("missing modules: " + ", ".join(missing))

    if args.require_complete and (
        result["unknown_targets"] or result["missing_in_current_tree"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
