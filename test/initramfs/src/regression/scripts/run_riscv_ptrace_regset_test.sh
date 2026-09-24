#!/bin/sh
# SPDX-License-Identifier: MPL-2.0

set -eu

/test/process/ptrace/getregset_riscv
echo "RISC-V ptrace regset regression passed."
