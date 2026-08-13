#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Resolve the Alpine package dependency closure for a root package.

M3 needs a riscv64 musl rootfs carrying Nix and every shared library it links.
Alpine ships a prebuilt riscv64 musl ``nix`` in its ``edge`` community repo; this
script walks the APKINDEX metadata (``P``/``V``/``D``/``p`` fields) to compute
the full install closure without needing apk-tools on the host.

Usage:
    resolve_deps.py --root nix \
        --index main=/path/main-APKINDEX \
        --index community=/path/community-APKINDEX \
        --index testing=/path/testing-APKINDEX \
        --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

# Repo priority: main > community > testing (apk resolves in this order).
REPO_PRIORITY = {"main": 0, "community": 1, "testing": 2}

# Shared-object dependencies that are satisfied by packages we resolve; these
# tokens map to a package through the ``p:`` provides field like everything else,
# so no special-casing is required. The two exceptions are handled in
# `_resolve_token`: absolute paths (/bin/sh -> busybox) and self-referential
# `name=version` constraints.


@dataclass
class Package:
    name: str
    version: str
    arch: str
    repo: str
    deps: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    # Where the .apk lives on the mirror (repo-relative filename).
    filename: str = ""

    @property
    def priority(self) -> int:
        return REPO_PRIORITY.get(self.repo, 99)


def _parse_block(block: str, repo: str) -> Package | None:
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if len(line) < 2 or line[1] != ":":
            continue
        key, _, val = line.partition(":")
        fields.setdefault(key, "").strip()
        fields[key] = val
    name = fields.get("P")
    version = fields.get("V")
    arch = fields.get("A")
    if not (name and version and arch):
        return None
    deps = fields.get("D", "").split() if fields.get("D") else []
    provides = fields.get("p", "").split() if fields.get("p") else []
    pkg = Package(
        name=name,
        version=version,
        arch=arch,
        repo=repo,
        deps=deps,
        provides=provides,
    )
    pkg.filename = f"{name}-{version}.apk"
    return pkg


def parse_indexes(index_files: dict[str, str]) -> dict[str, Package]:
    """Return the best package per name (highest repo priority, then version)."""
    best: dict[str, Package] = {}
    for repo, path in index_files.items():
        with open(path, encoding="utf-8") as f:
            content = f.read()
        for block in content.split("\n\n"):
            pkg = _parse_block(block, repo)
            if pkg is None:
                continue
            prev = best.get(pkg.name)
            if prev is None or (pkg.priority, pkg.version) < (prev.priority, prev.version):
                best[pkg.name] = pkg
    return best


def _version_key(version: str) -> tuple:
    """Best-effort numeric-ish sort key so '2.31.5-r1' > '2.31.4-r10'."""
    m = re.match(r"^(\d+(?:\.\d+)*)", version)
    head = m.group(1) if m else version
    nums = tuple(int(x) for x in head.split("."))
    r = re.search(r"-r(\d+)$", version)
    return (nums, int(r.group(1)) if r else 0)


def build_providers(packages: dict[str, Package]) -> dict[str, list[Package]]:
    """Map every dependency token (so:/cmd:/pc:/bare name) to its providers."""
    providers: dict[str, list[Package]] = defaultdict(list)
    for pkg in packages.values():
        # A package always satisfies its own bare name.
        providers[pkg.name].append(pkg)
        # And any `name=version` / `name>version` self-reference.
        for tok in pkg.provides:
            providers[tok].append(pkg)
            # so:/cmd:/pc: provides carry a version suffix (`so:libcurl.so.4=4.8.0`),
            # but dependencies reference the bare token (`so:libcurl.so.4`).
            if "=" in tok:
                base = tok.split("=", 1)[0]
                providers[base].append(pkg)
    for tok in providers:
        providers[tok].sort(key=lambda p: (p.priority, _version_key(p.version)))
    return providers


def _resolve_token(token: str, providers: dict[str, list[Package]]) -> Package | None:
    """Resolve a single apk dependency token to a package."""
    # Conflict markers are not installs.
    if token.startswith("!"):
        return None
    # Absolute path dependency -> busybox (provides /bin/sh).
    if token.startswith("/"):
        return providers.get("busybox", [None])[0]
    # Strip a version constraint: `foo=1`, `foo>=1`, `foo<1`, `foo~1`.
    base = re.split(r"[=<>~]", token)[0]
    candidates = providers.get(token) or providers.get(base) or []
    return candidates[0] if candidates else None


def resolve_closure(
    roots: Iterable[str],
    packages: dict[str, Package],
    providers: dict[str, list[Package]],
) -> list[Package]:
    """Breadth-first dependency closure, returned topologically (deps first)."""
    selected: dict[str, Package] = {}
    # Fix the ordering for deterministic output.
    queue = list(roots)

    def visit(name: str) -> None:
        if name in selected:
            return
        pkg = packages.get(name)
        if pkg is None:
            print(f"warning: root/dependency '{name}' not in any index", file=sys.stderr)
            return
        selected[name] = pkg
        queue.append(name)

    for root in roots:
        visit(root)

    # Keep processing until no new deps appear.
    index = 0
    while index < len(queue):
        name = queue[index]
        index += 1
        pkg = selected[name]
        for dep in pkg.deps:
            resolved = _resolve_token(dep, providers)
            if resolved is None:
                # Tolerate missing optional/provider-less tokens but report them.
                print(f"warning: unresolved dep '{dep}' for {name}", file=sys.stderr)
                continue
            if resolved.name in selected:
                continue
            selected[resolved.name] = resolved
            queue.append(resolved.name)

    # Topological order: a package appears only after its dependencies.
    ordered: list[Package] = []
    visited: set[str] = set()
    seen: set[str] = set()

    def topo(pkg: Package) -> None:
        if pkg.name in seen:
            return
        seen.add(pkg.name)
        for dep in pkg.deps:
            r = _resolve_token(dep, providers)
            if r is not None and r.name in selected:
                topo(r)
        if pkg.name not in visited:
            visited.add(pkg.name)
            ordered.append(pkg)

    for name in queue:
        topo(selected[name])

    return ordered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], help="root package name")
    parser.add_argument(
        "--index",
        action="append",
        default=[],
        metavar="REPO=PATH",
        help="APKINDEX file keyed by repo (e.g. main=/tmp/main-APKINDEX)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    if not args.root:
        parser.error("provide at least one --root package")
    if not args.index:
        parser.error("provide at least one --index REPO=PATH")

    index_files: dict[str, str] = {}
    for spec in args.index:
        repo, _, path = spec.partition("=")
        index_files[repo] = path

    packages = parse_indexes(index_files)
    providers = build_providers(packages)
    closure = resolve_closure(args.root, packages, providers)

    if args.json:
        out = [
            {
                "name": p.name,
                "version": p.version,
                "arch": p.arch,
                "repo": p.repo,
                "filename": p.filename,
            }
            for p in closure
        ]
        print(json.dumps(out, indent=2))
    else:
        for p in closure:
            print(f"{p.repo}\t{p.name}\t{p.version}\t{p.filename}")

    total_size = 0
    print(
        f"\n# {len(closure)} packages resolved",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
