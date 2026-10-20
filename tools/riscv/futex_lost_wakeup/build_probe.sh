#!/bin/sh
set -eu

out=${1:-/mnt/mydisk/futex-lost-wakeup-r1}
mkdir -p "$out"
src=$(dirname "$0")/futex_lost_wakeup_probe.c

gcc -O2 -static -pthread -Wall -Wextra -Werror "$src" \
	-o "$out/futex-lost-wakeup-x86_64"

if command -v riscv64-linux-gnu-gcc >/dev/null 2>&1; then
	riscv64-linux-gnu-gcc -O2 -static -pthread -Wall -Wextra -Werror \
		"$src" -o "$out/futex-lost-wakeup-riscv64"
else
	echo "riscv64-linux-gnu-gcc unavailable" >&2
	exit 3
fi

sha256sum "$out"/futex-lost-wakeup-* > "$out/SHA256SUMS"
file "$out"/futex-lost-wakeup-*
