#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# fsync durability matrix.
#
# Each cell performs an operation, makes it durable with fsync, and then the
# host kills QEMU (a power cut).  After a normal recovery boot the operation
# must be visible in full: once fsync returned, the data, the directory entry
# and the truncation result must survive.
#
# This is the complement of fs_crash_matrix.sh: that one crashes *inside* the
# commit (the operation may be present or absent, but never half-applied);
# this one crashes *after* fsync returned (the operation must be present).

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_durability_matrix.sh --kernel ELF --initramfs CPIO --base-root IMG
                               --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy per cell (required).
  --workspace DIR     Per-cell images, manifests and transcripts.
  --scenarios LIST    Space-separated subset of:
                      write rename unlink truncate dir-entry
                      (default: all five).
  --settle SECONDS    Guest time before the probe starts (default: 10).
  --ready-timeout SECONDS
                      How long to wait for the READY marker (default: 300).
  --timeout SECONDS   Per-boot deadline (default: 900).
  --loglevel N        Kernel log level (default: 4).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Exit status is 0 only if every cell's fsynced operation survives the power
cut and `e2fsck -fn` accepts the recovered image.
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
SCENARIOS="write rename unlink truncate dir-entry"
SETTLE=10
READY_TIMEOUT=300
TIMEOUT=900
LOGLEVEL=4
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --scenarios | \
            --settle | --ready-timeout | --timeout | --loglevel | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --scenarios) SCENARIOS="$2" ;;
                --settle) SETTLE="$2" ;;
                --ready-timeout) READY_TIMEOUT="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --accel) ACCEL="$2" ;;
                --qemu) QEMU="$2" ;;
            esac
            shift 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$INITRAMFS" && -n "$BASE_ROOT" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --initramfs, --base-root and --workspace are required"
[[ "$ACCEL" == "tcg" || "$ACCEL" == "kvm" ]] ||
    die_usage "--accel must be 'tcg' or 'kvm'"
for tool in debugfs e2fsck "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done
for path in "$KERNEL" "$INITRAMFS" "$BASE_ROOT" "$WORKSPACE"; do
    [[ "$path" != *$'\n'* && "$path" != *$'\r'* && "$path" != *' '* ]] ||
        die_usage "path must not contain whitespace or newlines: $path"
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

prepare_scenario() {
    # prepare_scenario <scenario> <dir>
    local scenario="$1" dir="$2"

    case "$scenario" in
        write)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_DURABILITY_PROBE_BEGIN"
# Only the power-cut boot performs the operation.
grep -q 'fs_durability=powercut' /proc/cmdline || exit 0
sleep SETTLE
python3 - <<'PY'
import os
with open('/opt/fs-durable/write.bin', 'wb') as f:
    f.write(b'W' * 4096)
    f.flush()
    os.fsync(f.fileno())
print('FS_DURABILITY_READY')
PY
while true; do sleep 1; done
PROBE
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_DURABILITY_VERIFY_BEGIN"
# The power-cut boot must not verify (and power off) before the probe runs.
grep -q 'fs_durability=powercut' /proc/cmdline && exit 0
python3 - <<'PY'
import os
path = '/opt/fs-durable/write.bin'
ok = os.path.exists(path) and open(path, 'rb').read() == b'W' * 4096
open('/opt/fs-durable/result.txt', 'w').write('ok=%s\n' % ok)
print('FS_DURABILITY_RESULT ok=%s' % ok)
PY
echo "FS_DURABILITY_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        rename)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_DURABILITY_PROBE_BEGIN"
# Only the power-cut boot performs the operation.
grep -q 'fs_durability=powercut' /proc/cmdline || exit 0
sleep SETTLE
python3 - <<'PY'
import os
with open('/opt/fs-durable/rename-old.bin', 'wb') as f:
    f.write(b'R' * 4096)
    f.flush()
    os.fsync(f.fileno())
os.rename('/opt/fs-durable/rename-old.bin', '/opt/fs-durable/rename-new.bin')
fd = os.open('/opt/fs-durable', os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
print('FS_DURABILITY_READY')
PY
while true; do sleep 1; done
PROBE
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_DURABILITY_VERIFY_BEGIN"
# The power-cut boot must not verify (and power off) before the probe runs.
grep -q 'fs_durability=powercut' /proc/cmdline && exit 0
python3 - <<'PY'
import os
old = '/opt/fs-durable/rename-old.bin'
new = '/opt/fs-durable/rename-new.bin'
ok = (not os.path.exists(old)) and os.path.exists(new) and \
     open(new, 'rb').read() == b'R' * 4096
open('/opt/fs-durable/result.txt', 'w').write('ok=%s\n' % ok)
print('FS_DURABILITY_RESULT ok=%s' % ok)
PY
echo "FS_DURABILITY_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        unlink)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_DURABILITY_PROBE_BEGIN"
# Only the power-cut boot performs the operation.
grep -q 'fs_durability=powercut' /proc/cmdline || exit 0
sleep SETTLE
python3 - <<'PY'
import os
with open('/opt/fs-durable/unlink.bin', 'wb') as f:
    f.write(b'U' * 4096)
    f.flush()
    os.fsync(f.fileno())
os.unlink('/opt/fs-durable/unlink.bin')
fd = os.open('/opt/fs-durable', os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
print('FS_DURABILITY_READY')
PY
while true; do sleep 1; done
PROBE
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_DURABILITY_VERIFY_BEGIN"
# The power-cut boot must not verify (and power off) before the probe runs.
grep -q 'fs_durability=powercut' /proc/cmdline && exit 0
python3 - <<'PY'
import os
ok = not os.path.exists('/opt/fs-durable/unlink.bin')
open('/opt/fs-durable/result.txt', 'w').write('ok=%s\n' % ok)
print('FS_DURABILITY_RESULT ok=%s' % ok)
PY
echo "FS_DURABILITY_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        truncate)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_DURABILITY_PROBE_BEGIN"
# Only the power-cut boot performs the operation.
grep -q 'fs_durability=powercut' /proc/cmdline || exit 0
sleep SETTLE
python3 - <<'PY'
import os
with open('/opt/fs-durable/truncate.bin', 'wb') as f:
    f.write(b'T' * 65536)
    f.flush()
    os.fsync(f.fileno())
os.truncate('/opt/fs-durable/truncate.bin', 0)
fd = os.open('/opt/fs-durable', os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
print('FS_DURABILITY_READY')
PY
while true; do sleep 1; done
PROBE
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_DURABILITY_VERIFY_BEGIN"
# The power-cut boot must not verify (and power off) before the probe runs.
grep -q 'fs_durability=powercut' /proc/cmdline && exit 0
python3 - <<'PY'
import os
path = '/opt/fs-durable/truncate.bin'
ok = os.path.exists(path) and os.path.getsize(path) == 0
open('/opt/fs-durable/result.txt', 'w').write('ok=%s\n' % ok)
print('FS_DURABILITY_RESULT ok=%s' % ok)
PY
echo "FS_DURABILITY_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        dir-entry)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_DURABILITY_PROBE_BEGIN"
# Only the power-cut boot performs the operation.
grep -q 'fs_durability=powercut' /proc/cmdline || exit 0
sleep SETTLE
python3 - <<'PY'
import os
with open('/opt/fs-durable/dir-entry.bin', 'wb') as f:
    f.write(b'D' * 4096)
    f.flush()
    os.fsync(f.fileno())
fd = os.open('/opt/fs-durable', os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
print('FS_DURABILITY_READY')
PY
while true; do sleep 1; done
PROBE
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_DURABILITY_VERIFY_BEGIN"
# The power-cut boot must not verify (and power off) before the probe runs.
grep -q 'fs_durability=powercut' /proc/cmdline && exit 0
python3 - <<'PY'
import os
path = '/opt/fs-durable/dir-entry.bin'
ok = os.path.exists(path) and open(path, 'rb').read() == b'D' * 4096
open('/opt/fs-durable/result.txt', 'w').write('ok=%s\n' % ok)
print('FS_DURABILITY_RESULT ok=%s' % ok)
PY
echo "FS_DURABILITY_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        *) die_usage "unknown scenario: $scenario" ;;
    esac

    # The probe sleeps for the configured settle time before acting.
    sed -i "s/^sleep SETTLE$/sleep $SETTLE/" "$dir/probe.sh"

    cat >"$dir/probe.service" <<'UNIT'
[Unit]
Description=fsync durability probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-durable/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

    cat >"$dir/verify.service" <<'UNIT'
[Unit]
Description=fsync durability verify
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-durable/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT
}

run_cell() {
    # run_cell <scenario>
    local scenario="$1"
    local dir="$WORKSPACE/$scenario"
    local image="$dir/root.ext2"
    local transcript1="$dir/powercut.txt"
    local transcript2="$dir/recovery.txt"
    mkdir -p -- "$dir"

    prepare_scenario "$scenario" "$dir"

    rm -f -- "$image"
    cp --sparse=always -- "$BASE_ROOT" "$image"

    {
        printf 'mkdir /opt/fs-durable\n'
        printf 'write %s /opt/fs-durable/probe.sh\n' "$dir/probe.sh"
        printf 'sif /opt/fs-durable/probe.sh mode 0100755\n'
        printf 'write %s /opt/fs-durable/verify.sh\n' "$dir/verify.sh"
        printf 'sif /opt/fs-durable/verify.sh mode 0100755\n'
        printf 'write %s /etc/systemd/system/fs-durable-probe.service\n' "$dir/probe.service"
        printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-durable-probe.service /etc/systemd/system/fs-durable-probe.service\n'
        printf 'write %s /etc/systemd/system/fs-durable-verify.service\n' "$dir/verify.service"
        printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-durable-verify.service /etc/systemd/system/fs-durable-verify.service\n'
        printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
        printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
    } | debugfs -w -f - "$image" >/dev/null 2>&1

    if ! e2fsck -fn "$image" >"$dir/fsck-prepared.txt" 2>&1; then
        printf '%s: FAIL (prepared image is not clean)\n' "$scenario"
        return 1
    fi

    printf '%s: power-cut boot\n' "$scenario"
    mapfile -t argv < <("$BOOT_SCRIPT" --print-argv \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$image" \
        --output "$transcript1" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --append-extra "asterinas.fs_durability=powercut" \
        --wait-for "Powering off.")
    "${argv[@]}" >"$transcript1" 2>&1 &
    local qemu_pid=$!

    local waited=0
    while (( waited < READY_TIMEOUT )); do
        grep -aq "FS_DURABILITY_READY" "$transcript1" && break
        if ! kill -0 "$qemu_pid" 2>/dev/null; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if ! grep -aq "FS_DURABILITY_READY" "$transcript1"; then
        printf '%s: FAIL (the probe never reported READY; see %s)\n' \
            "$scenario" "$transcript1"
        kill -9 "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
        return 1
    fi

    # Give the guest a moment to leave the fsync path, then cut power.  The
    # operation was already reported durable, so nothing may be lost.
    sleep 2
    kill -9 "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true

    printf '%s: recovery boot\n' "$scenario"
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$image" \
        --output "$transcript2" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --wait-for "Powering off."; then
        printf '%s: FAIL (recovery boot did not shut down; see %s)\n' \
            "$scenario" "$transcript2"
        return 1
    fi

    if ! debugfs -R "dump /opt/fs-durable/result.txt $dir/result.txt" \
        "$image" >/dev/null 2>&1 || [[ ! -f "$dir/result.txt" ]]; then
        printf '%s: FAIL (no verification result; see %s)\n' "$scenario" "$transcript2"
        return 1
    fi

    if ! grep -q '^ok=True' "$dir/result.txt"; then
        printf '%s: FAIL (fsynced operation did not survive the power cut)\n' "$scenario"
        return 1
    fi

    if ! e2fsck -fn "$image" >"$dir/fsck-after.txt" 2>&1; then
        printf '%s: FAIL (e2fsck -fn; see %s)\n' "$scenario" "$dir/fsck-after.txt"
        return 1
    fi

    printf '%s: PASS\n' "$scenario"
    rm -f -- "$image"
    return 0
}

failures=0
cells=0
for scenario in $SCENARIOS; do
    cells=$((cells + 1))
    if ! run_cell "$scenario"; then
        failures=$((failures + 1))
    fi
done

if (( failures > 0 )); then
    printf '%d of %d cell(s) failed\n' "$failures" "$cells" >&2
    exit 1
fi
printf 'all %d cell(s) passed\n' "$cells"
