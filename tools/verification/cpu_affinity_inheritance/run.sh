#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 The Asterinas Authors.
set -euo pipefail

model_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$model_dir/../../.." && pwd)
jar="$repo_dir/target/signal-job-control-model/tla2tools-1.7.4.jar"
sha256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88

if [[ ! -f "$jar" ]]; then
    printf 'missing cached TLC 1.7.4 jar: %s; no download attempted\n' "$jar" >&2
    exit 1
fi
printf '%s  %s\n' "$sha256" "$jar" | sha256sum --check --status

cache_dir="$repo_dir/target/cpu-affinity-inheritance-model"
mkdir -p -- "$cache_dir"
run_dir=$(mktemp -d "$cache_dir/run.XXXXXXXX")
printf 'Artifacts: %s\n' "$run_dir"
java -version > "$run_dir/java-version.txt" 2>&1
printf 'case\texit\tstates\n' > "$run_dir/results.tsv"

run_case() {
    local config=$1 expected_status=$2 expected_message=$3
    local status=0 log="$run_dir/$config.log"
    timeout --kill-after=5s 60s java -Xmx256m -XX:+UseSerialGC \
        -cp "$jar" tlc2.TLC -workers 1 -seed 1 -fp 0 \
        -metadir "$run_dir/$config-states" \
        -config "$model_dir/$config.cfg" \
        "$model_dir/CpuAffinityInheritance.tla" > "$log" 2>&1 || status=$?
    if [[ $status -ne $expected_status ]] || ! rg -Fq "$expected_message" "$log"; then
        printf 'unexpected result for %s (exit %s):\n' "$config" "$status" >&2
        tail -n 50 "$log" >&2
        return 1
    fi
    local counts
    counts=$(rg '^[1-9][0-9,]* states generated, [1-9][0-9,]* distinct states found' "$log")
    printf '%s\t%s\t%s\n' "$config" "$status" "$counts" | tee -a "$run_dir/results.tsv"
    if [[ $expected_status -ne 0 ]]; then
        sed -n '/^State 1:/,$p' "$log" > "$run_dir/$config.trace.txt"
        test -s "$run_dir/$config.trace.txt"
    fi
}

run_case ResetToAll 12 'Invariant ChildInheritedSnapshot is violated.'
run_case InheritSnapshot 0 'Model checking completed. No error has been found.'
printf 'PASS: expected reset-to-all counterexample and corrected finite model.\n'
