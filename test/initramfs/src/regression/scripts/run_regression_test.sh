#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

SCRIPT_DIR=/test

# The formal SMP4 I-cache job is deliberately isolated from the full regression
# suite.  This keeps unrelated regressions from preventing the architecture
# contract from running and makes its terminal marker unambiguous.
if grep -qw 'RISCV_ICACHE_REQUIRE_SMP4=1' /proc/cmdline; then
    if [ "$(uname -m)" != "riscv64" ]; then
        echo "RISCV_ICACHE_REQUIRE_SMP4=1 requires a riscv64 guest" >&2
        exit 1
    fi

    echo "Running formal SMP4 cross-hart I-cache regression"
    "${SCRIPT_DIR}/process/riscv_flush_icache/riscv_flush_icache" --require-smp4
    echo "All regression tests passed."
    exit 0
fi

for dir in $(find -L "${SCRIPT_DIR}" -mindepth 1 -maxdepth 1 -type d); do
    if [ -x "${dir}/run_test.sh" ]; then
        echo "Running test in $dir"
        (cd "$dir" && ./run_test.sh)
        echo "All test in $dir passed."
    else
        echo "Skipping $dir (no executable TEST_SCRIPT)"
    fi
done

echo "All regression tests passed."
