#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Reject staged changes that carry credentials or real public addresses.
#
# The rules follow xaj_vibenote.md section 7: API keys, token and password
# assignments, PEM private-key blocks, cloud and GitHub tokens, and public
# IPv4 addresses that are not documentation, private, loopback, link-local or
# benchmarking ranges. Well-known public DNS resolvers are allowed because the
# documents use them as configuration examples.
#
# Usage:
#   tools/check_secrets.sh          # added lines of the staged diff
#   tools/check_secrets.sh --all    # every line of every staged file
#
# Exit status: 0 clean, 1 findings, 2 usage or git error. Each finding names
# the file and line so the caller can show it directly.

set -euo pipefail

usage() {
    cat <<'EOF'
usage: tools/check_secrets.sh [--all]

Scans the git staging area for credentials and real public IPv4 addresses.
  (default)  scan the added lines of `git diff --cached`
  --all      scan every line of every staged file
EOF
}

if (( $# > 1 )); then
    usage >&2
    exit 2
fi

mode=staged
case "${1:-}" in
    "") ;;
    --all) mode=all ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    printf 'error: not inside a git repository\n' >&2
    exit 2
fi

exec python3 - "$mode" <<'PY'
import re
import subprocess
import sys

MODE = sys.argv[1]

KEYWORD_RULES = (
    (re.compile(r"api[_-]?key", re.IGNORECASE), "API key"),
    (re.compile(r"token\s*=", re.IGNORECASE), "token assignment"),
    (re.compile(r"password\s*=", re.IGNORECASE), "password assignment"),
    (re.compile(r"BEGIN\s+[A-Z ]*PRIVATE KEY"), "PEM private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"), "API secret key"),
)

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")

# Public DNS services that documents name as configuration; not site
# infrastructure. Stored as octet tuples so this list is not itself an address
# the scanner would report.
PUBLIC_RESOLVERS = frozenset(
    {
        (1, 0, 0, 1),
        (1, 1, 1, 1),
        (8, 8, 4, 4),
        (8, 8, 8, 8),
        (9, 9, 9, 9),
        (114, 114, 114, 114),
        (115, 115, 115, 115),
        (119, 29, 29, 29),
        (180, 76, 76, 76),
        (223, 5, 5, 5),
        (223, 6, 6, 6),
    }
)


def is_public_address(address: str) -> bool:
    octets = tuple(int(part) for part in address.split("."))
    if any(part > 255 for part in octets):
        return False
    first, second, third, _ = octets
    if first in (0, 10, 127) or first >= 224:
        return False
    if first == 100 and 64 <= second <= 127:
        return False
    if first == 169 and second == 254:
        return False
    if first == 172 and 16 <= second <= 31:
        return False
    if first == 192 and second == 168:
        return False
    if first == 198 and 18 <= second <= 19:
        return False
    if (first, second, third) in ((192, 0, 2), (198, 51, 100), (203, 0, 113)):
        return False
    return octets not in PUBLIC_RESOLVERS


def finding_labels(text: str):
    for pattern, label in KEYWORD_RULES:
        if pattern.search(text):
            yield label
    for match in IPV4.finditer(text):
        if is_public_address(match.group(1)):
            yield "public IPv4 address"


def clean_path(path: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        return path[1:-1]
    return path


def staged_added_lines():
    result = subprocess.run(
        ["git", "diff", "--cached", "--no-color", "--unified=0", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    path = None
    number = 0
    for raw in result.stdout.splitlines():
        if raw.startswith("+++ "):
            value = clean_path(raw[4:])
            path = None if value == "/dev/null" else value.removeprefix("b/")
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            number = int(match.group(1)) if match else 0
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            yield path, number, raw[1:]
            number += 1
        elif raw.startswith(" "):
            number += 1


def staged_file_lines():
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        capture_output=True,
    )
    for raw_name in names.stdout.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", "replace")
        shown = subprocess.run(["git", "show", ":" + name], capture_output=True)
        if b"\0" in shown.stdout[:8192]:
            continue
        for number, line in enumerate(shown.stdout.decode("utf-8", "replace").splitlines(), 1):
            yield name, number, line


def main() -> int:
    lines = staged_added_lines() if MODE == "staged" else staged_file_lines()
    if lines is None:
        print("error: git diff --cached failed", file=sys.stderr)
        return 2
    hits = 0
    for path, number, line in lines:
        for label in finding_labels(line):
            print(f"{path}:{number}: {label}: {line.strip()[:160]}")
            hits += 1
    if hits:
        print(
            f"{hits} finding(s) in the staging area; use a placeholder and keep "
            "the real value in the local configuration",
            file=sys.stderr,
        )
        return 1
    return 0


sys.exit(main())
PY
