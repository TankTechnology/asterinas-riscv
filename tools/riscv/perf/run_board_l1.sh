#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Build the L1 micro-benchmark, run it on the Megrez board over SSH and print
# the median of each metric.
#
#   tools/riscv/perf/run_board_l1.sh [samples] [ssh-target]
#
# Defaults: 3 samples, root@58.206.232.122 port 2222. The board must already
# be running Asterinas with dropbear started on 2222 (the debug console's
# `dropbear -p 2222 -r /etc/dropbear/dropbear_ed25519_host_key -E`).
#
# Every metric is reported in the unit the program prints, and the median is
# taken across samples so a single scheduling hiccup cannot be mistaken for the
# baseline. The source is `board_l1.c`; keep the two together.
set -euo pipefail

SAMPLES=${1:-3}
TARGET=${2:-root@58.206.232.122}
PORT=${L1_SSH_PORT:-2222}
KEY=${L1_SSH_KEY:-$HOME/.ssh/asterinas_megrez}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
BIN=/tmp/board_l1
OUT=$(mktemp)

cleanup() { rm -f "$OUT"; }
trap cleanup EXIT

echo "== building"
riscv64-linux-musl-gcc -O2 -static -pthread -L/usr/riscv64-linux-gnu/lib \
    -o "$BIN" "$HERE/board_l1.c"

echo "== deploying to $TARGET"
scp -q -i "$KEY" -P "$PORT" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o BatchMode=yes "$BIN" "$TARGET:/tmp/board_l1"

echo "== running $SAMPLES sample(s)"
for _ in $(seq 1 "$SAMPLES"); do
    ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes "$TARGET" \
        'chmod +x /tmp/board_l1; /tmp/board_l1' >>"$OUT"
done

echo "== median"
python3 - "$OUT" <<'PY'
import statistics
import sys

samples: dict[str, list[float]] = {}
units: dict[str, str] = {}
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line or "=" not in line:
        continue
    name, rest = line.split("=", 1)
    value, _, unit = rest.partition(" ")
    samples.setdefault(name, []).append(float(value))
    units[name] = unit

width = max(len(name) for name in samples)
for name in sorted(samples):
    values = samples[name]
    median = statistics.median(values)
    spread = f"{min(values):.2f}..{max(values):.2f}" if len(values) > 1 else ""
    print(f"{name:<{width}}  {median:>10.2f} {units[name]:<6} {spread}")
PY
