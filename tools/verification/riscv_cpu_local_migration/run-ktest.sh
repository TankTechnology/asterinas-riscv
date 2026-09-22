#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

# Run inside the persistent project development container. Check guest test
# counts as well as the process status: some SBI/QEMU combinations report a
# successful host exit even when the guest test runner reports failure.
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
mkdir -p "$repo_dir/target/riscv-trap-gp"
run_dir=$(mktemp -d "$repo_dir/target/riscv-trap-gp/ktest.XXXXXXXX")
printf 'Artifacts: %s\n' "$run_dir"
export SMP=4 CONSOLE=ttyS0
cd "$repo_dir/ostd"

for test_name in kernel_trap_return_preserves_current_cpu_gp user_trap_return_restores_user_gp; do
    test_dir="$run_dir/$test_name"
    mkdir "$test_dir"
    export ASTERINAS_QEMU_LOG_DIR="$test_dir"
    status=0
    timeout --kill-after=5s 120s cargo osdk test "$test_name" \
        --target-arch riscv64 --scheme riscv --features riscv_sv39_mode \
        > "$test_dir/runner.log" 2>&1 || status=$?
    if [[ $status -ne 0 ]] \
        || ! grep -Fq '1 passed; 0 failed;' "$test_dir/runner.log" \
        || ! grep -Fq "::$test_name ..." "$test_dir/runner.log" \
        || ! grep -Fq '[ktest runner] All crates tested.' "$test_dir/runner.log" \
        || grep -Fq '[caught panic]' "$test_dir/runner.log"; then
        tail -n 60 "$test_dir/runner.log" >&2
        printf 'FAIL: %s (host exit %s)\n' "$test_name" "$status" >&2
        exit 1
    fi
    printf 'PASS: %s (SMP=4, one test executed)\n' "$test_name"
done
