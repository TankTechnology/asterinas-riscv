#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# FALLOC_FL_PUNCH_HOLE gate for the ext2/ext4 implementation.
#
# New files use the legacy indirect mapping, which is what this gate
# exercises:
#
#   * a hole inside the direct blocks,
#   * a hole crossing the direct -> single-indirect boundary,
#   * a hole reaching EOF (the size must not change),
#   * a punch covering a whole double-indirect file, which must free every
#     data block and both indirect levels (`i_blocks` drops to zero),
#   * a misaligned offset (EINVAL),
#   * a punch on an extent-mapped image file (EOPNOTSUPP for now).
#
# The second boot re-reads every file and requires the holes to read as
# zeros and the untouched blocks to keep their patterns; the host then runs
# `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_punch_hole_check.sh --kernel ELF --initramfs CPIO --base-root IMG
                              --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy per run (required).
  --workspace DIR     Image, scripts and transcripts.
  --settle SECONDS    Guest time before the probe runs (default: 10).
  --timeout SECONDS   Per-boot deadline (default: 900).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Exit status is 0 only if the holes and the untouched blocks are correct
after a remount and `e2fsck -fn` accepts the image.
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
SETTLE=10
TIMEOUT=900
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
IMAGE="$WORKSPACE/punch.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"
CHECK_PY="$WORKSPACE/check.py"

cat >"$CHECK_PY" <<'CHECKS'
import json
import os
import sys

ROOT = '/opt/fs-punch'
BLOCK = 4096
DATA_BLOCKS = 64
PRUNE_BLOCKS = 3000
DATA_HOLES = [(8, 20), (60, 64)]
PRUNE_HOLES = [(0, 3000)]


def check_file(path, blocks, holes):
    problems = []
    if os.path.getsize(path) != blocks * BLOCK:
        problems.append('%s: size %d' % (path, os.path.getsize(path)))
        return problems
    with open(path, 'rb') as f:
        data = f.read()
    zero = b'\0' * BLOCK
    for index in range(blocks):
        chunk = data[index * BLOCK:(index + 1) * BLOCK]
        in_hole = any(start <= index < end for start, end in holes)
        if in_hole:
            if chunk != zero:
                problems.append('%s: block %d in the hole is not zeroed' % (path, index))
        elif chunk != bytes([index & 0xff]) * BLOCK:
            problems.append('%s: block %d lost its contents' % (path, index))
    return problems


def main():
    phase = sys.argv[1]
    problems = []
    problems += check_file(os.path.join(ROOT, 'data.bin'), DATA_BLOCKS, DATA_HOLES)
    problems += check_file(os.path.join(ROOT, 'prune.bin'), PRUNE_BLOCKS, PRUNE_HOLES)
    with open(os.path.join(ROOT, 'write-result.txt')) as f:
        recorded = json.load(f)
    if recorded.get('misaligned_errno') != 22:  # EINVAL
        problems.append('misaligned punch errno %s' % recorded.get('misaligned_errno'))
    if recorded.get('extent_rc') != 0 or recorded.get('extent_errno') != 0:
        problems.append('extent punch failed: rc=%s errno=%s'
                        % (recorded.get('extent_rc'), recorded.get('extent_errno')))
    if recorded.get('depth1_errno') != 95:  # EOPNOTSUPP
        problems.append('depth-one punch errno %s' % recorded.get('depth1_errno'))

    hostname = '/etc/hostname'
    original_len = recorded.get('hostname_len', 0)
    if os.path.getsize(hostname) != 12288:
        problems.append('hostname size %d' % os.path.getsize(hostname))
    else:
        with open(hostname, 'rb') as f:
            data = f.read()
        import hashlib
        if hashlib.sha256(data[:original_len]).hexdigest() != recorded.get('hostname_sha'):
            problems.append('hostname prefix was punched away')
        if data[4096:8192] != b'\0' * 4096:
            problems.append('extent hole is not zeroed')
        if data[8192:12288] != b'\0' * 4096:
            problems.append('punched extent tail is not zeroed')
    if recorded.get('blocks_after', 0) >= recorded.get('blocks_before', 0):
        problems.append('punch did not free blocks (%s -> %s)'
                        % (recorded.get('blocks_before'), recorded.get('blocks_after')))
    prune_blocks = os.stat(os.path.join(ROOT, 'prune.bin')).st_blocks
    if prune_blocks != 0:
        problems.append('prune.bin still owns %d blocks' % prune_blocks)

    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('FS_PUNCH_%s %s' % (phase.upper(), result.strip()), flush=True)
    for problem in problems[:20]:
        print('FS_PUNCH_PROBLEM %s' % problem, flush=True)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_punch=run' /proc/cmdline || exit 0
echo "FS_PUNCH_PROBE_BEGIN"
sleep $SETTLE
python3 - <<'PY'
import ctypes
import hashlib
import json
import os

ROOT = '/opt/fs-punch'
BLOCK = 4096
KEEP_SIZE = 0x01
PUNCH_HOLE = 0x02

libc = ctypes.CDLL('libc.so.6', use_errno=True)
_fallocate = libc.fallocate
_fallocate.argtypes = [ctypes.c_int, ctypes.c_int,
                       ctypes.c_int64, ctypes.c_int64]
_fallocate.restype = ctypes.c_int


def punch(fd, offset, length):
    ctypes.set_errno(0)
    rc = _fallocate(fd, PUNCH_HOLE | KEEP_SIZE, offset, length)
    return rc, ctypes.get_errno()


def write_file(path, blocks):
    with open(path, 'wb') as f:
        for index in range(blocks):
            f.write(bytes([index & 0xff]) * BLOCK)
        f.flush()
        os.fsync(f.fileno())


os.makedirs(ROOT, exist_ok=True)

# A hole inside the direct blocks, then one crossing into single-indirect.
data_path = os.path.join(ROOT, 'data.bin')
write_file(data_path, 64)
fd = os.open(data_path, os.O_RDWR)
blocks_before = os.fstat(fd).st_blocks
rc, err = punch(fd, 8 * BLOCK, 8 * BLOCK)
if rc != 0:
    raise OSError(err, 'punch inside direct blocks failed')
rc, err = punch(fd, 10 * BLOCK, 10 * BLOCK)
if rc != 0:
    raise OSError(err, 'punch across the indirect boundary failed')
rc, err = punch(fd, 60 * BLOCK, 4 * BLOCK)
if rc != 0:
    raise OSError(err, 'punch up to EOF failed')
os.fsync(fd)
blocks_after = os.fstat(fd).st_blocks
os.close(fd)

# Misaligned offset must be rejected.
fd = os.open(data_path, os.O_RDWR)
misaligned_rc, misaligned_errno = punch(fd, 100, BLOCK)
os.close(fd)

# An extent-mapped file: append two pages (a multi-block extent), then punch
# the first page to split it and the second to remove the remainder.  The
# original first block (the hostname itself) stays untouched.
hostname = '/etc/hostname'
with open(hostname, 'rb') as f:
    hostname_original = f.read()
fd = os.open(hostname, os.O_RDWR)
os.lseek(fd, 4096, os.SEEK_SET)
os.write(fd, b'H' * 4096)
os.write(fd, b'I' * 4096)
os.fsync(fd)
extent_rc, extent_errno = punch(fd, 4096, BLOCK)
if extent_rc == 0:
    extent_rc, extent_errno = punch(fd, 8192, BLOCK)
os.fsync(fd)
os.close(fd)

# A depth-one extent tree is still unsupported and must fail cleanly.
depth1_fd = os.open('/usr/lib/x86_64-linux-gnu/libLLVM.so.19.1', os.O_RDWR)
depth1_rc, depth1_errno = punch(depth1_fd, 0, BLOCK)
os.close(depth1_fd)

# Punching the whole file must free every data block and both indirect
# levels: the inode ends with no blocks at all.
prune_path = os.path.join(ROOT, 'prune.bin')
write_file(prune_path, 3000)
fd = os.open(prune_path, os.O_RDWR)
rc, err = punch(fd, 0, 3000 * BLOCK)
if rc != 0:
    raise OSError(err, 'whole-file punch failed')
os.fsync(fd)
os.close(fd)

result = {
    'blocks_before': blocks_before,
    'blocks_after': blocks_after,
    'misaligned_errno': misaligned_errno,
    'misaligned_rc': misaligned_rc,
    'extent_errno': extent_errno,
    'extent_rc': extent_rc,
    'depth1_errno': depth1_errno,
    'depth1_rc': depth1_rc,
    'hostname_len': len(hostname_original),
    'hostname_sha': hashlib.sha256(hostname_original).hexdigest(),
}
with open(os.path.join(ROOT, 'write-result.txt'), 'w') as f:
    json.dump(result, f, sort_keys=True)
    f.flush()
    os.fsync(f.fileno())
print('FS_PUNCH_WRITE ok=True blocks=%d->%d misaligned=%d extent=%d depth1=%d'
      % (blocks_before, blocks_after, misaligned_errno, extent_errno, depth1_errno),
      flush=True)
PY
python3 /opt/fs-punch/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.fs_punch=run' /proc/cmdline && exit 0
echo "FS_PUNCH_VERIFY_BEGIN"
python3 /opt/fs-punch/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=fallocate punch-hole probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-punch/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=fallocate punch-hole verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-punch/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-punch\n'
    printf 'write %s /opt/fs-punch/check.py\n' "$CHECK_PY"
    printf 'write %s /opt/fs-punch/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-punch/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-punch/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-punch/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-punch-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-punch-probe.service /etc/systemd/system/fs-punch-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-punch-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-punch-verify.service /etc/systemd/system/fs-punch-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'punch-hole: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_punch=run" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'FS_PUNCH_WRITE ok=True' "$WORKSPACE/write.txt"; then
    printf 'FAIL (the write phase reported problems); see %s\n' "$WORKSPACE/write.txt"
    grep -a 'FS_PUNCH_PROBLEM' "$WORKSPACE/write.txt" | head -20 || true
    exit 1
fi

printf 'punch-hole: verify boot (fresh mount)\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/verify.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --wait-for "Powering off."; then
    printf 'FAIL (the verify boot did not shut down; see %s)\n' \
        "$WORKSPACE/verify.txt"
    exit 1
fi

if ! grep -aq 'FS_PUNCH_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (holes or contents after remount); see %s\n' "$WORKSPACE/verify.txt"
    grep -a 'FS_PUNCH_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

printf 'PASS (%s)\n' "$(grep -a 'FS_PUNCH_VERIFY ok=True' "$WORKSPACE/verify.txt" |
    tail -1 | sed 's/.*FS_PUNCH_VERIFY //')"
rm -f -- "$IMAGE"
