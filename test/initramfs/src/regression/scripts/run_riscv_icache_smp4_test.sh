#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

if [ "$(uname -m)" != "riscv64" ]; then
    echo "RISC-V SMP4 icache regression requires a riscv64 guest" >&2
    exit 1
fi

/test/memory/riscv_icache/cross_hart --require-smp4
echo "RISC-V SMP4 icache regression passed."
