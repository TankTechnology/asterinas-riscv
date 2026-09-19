#!/usr/bin/env bash
# Run the redundant-units A/B as an alternating series.
#
# Alternating rather than off-off/on-on: this machine's desktop-gate timings
# have run-to-run spread of well over a minute (the xorg->openbox gap alone has
# been observed between 34 s and 247 s across unmasked runs), so drift over the
# series is the main threat. Interleaving the arms makes drift hit both.
#
# Host-side I/O is sampled during each run, so a timing difference can be tied
# to a change in how much the guest asked the disk for, rather than only
# showing up as an unexplained number.
set -uo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly RESULTS="$ROOT/target/ab-series-results.txt"

: >"$RESULTS"
printf 'series started %s\n' "$(date +%H:%M:%S)" | tee -a "$RESULTS"

run_arm() {
    local state="$1" name="$2"
    printf '\n=== arm %s (%s) starting %s ===\n' "$state" "$name" "$(date +%H:%M:%S)" | tee -a "$RESULTS"

    "$(dirname "${BASH_SOURCE[0]}")/sample-qemu-io.sh" "/tmp/io-$name.txt" 10 &
    local sampler=$!

    # The timer prints its own line; capture it and keep it in the results file.
    "$(dirname "${BASH_SOURCE[0]}")/ab-redundant-units.sh" "$state" "$name" 2>&1 | tee -a "$RESULTS"

    kill "$sampler" 2>/dev/null
    wait "$sampler" 2>/dev/null

    printf 'arm %s (%s) finished %s\n' "$state" "$name" "$(date +%H:%M:%S)" | tee -a "$RESULTS"
    printf 'last io sample: %s\n' "$(tail -1 "/tmp/io-$name.txt" 2>/dev/null)" | tee -a "$RESULTS"
}

for spec in "off base-1" "on masked-3" "off base-2" "on masked-4"; do
    run_arm $spec
done

printf '\nseries complete %s\n' "$(date +%H:%M:%S)" | tee -a "$RESULTS"
