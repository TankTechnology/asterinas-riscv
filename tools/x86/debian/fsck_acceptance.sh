#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Boot a Debian image, power it off from inside the guest, and check that the
# image is still consistent.
#
# This is the storage acceptance gate: a filesystem is only trustworthy if a
# clean shutdown leaves an image that `e2fsck -fn` accepts. Every run starts
# from a fresh copy of the base image, so a failure is reproduced from the same
# state and cannot be masked by the previous run.
#
# The probe image differs from a normal boot in one way: the evidence service
# is disabled. It is an ABI gate that powers off on the first missing feature
# and would end the run before the shutdown path is exercised.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fsck_acceptance.sh --kernel ELF --initramfs CPIO --base-root IMG
                          --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy for each run (required).
  --workspace DIR     Directory for per-run images and transcripts (required).
  --runs N            Number of runs (default: 1).
  --timeout SECONDS   Per-run boot deadline (default: 1800).
  --loglevel N        Kernel log level (default: 5, which shows warnings).
  --settle SECONDS    Guest time before the probe syncs (default: 25).
  --qemu PATH         QEMU binary (default: qemu-system-x86_64).
  --crash-after SEC   Instead of a clean shutdown, SIGKILL QEMU after SEC
                      seconds and require journal recovery to restore a
                      consistent image without structural repairs.
  --fault-stage N     Halt the kernel at ext2 journal commit stage N (1: after
                      the commit record; 2: halfway through the home blocks;
                      3: after the home blocks, before the checkpoint), then
                      kill QEMU and require a clean recovery.

Exit status is 0 only if every run's image passes `e2fsck -fn`.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
INITRAMFS=""
BASE_ROOT=""
WORKSPACE=""
RUNS=1
TIMEOUT=1800
LOGLEVEL=5
SETTLE=25
QEMU="qemu-system-x86_64"
CRASH_AFTER=0
FAULT_STAGE=0

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --runs | \
            --timeout | --loglevel | --settle | --qemu | --crash-after | --fault-stage)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --runs) RUNS="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --settle) SETTLE="$2" ;;
                --qemu) QEMU="$2" ;;
                --crash-after) CRASH_AFTER="$2" ;;
                --fault-stage) FAULT_STAGE="$2" ;;
            esac
            shift 2
            ;;
        *)
            die_usage "unknown argument: $1"
            ;;
    esac
done

[[ -n "$KERNEL" && -n "$INITRAMFS" && -n "$BASE_ROOT" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --initramfs, --base-root and --workspace are required"
[[ "$RUNS" =~ ^[1-9][0-9]*$ ]] || die_usage "--runs must be a positive integer"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$SETTLE" =~ ^[0-9]+$ ]] || die_usage "--settle must be a non-negative integer"
[[ "$CRASH_AFTER" =~ ^[0-9]+$ ]] || die_usage "--crash-after must be a non-negative integer"
[[ "$FAULT_STAGE" =~ ^[0-9]+$ ]] || die_usage "--fault-stage must be a non-negative integer"
(( FAULT_STAGE == 0 || CRASH_AFTER == 0 )) || die_usage "use either --crash-after or --fault-stage"
if (( FAULT_STAGE > 0 )); then
    CRASH_AFTER=1
fi
for path in "$KERNEL" "$INITRAMFS" "$BASE_ROOT" "$WORKSPACE"; do
    [[ "$path" != *$'\n'* && "$path" != *$'\r'* && "$path" != *' '* ]] ||
        die_usage "path must not contain whitespace or newlines: $path"
done
for tool in debugfs e2fsck "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done
[[ -f "$KERNEL" && -f "$INITRAMFS" && -f "$BASE_ROOT" ]] ||
    die_usage "an input path does not name a file"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
BOOT_SCRIPT="$REPO_ROOT/tools/x86/debian/boot_debian.sh"
[[ -x "$BOOT_SCRIPT" ]] || {
    printf 'error: boot script is missing: %s\n' "$BOOT_SCRIPT" >&2
    exit 1
}

mkdir -p -- "$WORKSPACE"

prepare_run() {
    local image="$1"
    local probe_script="$2"
    local probe_unit="$3"
    local probe_settle="$4"

    cat >"$probe_script" <<PROBE
#!/bin/sh
sleep $probe_settle
sync
echo "FSCK_PROBE_SYNC rc=\$?"
sleep 5
systemctl poweroff --no-block
PROBE

    rm -f -- "$image"
    cp --sparse=always -- "$BASE_ROOT" "$image"

    debugfs -w -f - "$image" >/dev/null 2>&1 <<DEBUG
write $probe_script /opt/fsck-probe.sh
sif /opt/fsck-probe.sh mode 0100755
sif /opt/fsck-probe.sh uid 0
sif /opt/fsck-probe.sh gid 0
write $probe_unit /etc/systemd/system/fsck-probe.service
symlink /etc/systemd/system/multi-user.target.wants/fsck-probe.service /etc/systemd/system/fsck-probe.service
rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service
rm /etc/systemd/system/asterinas-debian-ext4.service
DEBUG
}

main() {
    local probe_script="$WORKSPACE/fsck-probe.sh"
    local probe_unit="$WORKSPACE/fsck-probe.service"
    cat >"$probe_unit" <<'UNIT'
[Unit]
Description=Storage acceptance probe: settle, sync, power off
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fsck-probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

    local run
    local failures=0
    local probe_settle="$SETTLE"
    if (( CRASH_AFTER > 0 )); then
        # The probe must never power the guest off before the crash.
        probe_settle=600
    fi
    for ((run = 1; run <= RUNS; run++)); do
        local dir="$WORKSPACE/run-$run"
        local image="$dir/root.ext2"
        local transcript="$dir/transcript.txt"
        mkdir -p -- "$dir"

        prepare_run "$image" "$probe_script" "$probe_unit" "$probe_settle"
        if ! e2fsck -fn "$image" >"$dir/fsck-before.txt" 2>&1; then
            printf 'run %d: the prepared image is not clean; see %s\n' \
                "$run" "$dir/fsck-before.txt" >&2
            failures=$((failures + 1))
            continue
        fi

        if (( CRASH_AFTER > 0 )); then
            local extra=()
            if (( FAULT_STAGE > 0 )); then
                extra=(--append-extra "asterinas.ext4_fault_stage=$FAULT_STAGE")
            fi
            mapfile -t argv < <("$BOOT_SCRIPT" --print-argv \
                --kernel "$KERNEL" \
                --initramfs "$INITRAMFS" \
                --root-image "$image" \
                --output "$transcript" \
                --loglevel "$LOGLEVEL" \
                --qemu "$QEMU" \
                "${extra[@]}")
            "${argv[@]}" >"$transcript" 2>&1 &
            local qemu_pid=$!
            if (( FAULT_STAGE > 0 )); then
                local waited=0
                while (( waited < TIMEOUT )); do
                    grep -aq "fault injection at stage" "$transcript" && break
                    kill -0 "$qemu_pid" 2>/dev/null || break
                    sleep 2
                    waited=$((waited + 2))
                done
                if ! grep -aq "fault injection at stage" "$transcript"; then
                    printf 'run %d: fault stage %d was never reached; see %s\n' \
                        "$run" "$FAULT_STAGE" "$transcript" >&2
                    kill -9 "$qemu_pid" 2>/dev/null || true
                    wait "$qemu_pid" 2>/dev/null || true
                    failures=$((failures + 1))
                    continue
                fi
            else
                sleep "$CRASH_AFTER"
            fi
            kill -9 "$qemu_pid" 2>/dev/null || true
            wait "$qemu_pid" 2>/dev/null || true

            # Recovery may repair advisory counters (free counts), but it must
            # not have to repair the allocation metadata itself.
            e2fsck -fn "$image" >"$dir/fsck-before-recovery.txt" 2>&1 || true
            e2fsck -fy "$image" >"$dir/fsck-recovery.txt" 2>&1 || true
            if grep -Eq "Inode bitmap differences|Block bitmap differences|Unattached inode|is corrupted|Multiply-claimed|Extended attribute.*invalid|i_blocks is" \
                "$dir/fsck-recovery.txt"; then
                printf 'run %d: FAIL (journal recovery needed structural repairs; see %s)\n' \
                    "$run" "$dir/fsck-recovery.txt"
                failures=$((failures + 1))
                continue
            fi
            if e2fsck -fn "$image" >"$dir/fsck-after.txt" 2>&1; then
                if (( FAULT_STAGE > 0 )); then
                    printf 'run %d: PASS (fault stage %d, recovery clean)\n' \
                        "$run" "$FAULT_STAGE"
                else
                    printf 'run %d: PASS (SIGKILL after %ss, recovery clean)\n' \
                        "$run" "$CRASH_AFTER"
                fi
            else
                printf 'run %d: FAIL (image is not clean after recovery; see %s)\n' \
                    "$run" "$dir/fsck-after.txt"
                failures=$((failures + 1))
            fi
            continue
        fi

        # The guest halts after systemd-shutdown prints "Powering off."; QEMU
        # does not necessarily exit at that point, so wait for the marker and
        # let the boot script stop QEMU.
        if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
            --kernel "$KERNEL" \
            --initramfs "$INITRAMFS" \
            --root-image "$image" \
            --output "$transcript" \
            --loglevel "$LOGLEVEL" \
            --qemu "$QEMU" \
            --wait-for "Powering off."; then
            printf 'run %d: the guest did not shut down cleanly; see %s\n' \
                "$run" "$transcript" >&2
            failures=$((failures + 1))
            continue
        fi

        if ! grep -aq "Powering off" "$transcript"; then
            printf 'run %d: the guest never reported reaching poweroff; see %s\n' \
                "$run" "$transcript" >&2
            failures=$((failures + 1))
            continue
        fi

        if e2fsck -fn "$image" >"$dir/fsck-after.txt" 2>&1; then
            printf 'run %d: PASS (e2fsck -fn is clean)\n' "$run"
        else
            printf 'run %d: FAIL (e2fsck -fn reported problems; see %s)\n' \
                "$run" "$dir/fsck-after.txt"
            failures=$((failures + 1))
        fi
    done

    if (( failures != 0 )); then
        printf '%d of %d run(s) failed\n' "$failures" "$RUNS" >&2
        return 1
    fi
    printf 'all %d run(s) passed\n' "$RUNS"
}

main
