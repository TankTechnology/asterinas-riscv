#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# metadata_csum gate.
#
# The Debian image is converted in place with `tune2fs -O
# metadata_csum,metadata_csum_seed`; `e2fsck -fyD` then gives every directory
# block the 12-byte checksum tail the feature requires. The write boot
# exercises every checksummed structure:
#
#   * files created, fsynced and deleted (inodes, bitmaps, group descriptors,
#     directory tails and their checksums);
#   * a 200-byte `user.*` attribute, which does not fit inline and lands in an
#     external xattr block;
#   * a 100-page append to the extent-mapped `/etc/hostname`, promoting it to a
#     depth-one tree with a checksummed leaf block.
#
# The verify boot re-reads everything after a remount. The host requires the
# feature words, a valid superblock checksum and `e2fsck -fn` after both boots
# (e2fsck verifies every checksum: superblock, descriptors, bitmaps, inodes,
# extent leaves, directory blocks and xattr blocks).

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_metadata_csum_check.sh --kernel ELF --initramfs CPIO --base-root IMG
                                 --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy per run (required).
  --workspace DIR     Images, scripts and transcripts.
  --settle SECONDS    Guest time before the probe runs (default: 10).
  --timeout SECONDS   Per-boot deadline (default: 900).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Exit status is 0 only if the workload survives a remount with every
checksum still valid.
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
for tool in debugfs dumpe2fs e2fsck python3 tune2fs "$QEMU"; do
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
IMAGE="$WORKSPACE/metadata-csum.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"
CHECK_PY="$WORKSPACE/check.py"
FEATURES_PY="$WORKSPACE/check_features.py"

cat >"$CHECK_PY" <<CHECKS
import os
import sys

ROOT = '/opt/mc'
BLOCK = 4096
APPENDS = 100
FILES = 120


def write_phase():
    os.makedirs(ROOT, exist_ok=True)
    with open(os.path.join(ROOT, 'write.bin'), 'wb') as f:
        for index in range(32):
            f.write(bytes([index & 0xFF]) * BLOCK)
        f.flush()
        os.fsync(f.fileno())
    # 200 bytes do not fit in the in-inode area, so this allocates an
    # external xattr block with its own checksum.
    os.setxattr(os.path.join(ROOT, 'write.bin'), 'user.big', b'a' * 200)
    sub = os.path.join(ROOT, 'sub')
    os.makedirs(sub, exist_ok=True)
    for index in range(FILES):
        with open(os.path.join(sub, 'f%03d' % index), 'wb') as f:
            f.write(b'x' * 64)
    for index in range(0, FILES, 2):
        os.unlink(os.path.join(sub, 'f%03d' % index))
    # /usr/bin is htree-indexed in the image. Creating an entry there forces
    # the directory to be converted back to linear blocks, which must lay the
    # entries out inside the checksum-tail limit.
    indexed = '/usr/bin/mc-probe-dir'
    os.makedirs(indexed, exist_ok=True)
    with open(os.path.join(indexed, 'note.txt'), 'w') as f:
        f.write('indexed\n')
        f.flush()
        os.fsync(f.fileno())
    os.unlink(os.path.join(indexed, 'note.txt'))
    os.rmdir(indexed)
    # Appending page by page promotes the extent tree to depth one, whose
    # leaf block carries a checksum.
    hostname = '/etc/hostname'
    fd = os.open(hostname, os.O_RDWR)
    for index in range(APPENDS):
        os.pwrite(fd, bytes([index & 0xFF]) * BLOCK, BLOCK + index * BLOCK)
    os.fsync(fd)
    os.close(fd)
    print('MC_WRITE_DONE', flush=True)
    return []


def verify_phase():
    problems = []
    path = os.path.join(ROOT, 'write.bin')
    with open(path, 'rb') as f:
        data = f.read()
    for index in range(32):
        if data[index * BLOCK:(index + 1) * BLOCK] != bytes([index & 0xFF]) * BLOCK:
            problems.append('write.bin block %d' % index)
    if os.getxattr(path, 'user.big') != b'a' * 200:
        problems.append('user.big xattr')
    for index in range(FILES):
        p = os.path.join(ROOT, 'sub', 'f%03d' % index)
        if index % 2 == 0:
            if os.path.exists(p):
                problems.append('f%03d still exists' % index)
        elif not os.path.exists(p):
            problems.append('f%03d missing' % index)
    for probe in ('/usr/bin/sh', '/usr/bin/dash'):
        if not os.path.exists(probe):
            problems.append('%s missing after the indexed-dir conversion' % probe)
    with open('/etc/hostname', 'rb') as f:
        data = f.read()
    if len(data) != BLOCK + APPENDS * BLOCK:
        problems.append('hostname size %d' % len(data))
    else:
        for index in range(APPENDS):
            if data[BLOCK + index * BLOCK:BLOCK + (index + 1) * BLOCK] != \
                    bytes([index & 0xFF]) * BLOCK:
                problems.append('hostname appended block %d' % index)
    return problems


def main():
    phase = sys.argv[1]
    problems = write_phase() if phase == 'write' else verify_phase()
    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('MC_%s %s' % (phase.upper(), result.strip()), flush=True)
    for problem in problems[:20]:
        print('MC_PROBLEM %s' % problem, flush=True)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$FEATURES_PY" <<'FEATURES'
"""Checks the metadata_csum encoding of an ext4 image from the host.

Usage: check_features.py IMAGE
"""

import struct
import sys

RO_COMPAT_METADATA_CSUM = 1 << 10
SUPERBLOCK_CHECKSUM_OFFSET = 0x3FC


def crc32c(crc, data):
    poly = 0x82F63B78
    value = crc & 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ poly if value & 1 else value >> 1
    return value


def check(image):
    problems = []
    with open(image, 'rb') as f:
        f.seek(1024)
        sb = f.read(1024)
    ro_compat = struct.unpack_from('<I', sb, 0x64)[0]
    incompat = struct.unpack_from('<I', sb, 0x60)[0]
    if not ro_compat & RO_COMPAT_METADATA_CSUM:
        problems.append('metadata_csum is not set')
    # s_csum_seed may legitimately be zero (tune2fs conversion).
    if not incompat & 0x2000:
        problems.append('csum_seed is not set')
    stored = struct.unpack_from('<I', sb, SUPERBLOCK_CHECKSUM_OFFSET)[0]
    computed = crc32c(0xFFFFFFFF, sb[:SUPERBLOCK_CHECKSUM_OFFSET])
    if stored != computed:
        problems.append(
            'superblock checksum %#x, expected %#x' % (stored, computed)
        )
    return problems


def main():
    problems = check(sys.argv[1])
    for problem in problems:
        print('FEATURES_PROBLEM %s: %s' % (sys.argv[1], problem))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
FEATURES

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.mc=run' /proc/cmdline || exit 0
echo "MC_PROBE_BEGIN"
sleep $SETTLE
python3 /opt/mc/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.mc=run' /proc/cmdline && exit 0
echo "MC_VERIFY_BEGIN"
python3 /opt/mc/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=metadata_csum probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/mc/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=metadata_csum verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/mc/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"

tune2fs -O metadata_csum,metadata_csum_seed "$IMAGE" \
    >"$WORKSPACE/tune2fs.txt" 2>&1 || {
    printf 'FAIL (tune2fs could not enable metadata_csum; see %s)\n' \
        "$WORKSPACE/tune2fs.txt"
    exit 1
}

# Converting an existing image leaves directories without the checksum tail;
# `-D` rebuilds them and adds the tails.
e2fsck -fyD "$IMAGE" >"$WORKSPACE/fsck-convert.txt" 2>&1 || true
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-converted.txt" 2>&1; then
    printf 'FAIL (the converted image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-converted.txt"
    exit 1
fi
if ! python3 "$FEATURES_PY" "$IMAGE" >"$WORKSPACE/features.txt" 2>&1; then
    printf 'FAIL (converted image features); see %s\n' "$WORKSPACE/features.txt"
    cat "$WORKSPACE/features.txt"
    exit 1
fi

{
    printf 'mkdir /opt/mc\n'
    printf 'write %s /opt/mc/check.py\n' "$CHECK_PY"
    printf 'write %s /opt/mc/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/mc/probe.sh mode 0100755\n'
    printf 'write %s /opt/mc/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/mc/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/mc-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/mc-probe.service /etc/systemd/system/mc-probe.service\n'
    printf 'write %s /etc/systemd/system/mc-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/mc-verify.service /etc/systemd/system/mc-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'metadata-csum: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.mc=run" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'MC_WRITE_DONE' "$WORKSPACE/write.txt"; then
    printf 'FAIL (the write workload did not finish); see %s\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-write.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn after the write boot; see %s)\n' \
        "$WORKSPACE/fsck-write.txt"
    head -20 "$WORKSPACE/fsck-write.txt" || true
    exit 1
fi
if ! python3 "$FEATURES_PY" "$IMAGE" >"$WORKSPACE/features-write.txt" 2>&1; then
    printf 'FAIL (superblock checksum after the write boot); see %s\n' \
        "$WORKSPACE/features-write.txt"
    cat "$WORKSPACE/features-write.txt"
    exit 1
fi

debugfs -R "dump_extents /etc/hostname" "$IMAGE" \
    >"$WORKSPACE/dump-extents.txt" 2>&1 || true
if ! grep -q '^ 0/ 1' "$WORKSPACE/dump-extents.txt"; then
    printf 'FAIL (the appended file is not a depth-one extent tree; see %s)\n' \
        "$WORKSPACE/dump-extents.txt"
    exit 1
fi
if ! debugfs -R "stat /opt/mc/write.bin" "$IMAGE" 2>/dev/null |
    grep -q 'File ACL: [1-9]'; then
    printf 'FAIL (the large xattr did not allocate an external block)\n'
    exit 1
fi

printf 'metadata-csum: verify boot (fresh mount)\n'
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

if ! grep -aq 'MC_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (contents after the remount); see %s\n' "$WORKSPACE/verify.txt"
    grep -a 'MC_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-verify.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn after the verify boot; see %s)\n' \
        "$WORKSPACE/fsck-verify.txt"
    head -20 "$WORKSPACE/fsck-verify.txt" || true
    exit 1
fi
if ! python3 "$FEATURES_PY" "$IMAGE" >"$WORKSPACE/features-verify.txt" 2>&1; then
    printf 'FAIL (superblock checksum after the verify boot); see %s\n' \
        "$WORKSPACE/features-verify.txt"
    cat "$WORKSPACE/features-verify.txt"
    exit 1
fi

printf 'PASS (%s)\n' "$(grep -a 'MC_VERIFY ok=True' "$WORKSPACE/verify.txt" |
    tail -1 | sed 's/.*MC_VERIFY //')"
rm -f -- "$IMAGE"
