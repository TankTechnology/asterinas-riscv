#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Inject the Claude Code binary and its settings.json into a Debian root
# image. The settings file carries the API env; it is copied read-only from
# the host and never printed.

set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
IMAGE=${1:-$REPO_ROOT/target/debian-x86-toolchain3/rootfs/debian-root.ext2}
CLAUDE=${CLAUDE_BINARY:-/opt/claude-code/bin/claude}
SETTINGS=${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}

[[ -f $IMAGE ]] || { echo "missing image: $IMAGE" >&2; exit 2; }
[[ -x $CLAUDE ]] || { echo "missing claude binary: $CLAUDE" >&2; exit 2; }
[[ -f $SETTINGS ]] || { echo "missing settings: $SETTINGS" >&2; exit 2; }

binary_bytes=$(stat -c '%s' "$CLAUDE")
free_blocks=$(dumpe2fs -h "$IMAGE" 2>/dev/null | awk '/^Free blocks:/{print $3}')
block_size=$(dumpe2fs -h "$IMAGE" 2>/dev/null | awk '/^Block size:/{print $3}')
need_blocks=$((binary_bytes / block_size + 16384))
if [[ -z $free_blocks || $free_blocks -lt $need_blocks ]]; then
    echo "not enough space: free_blocks=${free_blocks:-?} need_blocks=$need_blocks" >&2
    exit 3
fi

ds() { sudo -n debugfs -w -R "$1" "$IMAGE" >/dev/null 2>&1 || true; }

# debugfs has no recursive mkdir and refuses to replace existing files.
ds "mkdir /opt"
ds "mkdir /opt/claude-code"
ds "mkdir /opt/claude-code/bin"
ds "mkdir /root"
ds "mkdir /root/.claude"
ds "rm /opt/claude-code/bin/claude"
ds "rm /root/.claude/settings.json"

ds "write $CLAUDE /opt/claude-code/bin/claude"
ds "sif /opt/claude-code/bin/claude mode 0100755"
ds "write $SETTINGS /root/.claude/settings.json"
ds "sif /root/.claude/settings.json mode 0100600"

for path in /opt/claude-code/bin/claude /root/.claude/settings.json; do
    line=$(sudo -n debugfs -R "stat $path" "$IMAGE" 2>/dev/null | head -1)
    [[ $line == *Inode* ]] || { echo "injection failed: $path" >&2; exit 4; }
done

echo "injected claude ($binary_bytes bytes) and settings into $IMAGE"
echo "  free_blocks before=$free_blocks block_size=$block_size"
