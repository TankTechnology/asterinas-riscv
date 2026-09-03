#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(uname -m)" != "riscv64" ]; then
    echo "RISC-V SMP4 affinity regression requires a riscv64 guest" >&2
    exit 1
fi

output=$(/test/process/cpu_affinity/cpu_affinity)
printf '%s\n' "$output"
printf '%s\n' "$output" | grep -Fxq \
    "Observed affinity migration across 4 CPU(s) for 32 rounds"
echo "RISC-V SMP4 affinity migration regression passed."
