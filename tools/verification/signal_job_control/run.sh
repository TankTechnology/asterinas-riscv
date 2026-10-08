#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 The Asterinas Authors.
set -euo pipefail

model_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$model_dir/../../.." && pwd)
cache_dir="$repo_dir/target/signal-job-control-model"
jar="$cache_dir/tla2tools-1.7.4.jar"
sha1=bee4a54f3ee3d4afc347c3240ec2d9e93b075104
sha256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
mkdir -p -- "$cache_dir"

if [[ ! -f "$jar" ]]; then
    download_file=$(mktemp "$cache_dir/download.XXXXXXXX")
    trap 'rm -f -- "$download_file"' EXIT
    curl --fail --location --retry 2 --connect-timeout 15 --max-time 120 \
        --output "$download_file" \
        https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
    printf '%s  %s\n' "$sha1" "$download_file" | sha1sum --check --status
    printf '%s  %s\n' "$sha256" "$download_file" | sha256sum --check --status
    mv -- "$download_file" "$jar"
    trap - EXIT
fi
printf '%s  %s\n' "$sha1" "$jar" | sha1sum --check --status
printf '%s  %s\n' "$sha256" "$jar" | sha256sum --check --status

run_dir=$(mktemp -d "$cache_dir/run.XXXXXXXX")
printf 'Artifacts: %s\n' "$run_dir"
java -version > "$run_dir/java-version.txt" 2>&1
printf 'case\texit\tstates\n' > "$run_dir/results.tsv"

run_case() {
    local config=$1 module=$2 expected_status=$3 expected_message=$4
    local status=0 log="$run_dir/$1.log"
    timeout --kill-after=5s 120s java -Xmx768m -XX:+UseParallelGC \
        -cp "$jar" tlc2.TLC -workers 1 -seed 1 -fp 0 \
        -metadir "$run_dir/$config-states" \
        -config "$model_dir/$config.cfg" "$model_dir/$module.tla" \
        > "$log" 2>&1 || status=$?
    if [[ $status -ne $expected_status ]] || ! rg -q "$expected_message" "$log"; then
        printf 'Unexpected result for %s (exit %s):\n' "$config" "$status" >&2
        tail -n 50 "$log" >&2
        return 1
    fi
    # Require actual exploration; parse/syntax errors never count as success.
    local counts
    counts=$(rg '^[1-9][0-9,]* states generated, [1-9][0-9,]* distinct states found' "$log")
    printf '%s\t%s\t%s\n' "$config" "$status" "$counts" | tee -a "$run_dir/results.tsv"
    if [[ $expected_status -ne 0 ]]; then
        sed -n '/^State 1:/,$p' "$log" > "$run_dir/$config.trace.txt"
        test -s "$run_dir/$config.trace.txt"
    fi
}

# Demonstrate sensitivity before accepting the corrected protocols.
run_case NoPendingCancellation JobControl 12 'Invariant PendingMatchesLatestGeneration is violated'
run_case NoSelectedRevocation JobControl 12 'Invariant NoStaleStopCommit is violated'
run_case NoRememberedWake WaitWake 12 'Invariant NoLostWake is violated'
run_case NoRememberedWakeLiveness WaitWake 13 'Temporal properties were violated'
run_case JobControl JobControl 0 'Model checking completed. No error has been found'
run_case WaitWake WaitWake 0 'Model checking completed. No error has been found'
run_case PrematureGroupReport GroupStop 12 'Invariant NoPrematureReport is violated'
run_case MissingJoinEnrollment GroupStop 12 'Invariant EveryMemberEnrolled is violated'
run_case DoubleExitDecrement GroupStop 12 'Invariant OutstandingMatchesPending is violated'
run_case StaleGroupAck GroupStop 12 'Invariant NoStaleAcknowledgment is violated'
run_case PostAckUserspace GroupStop 12 'Invariant NoUserEscape is violated'
run_case GroupStop GroupStop 0 'Model checking completed. No error has been found'
run_case ResetFutexDeadline FutexRestart 12 'Invariant DeadlinePreserved is violated'
run_case LostFutexWake FutexRestart 12 'Invariant NoLostSuccessfulWake is violated'
run_case SpuriousFutexSuccess FutexRestart 12 'Invariant NoInventedSuccessfulWake is violated'
run_case FutexRestart FutexRestart 0 'Model checking completed. No error has been found'

run_case StalePtraceResume PtraceGroupStop 12 'Invariant NoLostGroupObligation is violated'
run_case SigcontReleasesPtrace PtraceGroupStop 12 'Invariant PtraceStopOwnedByTracer is violated'
run_case DetachLosesGroupPark PtraceGroupStop 12 'Invariant NoLostGroupObligation is violated'
run_case PtraceGroupStop PtraceGroupStop 0 'Model checking completed. No error has been found'
run_case CrossConsumeStop WaitReports 12 'Invariant StopSlotsIndependent is violated'
run_case SplitContinue WaitReports 12 'Invariant SingleContinueConsumer is violated'
run_case ConsumeWaitPeek WaitReports 12 'Invariant (StopSlotsIndependent|ContinueSlotPreserved) is violated'
run_case WaitReports WaitReports 0 'Model checking completed. No error has been found'
run_case UnblockWaitedSignal SigwaitPending 12 'Invariant WaitedSignalPreserved is violated'
run_case SigwaitPending SigwaitPending 0 'Model checking completed. No error has been found'
run_case SplitDispositionCheck ChildNotification 12 'Invariant DispositionMatchesCommit is violated'
run_case SuppressWaitWake ChildNotification 12 'Invariant SuppressionPreservesWaitWake is violated'
run_case ChildNotification ChildNotification 0 'Model checking completed. No error has been found'
printf 'PASS: twenty-one expected negative controls and eight corrected finite models.\n'
