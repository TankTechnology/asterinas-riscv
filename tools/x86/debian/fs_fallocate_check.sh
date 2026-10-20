#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# fallocate semantics gate for the ext2/ext4 implementation.
#
# The write boot exercises, inside the guest:
#
#   * FALLOC_FL_ZERO_RANGE over an existing range (contents zeroed, size
#     unchanged),
#   * FALLOC_FL_ZERO_RANGE past EOF (file grows and the new range is zeros),
#   * FALLOC_FL_ZERO_RANGE|FALLOC_FL_KEEP_SIZE past EOF (size unchanged),
#   * plain allocation with and without KEEP_SIZE,
#   * FALLOC_FL_PUNCH_HOLE, which zeroes the requested range and keeps the
#     file size.
#
# The second boot remounts the image and re-checks the contents, so the zeros
# must have reached the disk rather than only the page cache.  The host then
# requires `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_fallocate_check.sh --kernel ELF --initramfs CPIO --base-root IMG
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

Exit status is 0 only if every range has the expected contents after a
remount and `e2fsck -fn` accepts the image.
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
IMAGE="$WORKSPACE/fallocate.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"
CONSTANTS="$WORKSPACE/constants.py"

cat >"$CONSTANTS" <<'CONSTANTS'
import ctypes
import os

ROOT = '/opt/fs-fallocate'
KEEP_SIZE = 0x01
PUNCH_HOLE = 0x02
ZERO_RANGE = 0x10

libc = ctypes.CDLL('libc.so.6', use_errno=True)
_fallocate = libc.fallocate
_fallocate.argtypes = [ctypes.c_int, ctypes.c_int,
                       ctypes.c_int64, ctypes.c_int64]
_fallocate.restype = ctypes.c_int


def fallocate(fd, mode, offset, length):
    ctypes.set_errno(0)
    rc = _fallocate(fd, mode, offset, length)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def write_pages(path, data):
    with open(path, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
CONSTANTS

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_fallocate=write' /proc/cmdline || exit 0
echo "FS_FALLOCATE_PROBE_BEGIN"
sleep $SETTLE
python3 - <<'PY'
import errno
import os
import sys

sys.path.insert(0, '/opt/fs-fallocate')
from constants import (KEEP_SIZE, PUNCH_HOLE, ROOT, ZERO_RANGE, fallocate,
                       write_pages)

os.makedirs(ROOT, exist_ok=True)

# ZERO_RANGE over an existing range.
p = os.path.join(ROOT, 'zero-basic.bin')
write_pages(p, b'A' * 8192)
fd = os.open(p, os.O_RDWR)
fallocate(fd, ZERO_RANGE, 4096, 4096)
os.fsync(fd)
os.close(fd)

# ZERO_RANGE past EOF grows the file and zeroes the new range.
p = os.path.join(ROOT, 'zero-grow.bin')
write_pages(p, b'G' * 4096)
fd = os.open(p, os.O_RDWR)
fallocate(fd, ZERO_RANGE, 4096, 4096)
os.fsync(fd)
os.close(fd)

# ZERO_RANGE|KEEP_SIZE past EOF must not change the size.
p = os.path.join(ROOT, 'zero-keep.bin')
write_pages(p, b'K' * 4096)
fd = os.open(p, os.O_RDWR)
fallocate(fd, ZERO_RANGE | KEEP_SIZE, 4096, 4096)
os.fsync(fd)
os.close(fd)

# Plain allocation, growing and keep-size.
p = os.path.join(ROOT, 'alloc-grow.bin')
fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
fallocate(fd, 0, 0, 4096)
os.fsync(fd)
os.close(fd)
p = os.path.join(ROOT, 'alloc-keep.bin')
fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
fallocate(fd, KEEP_SIZE, 0, 8192)
os.fsync(fd)
os.close(fd)
keep_size = os.stat(p).st_size
keep_blocks = os.stat(p).st_blocks

# Punch-hole zeroes the range and keeps the size.
p = os.path.join(ROOT, 'punch.bin')
write_pages(p, b'H' * 8192)
fd = os.open(p, os.O_RDWR)
punch_errno = 0
try:
    fallocate(fd, PUNCH_HOLE | KEEP_SIZE, 0, 4096)
except OSError as exc:
    punch_errno = exc.errno
os.fsync(fd)
os.close(fd)

with open(os.path.join(ROOT, 'write-result.txt'), 'w') as f:
    f.write('zero_keep_size=%d\n' % os.stat(os.path.join(ROOT, 'zero-keep.bin')).st_size)
    f.write('alloc_keep_size=%d\n' % keep_size)
    f.write('alloc_keep_blocks=%d\n' % keep_blocks)
    f.write('punch_errno=%d\n' % punch_errno)
    f.flush()
    os.fsync(f.fileno())
print('FS_FALLOCATE_WRITE punch_errno=%d alloc_keep_size=%d' % (punch_errno, keep_size))
PY
python3 /opt/fs-fallocate/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.fs_fallocate=write' /proc/cmdline && exit 0
echo "FS_FALLOCATE_VERIFY_BEGIN"
python3 /opt/fs-fallocate/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$WORKSPACE/check.py" <<'CHECKS'
import os
import sys

sys.path.insert(0, '/opt/fs-fallocate')
from constants import ROOT


def read(name):
    with open(os.path.join(ROOT, name), 'rb') as f:
        return f.read()


def check(problems):
    data = read('zero-basic.bin')
    if len(data) != 8192:
        problems.append('zero-basic size %d' % len(data))
    elif data[:4096] != b'A' * 4096 or data[4096:] != b'\0' * 4096:
        problems.append('zero-basic contents are wrong')

    data = read('zero-grow.bin')
    if len(data) != 8192:
        problems.append('zero-grow size %d' % len(data))
    elif data[:4096] != b'G' * 4096 or data[4096:] != b'\0' * 4096:
        problems.append('zero-grow contents are wrong')

    data = read('zero-keep.bin')
    if len(data) != 4096:
        problems.append('zero-keep size %d' % len(data))
    elif data != b'K' * 4096:
        problems.append('zero-keep contents are wrong')

    data = read('alloc-grow.bin')
    if len(data) != 4096 or data != b'\0' * 4096:
        problems.append('alloc-grow should be 4096 zero bytes, got %d' % len(data))

    if os.path.getsize(os.path.join(ROOT, 'alloc-keep.bin')) != 0:
        problems.append('alloc-keep changed the file size')
    data = read('punch.bin')
    if len(data) != 8192:
        problems.append('punch-hole changed the size to %d' % len(data))
    elif data[:4096] != b'\0' * 4096 or data[4096:] != b'H' * 4096:
        problems.append('punch-hole did not zero the requested range')


def main():
    phase = sys.argv[1]
    with open(os.path.join(ROOT, 'write-result.txt')) as f:
        recorded = dict(line.strip().split('=') for line in f if '=' in line)
    problems = []
    check(problems)
    if recorded.get('zero_keep_size') != '4096':
        problems.append('write phase: ZERO_RANGE|KEEP_SIZE changed the size')
    if recorded.get('punch_errno') != str(0):
        problems.append('write phase: punch-hole errno %s' % recorded.get('punch_errno'))
    result = {'ok': not problems, 'phase': phase, 'problems': problems[:20],
              'recorded': recorded}
    import json
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        json.dump(result, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    print('FS_FALLOCATE_%s ok=%s problems=%d'
          % (phase.upper(), not problems, len(problems)))
    for problem in problems[:20]:
        print('FS_FALLOCATE_PROBLEM %s' % problem)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=fallocate semantics probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-fallocate/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=fallocate semantics verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-fallocate/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-fallocate\n'
    printf 'write %s /opt/fs-fallocate/constants.py\n' "$CONSTANTS"
    printf 'write %s /opt/fs-fallocate/check.py\n' "$WORKSPACE/check.py"
    printf 'write %s /opt/fs-fallocate/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-fallocate/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-fallocate/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-fallocate/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-fallocate-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-fallocate-probe.service /etc/systemd/system/fs-fallocate-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-fallocate-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-fallocate-verify.service /etc/systemd/system/fs-fallocate-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'fallocate: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_fallocate=write" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'FS_FALLOCATE_WRITE punch_errno=0' "$WORKSPACE/write.txt"; then
    printf 'FAIL (the write phase did not complete); see %s\n' "$WORKSPACE/write.txt"
    exit 1
fi

printf 'fallocate: verify boot (fresh mount)\n'
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

if ! grep -aq 'FS_FALLOCATE_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (fallocate contents after remount); see %s\n' "$WORKSPACE/verify.txt"
    grep -a 'FS_FALLOCATE_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

printf 'PASS (ZERO_RANGE, allocation and punch-hole semantics survived a remount)\n'
rm -f -- "$IMAGE"
