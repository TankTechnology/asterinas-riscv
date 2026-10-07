#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

mode=${1:?benchmark mode is required}
case "$mode" in
    hackbench)
        output=$(/nix/store/x20wh87sabrngbrx103454f5sf4f78lw-hackbench-riscv64-unknown-linux-gnu-0.92/bin/hackbench -g 8 -l 1000 -p -T)
        printf '%s\n' "$output"
        # hackbench reports `Time: seconds.milliseconds`; normalize to us.
        sample=$(printf '%s\n' "$output" | awk '/^Time:/ { split($2, a, "."); print a[1] * 1000000 + a[2] * 1000; exit }')
        ;;
    schbench)
        output=$(/nix/store/czasgjl1wx0jc4kx576n9gd156kp1wkw-schbench-riscv64-unknown-linux-gnu-v1.0/bin/schbench -F 256 -n 5 -r 10 -i 20)
        printf '%s\n' "$output"
        # schbench reports the highlighted tail as `* 99.0th: N`; use the
        # request-latency tail (the second highlighted line), not wakeup time.
        sample=$(printf '%s\n' "$output" | grep '99.0th' | tail -n 1 | awk '{ print $3 }')
        ;;
    *) echo "unsupported benchmark: $mode" >&2; exit 2 ;;
esac

if [ -n "$sample" ]; then
    printf 'PERF_SAMPLE=%s\n' "$sample"
elif [ "$mode" = schbench ]; then
    # The host runner extracts the request percentile from the retained log;
    # Asterinas' minimal guest shell cannot reliably parse this multi-line output.
    echo 'SCHBENCH_RESULT_DEFERRED_TO_RUNNER'
else
    echo "benchmark result not found" >&2
    exit 1
fi
