#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# ext4 depth-two extent tree gate.
#
# The host copies the Debian image, creates `/deep.bin`, allocates 5800
# blocks and punches every odd block. The resulting file owns 2900 singleton
# extents; with 340 records per leaf and four index entries in the inode, that
# no longer fits in a depth-one tree, so `debugfs` builds a depth-two tree.
#
# Boot 1 ("write") reads the punched holes, writes a pattern into every odd
# block (one insertion into a nearly full leaf each time, which splits leaves
# and grows the depth-one index block) and into four even blocks, then reads
# everything back.
#
# Boot 2 ("punch") punches a range that spans several leaves, truncates the
# file, writes into the resulting holes and verifies the survivors.
#
# After each boot the host requires the tree to keep depth two where the
# index cannot fit in the inode, to collapse to a shallower but valid shape
# once it can, and `e2fsck -fn` to accept the image.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_extent_depth_check.sh --kernel ELF --initramfs CPIO --base-root IMG
                                --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy per phase (required).
  --workspace DIR     Image, scripts and transcripts.
  --settle SECONDS    Guest time before the probe runs (default: 5).
  --timeout SECONDS   Per-boot deadline (default: 1800).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

The base image must not enable another gate's probe unit; a leftover oneshot
service may power the VM off while the probe is still running.

Exit status is 0 only if both phases verify their contents, the write phase
keeps a depth-two tree, the punch phase leaves a tree that cannot be collapsed
any further, and `e2fsck -fn` accepts the image after each boot.
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
SETTLE=5
TIMEOUT=1800
LOGLEVEL=4
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --settle | \
            --timeout | --loglevel | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --settle) SETTLE="$2" ;;
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
[[ "$SETTLE" =~ ^[0-9]+$ ]] || die_usage "--settle must be a non-negative integer"
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
IMAGE="$WORKSPACE/depth.ext2"
EMPTY="$WORKSPACE/empty.bin"
COMMANDS="$WORKSPACE/debugfs.cmds"
PROBE_PY="$WORKSPACE/probe.py"
PROBE_SH="$WORKSPACE/probe.sh"
PROBE_UNIT="$WORKSPACE/fs-depth.service"

BLOCKS=5800
FILE_BYTES=$((BLOCKS * 4096))

cat >"$PROBE_PY" <<'PY'
import ctypes
import os
import sys

BLOCK = 4096
BLOCKS = 5800
PATH = '/deep.bin'
PUNCH_HOLE = 0x02
KEEP_SIZE = 0x01
ZERO = b'\0' * BLOCK

libc = ctypes.CDLL('libc.so.6', use_errno=True)
_fallocate = libc.fallocate
_fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
_fallocate.restype = ctypes.c_int


def punch(fd, offset, length):
    ctypes.set_errno(0)
    rc = _fallocate(fd, PUNCH_HOLE | KEEP_SIZE, offset, length)
    return rc, ctypes.get_errno()


def read_block(file, index):
    file.seek(index * BLOCK)
    return file.read(BLOCK)


def expect(file, index, want, problems, label='block'):
    got = read_block(file, index)
    if got != want:
        problems.append('%s %d mismatch' % (label, index))


def write_phase(problems):
    if os.path.getsize(PATH) != BLOCKS * BLOCK:
        problems.append('size %d' % os.path.getsize(PATH))
        return
    with open(PATH, 'r+b') as file:
        # The odd blocks were punched on the host and must read as holes.
        for index in range(1, BLOCKS, 2):
            expect(file, index, ZERO, problems, 'hole')
        for index in range(1, BLOCKS, 2):
            file.seek(index * BLOCK)
            file.write(b'A' * BLOCK)
        for index in (2, 1000, 3000, 5000):
            file.seek(index * BLOCK)
            file.write(b'B' * BLOCK)
        file.flush()
        os.fsync(file.fileno())
        for index in range(1, BLOCKS, 2):
            expect(file, index, b'A' * BLOCK, problems)
        for index in (2, 1000, 3000, 5000):
            expect(file, index, b'B' * BLOCK, problems)
    print('FS_DEPTH_WRITE_DONE blocks=%d' % BLOCKS, flush=True)


def punch_phase(problems):
    with open(PATH, 'r+b') as file:
        expect(file, 1, b'A' * BLOCK, problems, 'survivor')
        expect(file, 2, b'B' * BLOCK, problems, 'survivor')
        expect(file, 5001, b'A' * BLOCK, problems, 'survivor')
        expect(file, 3000, b'B' * BLOCK, problems, 'survivor')

        rc, err = punch(file.fileno(), 1000 * BLOCK, (5000 - 1000) * BLOCK)
        if rc != 0:
            problems.append('punch failed errno=%d' % err)
            return
        os.fsync(file.fileno())
        expect(file, 1001, ZERO, problems, 'punched')
        expect(file, 3000, ZERO, problems, 'punched')
        expect(file, 5001, b'A' * BLOCK, problems, 'survivor')
        expect(file, 2, b'B' * BLOCK, problems, 'survivor')

        os.truncate(PATH, 2000 * BLOCK)
        if os.path.getsize(PATH) != 2000 * BLOCK:
            problems.append('truncated size %d' % os.path.getsize(PATH))
        expect(file, 1001, ZERO, problems, 'truncated')
        expect(file, 1999, ZERO, problems, 'truncated')

        file.seek(1500 * BLOCK)
        file.write(b'C' * BLOCK)
        file.seek(1998 * BLOCK)
        file.write(b'D' * BLOCK)
        file.flush()
        os.fsync(file.fileno())
        expect(file, 1500, b'C' * BLOCK, problems)
        expect(file, 1998, b'D' * BLOCK, problems)
    print('FS_DEPTH_PUNCH_DONE blocks=2000', flush=True)


def main():
    phase = None
    with open('/proc/cmdline') as file:
        for token in file.read().split():
            if token.startswith('asterinas.fs_depth='):
                phase = token.split('=', 1)[1]
    if phase not in ('write', 'punch'):
        print('FS_DEPTH_SKIP phase=%s' % phase, flush=True)
        return 0

    problems = []
    if phase == 'write':
        write_phase(problems)
    else:
        punch_phase(problems)

    result = 'ok=%s phase=%s problems=%d\n' % (not problems, phase, len(problems))
    path = '/opt/fs-depth/result-%s.txt' % phase
    with open(path, 'w') as file:
        file.write(result)
        file.flush()
        os.fsync(file.fileno())
    print('FS_DEPTH_%s %s' % (phase.upper(), result.strip()), flush=True)
    for problem in problems[:20]:
        print('FS_DEPTH_PROBLEM %s' % problem, flush=True)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
PY

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_depth=' /proc/cmdline || exit 0
echo "FS_DEPTH_PROBE_BEGIN"
sleep $SETTLE
python3 /opt/fs-depth/probe.py
sync
echo "FS_DEPTH_PROBE_DONE"
sync
systemctl poweroff --no-block
PROBE

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=ext4 depth-two extent gate
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-depth/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

: >"$EMPTY"
rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'write %s /deep.bin\n' "$EMPTY"
    printf 'fallocate /deep.bin 0 %d\n' "$((BLOCKS - 1))"
    for ((index = 1; index < BLOCKS; index += 2)); do
        printf 'punch /deep.bin %d %d\n' "$index" "$index"
    done
    printf 'set_inode_field /deep.bin size %d\n' "$FILE_BYTES"
    printf 'mkdir /opt/fs-depth\n'
    printf 'write %s /opt/fs-depth/probe.py\n' "$PROBE_PY"
    printf 'write %s /opt/fs-depth/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-depth/probe.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-depth.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-depth.service /etc/systemd/system/fs-depth.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} >"$COMMANDS"
debugfs -w -f "$COMMANDS" "$IMAGE" >/dev/null 2>&1

depth_two() {
    debugfs -R 'dump_extents /deep.bin' "$IMAGE" 2>/dev/null |
        grep -q '^ 0/ 2'
}

if ! depth_two; then
    printf 'FAIL (the prepared tree is not depth two; see dump_extents)\n'
    exit 1
fi
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' "$WORKSPACE"
    exit 1
fi

run_phase() {
    local phase="$1"
    local expect="$2"
    local transcript="$WORKSPACE/$phase.txt"
    printf 'depth-two: %s boot\n' "$phase"
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$transcript" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --append-extra "asterinas.fs_depth=$phase" \
        --wait-for "Powering off."; then
        printf 'FAIL (%s boot did not shut down; see %s)\n' "$phase" "$transcript"
        exit 1
    fi
    if ! debugfs -R "dump /opt/fs-depth/result-$phase.txt $WORKSPACE/result-$phase.txt" \
        "$IMAGE" >/dev/null 2>&1 || [[ ! -f "$WORKSPACE/result-$phase.txt" ]]; then
        printf 'FAIL (%s wrote no result; see %s)\n' "$phase" "$transcript"
        exit 1
    fi
    if ! grep -q '^ok=True' "$WORKSPACE/result-$phase.txt"; then
        printf 'FAIL (%s verification): %s\n' "$phase" \
            "$(cat "$WORKSPACE/result-$phase.txt")"
        exit 1
    fi
    if [[ "$expect" == depth2 ]] && ! depth_two; then
        printf 'FAIL (the tree left depth two after %s)\n' "$phase"
        debugfs -R 'dump_extents /deep.bin' "$IMAGE" 2>/dev/null |
            head -6 || true
        exit 1
    fi
    if [[ "$expect" == shallow ]] && grep -q '^ 0/ [3-9]' \
        <(debugfs -R 'dump_extents /deep.bin' "$IMAGE" 2>/dev/null); then
        printf 'FAIL (the tree got deeper after %s)\n' "$phase"
        exit 1
    fi
    if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-$phase.txt" 2>&1; then
        printf 'FAIL (%s left the image unclean; see %s/fsck-%s.txt)\n' \
            "$phase" "$WORKSPACE" "$phase"
        exit 1
    fi
    # "could be narrower" is e2fsck's packing hint for fragmented leaves; the
    # exit status above is the correctness signal.  A depth that can still be
    # collapsed is a real tree-shape problem and is rejected.
    if grep -q 'could be collapsed' "$WORKSPACE/fsck-$phase.txt"; then
        printf 'FAIL (%s left a tree that can still be collapsed; see %s/fsck-%s.txt)\n' \
            "$phase" "$WORKSPACE" "$phase"
        exit 1
    fi
    if grep -q 'could be narrower' "$WORKSPACE/fsck-$phase.txt"; then
        printf 'depth-two: %s: e2fsck suggests packing the fragmented leaves (informational)\n' \
            "$phase"
    fi
    printf 'depth-two: %s PASS (depth %s)\n' "$phase" \
        "$(debugfs -R 'dump_extents /deep.bin' "$IMAGE" 2>/dev/null |
            awk 'NR == 2 { print $1 $2 }')"
}

run_phase write depth2
run_phase punch shallow

printf 'PASS (depth-two reads, splits, punches, truncation, fsck clean)\n'
