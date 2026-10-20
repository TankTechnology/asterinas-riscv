#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Crash-consistency matrix for the ext2/ext4 implementation.
#
# Every cell boots one image twice:
#
#   1. The boot uses a deterministic probe as `init` (Stage1's
#      `--root-init-path`) and arms `asterinas.ext4_fault_stage=N`, so the
#      *first* metadata commit of the boot -- the probe's target operation --
#      is interrupted at journal stage N.  No systemd runs, so no unrelated
#      commit can consume the fault.
#   2. The same image boots normally.  The kernel must replay the journal,
#      Stage1 mounts the result, a verify unit checks the operation's
#      invariant and powers off.  The host then requires `e2fsck -fn`.
#
# Stages (see `kernel/core/src/fs/fs_impls/ext2/fs.rs`):
#   1 - after the journal commit record, before the home blocks
#   2 - halfway through the home blocks
#   3 - after the home blocks, before the checkpoint
#
# An atomic operation may be present or absent after recovery, but it must
# never be half-applied, and the image must need no structural repair.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_crash_matrix.sh --kernel ELF --initramfs CPIO --base-root IMG
                          --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs with --root-init-path support
                      (required).
  --base-root PATH    Debian ext2/ext4 image to copy per cell (required).
  --workspace DIR     Per-cell images, manifests and transcripts.
  --scenarios LIST    Space-separated subset of: create unlink rename truncate
                      (default: all four).
  --stages LIST       Space-separated journal stages (default: 1 2 3).
  --timeout SECONDS   Per-boot deadline (default: 900).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Exit status is 0 only if every cell recovers to a consistent state.
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
SCENARIOS="create unlink rename truncate"
STAGES="1 2 3"
TIMEOUT=900
LOGLEVEL=4
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --scenarios | \
            --stages | --timeout | --loglevel | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --scenarios) SCENARIOS="$2" ;;
                --stages) STAGES="$2" ;;
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

# Writes the scenario's setup payloads, phase-1 probe and phase-2 verifier.
# The payloads live under /opt/fs-crash on the image and are created on the
# host, before the first boot, so no guest commit other than the target
# operation can trigger the fault.
prepare_scenario() {
    # prepare_scenario <scenario> <dir>
    local scenario="$1" dir="$2"
    local payload="$dir/payload"

    mkdir -p -- "$payload"
    local setup_file=""

    case "$scenario" in
        create) ;;
        unlink)
            python3 -c "open('$payload/unlink.bin','wb').write(b'U'*4096)"
            setup_file="unlink.bin"
            ;;
        rename)
            python3 -c "open('$payload/rename-old.bin','wb').write(b'R'*4096)"
            setup_file="rename-old.bin"
            ;;
        truncate)
            python3 -c "open('$payload/truncate.bin','wb').write(b'T'*65536)"
            setup_file="truncate.bin"
            ;;
        *) die_usage "unknown scenario: $scenario" ;;
    esac

    case "$scenario" in
        create)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_CRASH_PROBE_BEGIN"
python3 - <<'PY'
import os
P = '/opt/fs-crash'
with open(P + '/create.bin', 'wb') as f:
    f.write(b'C' * 4096)
    f.flush()
    os.fsync(f.fileno())
print('FS_CRASH_PROBE_DONE')
PY
while true; do sleep 1; done
PROBE
            ;;
        unlink)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_CRASH_PROBE_BEGIN"
python3 - <<'PY'
import os
P = '/opt/fs-crash'
os.unlink(P + '/unlink.bin')
fd = os.open(P, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
print('FS_CRASH_PROBE_DONE')
PY
while true; do sleep 1; done
PROBE
            ;;
        rename)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_CRASH_PROBE_BEGIN"
python3 - <<'PY'
import os
P = '/opt/fs-crash'
os.rename(P + '/rename-old.bin', P + '/rename-new.bin')
fd = os.open(P, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
print('FS_CRASH_PROBE_DONE')
PY
while true; do sleep 1; done
PROBE
            ;;
        truncate)
            cat >"$dir/probe.sh" <<'PROBE'
#!/bin/sh
echo "FS_CRASH_PROBE_BEGIN"
python3 - <<'PY'
import os
P = '/opt/fs-crash'
os.truncate(P + '/truncate.bin', 0)
fd = os.open(P, os.O_RDONLY | os.O_DIRECTORY)
os.fsync(fd)
os.close(fd)
print('FS_CRASH_PROBE_DONE')
PY
while true; do sleep 1; done
PROBE
            ;;
    esac

    case "$scenario" in
        create)
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_CRASH_VERIFY_BEGIN"
python3 - <<'PY'
import os
path = '/opt/fs-crash/create.bin'
if os.path.exists(path):
    data = open(path, 'rb').read()
    ok = data == b'C' * 4096
    detail = 'present len=%d' % len(data)
else:
    ok = True
    detail = 'absent'
open('/opt/fs-crash/result.txt', 'w').write('ok=%s %s\n' % (ok, detail))
print('FS_CRASH_RESULT ok=%s %s' % (ok, detail))
PY
echo "FS_CRASH_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        unlink)
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_CRASH_VERIFY_BEGIN"
python3 - <<'PY'
import os
path = '/opt/fs-crash/unlink.bin'
if os.path.exists(path):
    data = open(path, 'rb').read()
    ok = data == b'U' * 4096
    detail = 'present len=%d' % len(data)
else:
    ok = True
    detail = 'absent'
open('/opt/fs-crash/result.txt', 'w').write('ok=%s %s\n' % (ok, detail))
print('FS_CRASH_RESULT ok=%s %s' % (ok, detail))
PY
echo "FS_CRASH_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        rename)
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_CRASH_VERIFY_BEGIN"
python3 - <<'PY'
import os
old = '/opt/fs-crash/rename-old.bin'
new = '/opt/fs-crash/rename-new.bin'
old_exists = os.path.exists(old)
new_exists = os.path.exists(new)
ok = old_exists != new_exists
if ok:
    path = old if old_exists else new
    data = open(path, 'rb').read()
    ok = data == b'R' * 4096
    detail = ('old' if old_exists else 'new') + ' len=%d' % len(data)
else:
    detail = 'old=%s new=%s' % (old_exists, new_exists)
open('/opt/fs-crash/result.txt', 'w').write('ok=%s %s\n' % (ok, detail))
print('FS_CRASH_RESULT ok=%s %s' % (ok, detail))
PY
echo "FS_CRASH_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
        truncate)
            cat >"$dir/verify.sh" <<'VERIFY'
#!/bin/sh
echo "FS_CRASH_VERIFY_BEGIN"
python3 - <<'PY'
import os
path = '/opt/fs-crash/truncate.bin'
if not os.path.exists(path):
    ok = False
    detail = 'missing'
else:
    size = os.path.getsize(path)
    if size == 0:
        ok = True
        detail = 'size=0'
    elif size == 65536:
        data = open(path, 'rb').read()
        ok = data == b'T' * 65536
        detail = 'size=65536 intact=%s' % ok
    else:
        ok = False
        detail = 'size=%d' % size
open('/opt/fs-crash/result.txt', 'w').write('ok=%s %s\n' % (ok, detail))
print('FS_CRASH_RESULT ok=%s %s' % (ok, detail))
PY
echo "FS_CRASH_VERIFY_DONE"
sync
systemctl poweroff --no-block
VERIFY
            ;;
    esac

    cat >"$dir/verify.service" <<'UNIT'
[Unit]
Description=Fs crash matrix verify
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-crash/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

    SETUP_FILE="$setup_file"
}

run_cell() {
    # run_cell <scenario> <stage>
    local scenario="$1" stage="$2"
    local dir="$WORKSPACE/$scenario-stage-$stage"
    local image="$dir/root.ext2"
    local transcript1="$dir/boot1.txt"
    local transcript2="$dir/boot2.txt"
    mkdir -p -- "$dir"

    prepare_scenario "$scenario" "$dir"

    rm -f -- "$image"
    cp --sparse=always -- "$BASE_ROOT" "$image"

    # Inject the payload, the probe and the pre-installed verifier before the
    # first boot.  Nothing is written to the image between the crash and the
    # recovery boot, so the kernel's replay is what the verifier observes.
    {
        printf 'mkdir /opt/fs-crash\n'
        if [[ -n "$SETUP_FILE" ]]; then
            printf 'write %s /opt/fs-crash/%s\n' "$dir/payload/$SETUP_FILE" "$SETUP_FILE"
        fi
        printf 'write %s /opt/fs-crash/probe.sh\n' "$dir/probe.sh"
        printf 'sif /opt/fs-crash/probe.sh mode 0100755\n'
        printf 'write %s /opt/fs-crash/verify.sh\n' "$dir/verify.sh"
        printf 'sif /opt/fs-crash/verify.sh mode 0100755\n'
        printf 'write %s /etc/systemd/system/fs-crash-verify.service\n' "$dir/verify.service"
        printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-crash-verify.service /etc/systemd/system/fs-crash-verify.service\n'
        printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
        printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
    } | debugfs -w -f - "$image" >/dev/null 2>&1

    if ! e2fsck -fn "$image" >"$dir/fsck-prepared.txt" 2>&1; then
        printf '%s/stage %s: FAIL (prepared image is not clean)\n' "$scenario" "$stage"
        return 1
    fi

    printf '%s/stage %s: crash boot\n' "$scenario" "$stage"
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$image" \
        --output "$transcript1" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --root-init-path /opt/fs-crash/probe.sh \
        --append-extra "asterinas.ext4_fault_stage=$stage" \
        --wait-for "fault injection at stage"; then
        printf '%s/stage %s: FAIL (fault stage was not reached; see %s)\n' \
            "$scenario" "$stage" "$transcript1"
        return 1
    fi

    printf '%s/stage %s: recovery boot\n' "$scenario" "$stage"
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$image" \
        --output "$transcript2" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --wait-for "Powering off."; then
        printf '%s/stage %s: FAIL (recovery boot did not shut down; see %s)\n' \
            "$scenario" "$stage" "$transcript2"
        return 1
    fi

    if ! debugfs -R "dump /opt/fs-crash/result.txt $dir/result.txt" \
        "$image" >/dev/null 2>&1 || [[ ! -f "$dir/result.txt" ]]; then
        printf '%s/stage %s: FAIL (verifier wrote no result; see %s)\n' \
            "$scenario" "$stage" "$transcript2"
        return 1
    fi

    if ! grep -q '^ok=True' "$dir/result.txt"; then
        printf '%s/stage %s: FAIL (invariant): %s\n' \
            "$scenario" "$stage" "$(cat "$dir/result.txt")"
        return 1
    fi

    if ! e2fsck -fn "$image" >"$dir/fsck-after.txt" 2>&1; then
        printf '%s/stage %s: FAIL (e2fsck -fn; see %s)\n' \
            "$scenario" "$stage" "$dir/fsck-after.txt"
        return 1
    fi

    printf '%s/stage %s: PASS (%s)\n' "$scenario" "$stage" "$(cat "$dir/result.txt")"
    # The transcripts and fsck reports are the evidence; the multi-gigabyte
    # image is only needed to debug a failure.
    rm -f -- "$image"
    return 0
}

failures=0
cells=0
for stage in $STAGES; do
    for scenario in $SCENARIOS; do
        cells=$((cells + 1))
        if ! run_cell "$scenario" "$stage"; then
            failures=$((failures + 1))
        fi
    done
done

if (( failures > 0 )); then
    printf '%d of %d cell(s) failed\n' "$failures" "$cells" >&2
    exit 1
fi
printf 'all %d cell(s) passed\n' "$cells"
