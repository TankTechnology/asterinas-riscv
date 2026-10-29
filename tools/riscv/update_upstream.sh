#!/bin/sh
# Fetch official Asterinas main over HTTPS and replay the downstream commit
# stack in this worktree.  The old fork history is not merged: its merge base
# with upstream is from 2022 and the trees are structurally different.
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repo_root"

url=${UPSTREAM_URL:-https://github.com/asterinas/asterinas.git}

if [ -n "$(git status --porcelain)" ]; then
    printf '%s\n' "error: worktree is dirty; commit or move local changes first" >&2
    exit 2
fi

printf '%s\n' "fetching $url main"
git fetch --no-tags "$url" main:refs/remotes/upstream/main
git rebase upstream/main

printf '%s\n' "rebased onto $(git rev-parse --short upstream/main)"
printf '%s\n' "next: rebuild the RISC-V kernel and run the Debian direct gate"
