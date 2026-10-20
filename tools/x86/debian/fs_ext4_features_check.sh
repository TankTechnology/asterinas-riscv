#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# ext4 feature gate for the 64BIT and FLEX_BG incompat flags.
#
# Two images are used:
#
#   * the Debian root image is converted in place to 64-bit descriptors
#     (`resize2fs -b`) and gets FLEX_BG enabled (`tune2fs -O flex_bg`). Its
#     metadata still lives inside each group, so this half of the gate checks
#     that every descriptor read/write uses the 64-byte stride and leaves the
#     high block-number words untouched.
#   * a fresh image is formatted with `mke2fs -O 64bit,flex_bg` and attached
#     as a second virtio-blk disk. `mke2fs` packs the bitmaps and inode table
#     of the flex group into the first group, so group 1's metadata lives
#     outside its own block range -- the layout that plain ext2 validation
#     rejects. The guest mounts it, writes, fsyncs and unmounts it.
#
# The host requires the `64bit` and `flex_bg` feature words, a descriptor size
# of 64, zero high block-number words in every descriptor before and after
# both boots, at least one cross-group metadata pointer in the fresh image,
# correct file contents after the remount, and `e2fsck -fn` on both images.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_ext4_features_check.sh --kernel ELF --initramfs CPIO --base-root IMG
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

Exit status is 0 only if the 64BIT/FLEX_BG layout stays consistent across
two boots and `e2fsck -fn` accepts both images.
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
for tool in debugfs dumpe2fs e2fsck mke2fs resize2fs tune2fs python3 "$QEMU"; do
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
IMAGE="$WORKSPACE/features-root.ext2"
DATA_IMAGE="$WORKSPACE/features-data.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"
CHECK_PY="$WORKSPACE/check.py"
FEATURES_PY="$WORKSPACE/check_features.py"

cat >"$CHECK_PY" <<'CHECKS'
import json
import os
import subprocess
import sys

ROOT = '/opt/feat-check'
MNT = '/mnt/feat'
DATA = '/dev/vdb'
BLOCK = 4096
FILES = 120
BIG_BLOCKS = 64
DISK_BLOCKS = 32
BIG_BYTE = 0xA5
DISK_BYTE = 0x5A


def write_blocks(path, count, byte):
    with open(path, 'wb') as f:
        for _ in range(count):
            f.write(bytes([byte]) * BLOCK)
        f.flush()
        os.fsync(f.fileno())


def file_pattern(count, byte):
    return bytes([byte]) * (count * BLOCK)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mount_data():
    os.makedirs(MNT, exist_ok=True)
    return subprocess.run(
        ['mount', '-t', 'ext4', DATA, MNT], capture_output=True
    ).returncode


def umount_data():
    return subprocess.run(['umount', MNT], capture_output=True).returncode


def setup():
    os.makedirs(ROOT, exist_ok=True)
    os.chmod(ROOT, 0o777)
    write_blocks(os.path.join(ROOT, 'big.bin'), BIG_BLOCKS, BIG_BYTE)
    for index in range(FILES):
        path = os.path.join(ROOT, 'f%03d' % index)
        with open(path, 'wb') as f:
            f.write(bytes([index & 0xFF]) * BLOCK)
            f.flush()
            os.fsync(f.fileno())
    # Removing half of the files forces bitmap and descriptor updates for
    # several groups before the directory is made durable.
    for index in range(0, FILES, 2):
        os.unlink(os.path.join(ROOT, 'f%03d' % index))
    fsync_dir(ROOT)


def observe():
    problems = []
    big = os.path.join(ROOT, 'big.bin')
    if not os.path.exists(big):
        problems.append('big.bin is missing')
    elif os.path.getsize(big) != BIG_BLOCKS * BLOCK:
        problems.append('big.bin has size %d' % os.path.getsize(big))
    else:
        with open(big, 'rb') as f:
            if f.read() != file_pattern(BIG_BLOCKS, BIG_BYTE):
                problems.append('big.bin lost its contents')
    for index in range(FILES):
        path = os.path.join(ROOT, 'f%03d' % index)
        if index % 2 == 0:
            if os.path.exists(path):
                problems.append('%s was not unlinked' % os.path.basename(path))
            continue
        if not os.path.exists(path):
            problems.append('%s is missing' % os.path.basename(path))
            continue
        with open(path, 'rb') as f:
            if f.read() != bytes([index & 0xFF]) * BLOCK:
                problems.append('%s lost its contents' % os.path.basename(path))
    return problems


def write_phase():
    problems = []
    setup()
    mount_rc = mount_data()
    if mount_rc != 0:
        problems.append('mount %s failed rc=%d' % (DATA, mount_rc))
        umount_rc = None
    else:
        write_blocks(os.path.join(MNT, 'disk.bin'), DISK_BLOCKS, DISK_BYTE)
        fsync_dir(MNT)
        umount_rc = umount_data()
        if umount_rc != 0:
            problems.append('umount %s failed rc=%d' % (MNT, umount_rc))
    with open(os.path.join(ROOT, 'write-result.json'), 'w') as f:
        json.dump({'mount_rc': mount_rc, 'umount_rc': umount_rc}, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    return problems


def verify_phase():
    problems = observe()
    mount_rc = mount_data()
    if mount_rc != 0:
        problems.append('remount %s failed rc=%d' % (DATA, mount_rc))
    else:
        path = os.path.join(MNT, 'disk.bin')
        if not os.path.exists(path):
            problems.append('disk.bin is missing from the second disk')
        elif os.path.getsize(path) != DISK_BLOCKS * BLOCK:
            problems.append('disk.bin has size %d' % os.path.getsize(path))
        else:
            with open(path, 'rb') as f:
                if f.read() != file_pattern(DISK_BLOCKS, DISK_BYTE):
                    problems.append('disk.bin lost its contents')
        if umount_data() != 0:
            problems.append('second umount failed')
    return problems


def main():
    phase = sys.argv[1]
    if phase == 'write':
        problems = write_phase()
    else:
        problems = verify_phase()
    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('FS_FEAT_%s %s' % (phase.upper(), result.strip()), flush=True)
    for problem in problems[:20]:
        print('FS_FEAT_PROBLEM %s' % problem, flush=True)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$FEATURES_PY" <<'FEATURES'
"""Checks the 64BIT/FLEX_BG encoding of an ext4 image from the host.

Usage: check_features.py IMAGE [--flex-layout]
"""

import struct
import subprocess
import sys

BLOCK = 4096
COMPAT_64BIT = 1 << 7
COMPAT_FLEX_BG = 1 << 9


def read_superblock(image):
    with open(image, 'rb') as f:
        f.seek(1024)
        sb = f.read(1024)
    if len(sb) != 1024:
        raise SystemExit('%s: short superblock' % image)
    fields = {
        'inodes_count': struct.unpack_from('<I', sb, 0x00)[0],
        'blocks_count': struct.unpack_from('<I', sb, 0x04)[0],
        'first_data_block': struct.unpack_from('<I', sb, 0x14)[0],
        'log_block_size': struct.unpack_from('<I', sb, 0x18)[0],
        'blocks_per_group': struct.unpack_from('<I', sb, 0x20)[0],
        'inodes_per_group': struct.unpack_from('<I', sb, 0x28)[0],
        'feature_incompat': struct.unpack_from('<I', sb, 0x60)[0],
        'desc_size': struct.unpack_from('<H', sb, 0xFE)[0],
    }
    return sb, fields


def descriptor_offsets(sb, fields):
    """Yields (group, byte offset) of every descriptor in the primary table."""
    block_size = 1024 << fields['log_block_size']
    if block_size != BLOCK:
        raise SystemExit('unexpected block size %d' % block_size)
    # The primary descriptor table follows the superblock block: block 1 with
    # 4 KiB blocks.
    table = (1024 // block_size + (1024 + block_size - 1) // block_size) * block_size
    nr_groups = (fields['blocks_count'] + fields['blocks_per_group'] - 1) // (
        fields['blocks_per_group']
    )
    for group in range(nr_groups):
        yield group, table + group * fields['desc_size']


def check_image(image, flex_layout=False):
    problems = []
    sb, fields = read_superblock(image)
    incompat = fields['feature_incompat']
    if not incompat & COMPAT_64BIT:
        problems.append('64BIT is not set')
    if not incompat & COMPAT_FLEX_BG:
        problems.append('FLEX_BG is not set')
    if fields['desc_size'] != 64:
        problems.append('descriptor size is %d, expected 64' % fields['desc_size'])

    # The high block-number words live in the 64-byte extension. Asterinas
    # only ever rewrites the first 32 bytes, so they must stay zero.
    with open(image, 'rb') as f:
        for group, offset in descriptor_offsets(sb, fields):
            f.seek(offset + 32)
            words = struct.unpack('<III', f.read(12))
            if words != (0, 0, 0):
                problems.append(
                    'group %d high block words are %r' % (group, words)
                )

    if flex_layout:
        output = subprocess.run(
            ['dumpe2fs', '-g', image], capture_output=True, text=True, check=False
        )
        if output.returncode != 0:
            problems.append('dumpe2fs -g failed')
        else:
            cross_group = False
            for line in output.stdout.splitlines():
                parts = line.split(':')
                if len(parts) < 5 or not parts[0].isdigit():
                    continue
                group, first, bitmap = int(parts[0]), int(parts[1]), int(parts[4])
                last = first + fields['blocks_per_group']
                if group > 0 and not first <= bitmap < last:
                    cross_group = True
            if not cross_group:
                problems.append('no group metadata placed outside its own group')
    return problems


def main():
    image = sys.argv[1]
    flex_layout = '--flex-layout' in sys.argv[2:]
    problems = check_image(image, flex_layout)
    for problem in problems:
        print('FEATURES_PROBLEM %s: %s' % (image, problem))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
FEATURES

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_features=run' /proc/cmdline || exit 0
echo "FS_FEAT_PROBE_BEGIN"
sleep $SETTLE
python3 /opt/feat-check/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.fs_features=run' /proc/cmdline && exit 0
echo "FS_FEAT_VERIFY_BEGIN"
python3 /opt/feat-check/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=ext4 64BIT/FLEX_BG probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/feat-check/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=ext4 64BIT/FLEX_BG verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/feat-check/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE" "$DATA_IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"

# Turning on 64BIT is a resize2fs operation; tune2fs only clears the "please
# run resize2fs -b" flag and enables the remaining feature.
resize2fs -b "$IMAGE" >"$WORKSPACE/resize2fs.txt" 2>&1 ||
    {
        printf 'FAIL (resize2fs -b could not convert the image; see %s)\n' \
            "$WORKSPACE/resize2fs.txt"
        exit 1
    }
tune2fs -O 64bit,flex_bg "$IMAGE" >"$WORKSPACE/tune2fs.txt" 2>&1 ||
    {
        printf 'FAIL (tune2fs could not enable 64bit/flex_bg; see %s)\n' \
            "$WORKSPACE/tune2fs.txt"
        exit 1
    }

mke2fs -q -t ext4 -b 4096 -I 256 -N 32768 \
    -O 64bit,flex_bg,metadata_csum,^orphan_file \
    "$DATA_IMAGE" 256M >"$WORKSPACE/mke2fs.txt" 2>&1 ||
    {
        printf 'FAIL (mke2fs could not create the flex_bg image; see %s)\n' \
            "$WORKSPACE/mke2fs.txt"
        exit 1
    }

if ! python3 "$FEATURES_PY" "$IMAGE" >"$WORKSPACE/features-root.txt" 2>&1; then
    printf 'FAIL (root image features); see %s\n' "$WORKSPACE/features-root.txt"
    cat "$WORKSPACE/features-root.txt"
    exit 1
fi
if ! python3 "$FEATURES_PY" "$DATA_IMAGE" --flex-layout \
    >"$WORKSPACE/features-data.txt" 2>&1; then
    printf 'FAIL (fresh flex_bg image); see %s\n' "$WORKSPACE/features-data.txt"
    cat "$WORKSPACE/features-data.txt"
    exit 1
fi

{
    printf 'mkdir /opt/feat-check\n'
    printf 'write %s /opt/feat-check/check.py\n' "$CHECK_PY"
    printf 'write %s /opt/feat-check/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/feat-check/probe.sh mode 0100755\n'
    printf 'write %s /opt/feat-check/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/feat-check/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/feat-check-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/feat-check-probe.service /etc/systemd/system/feat-check-probe.service\n'
    printf 'write %s /etc/systemd/system/feat-check-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/feat-check-verify.service /etc/systemd/system/feat-check-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared-root.txt" 2>&1; then
    printf 'FAIL (prepared root image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared-root.txt"
    exit 1
fi
if ! e2fsck -fn "$DATA_IMAGE" >"$WORKSPACE/fsck-prepared-data.txt" 2>&1; then
    printf 'FAIL (fresh image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared-data.txt"
    exit 1
fi

printf 'ext4-features: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --extra-drive "$DATA_IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_features=run" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'FS_FEAT_WRITE ok=True' "$WORKSPACE/write.txt"; then
    printf 'FAIL (write phase reported problems); see %s\n' "$WORKSPACE/write.txt"
    grep -a 'FS_FEAT_PROBLEM' "$WORKSPACE/write.txt" | head -20 || true
    exit 1
fi

for image in "$IMAGE" "$DATA_IMAGE"; do
    if ! e2fsck -fn "$image" >"$WORKSPACE/fsck-write-$(basename "$image").txt" 2>&1; then
        printf 'FAIL (e2fsck -fn after the write boot; see %s)\n' \
            "$WORKSPACE/fsck-write-$(basename "$image").txt"
        exit 1
    fi
done
if ! python3 "$FEATURES_PY" "$IMAGE" >"$WORKSPACE/features-after-write.txt" 2>&1; then
    printf 'FAIL (root features after the write boot); see %s\n' \
        "$WORKSPACE/features-after-write.txt"
    cat "$WORKSPACE/features-after-write.txt"
    exit 1
fi
if ! python3 "$FEATURES_PY" "$DATA_IMAGE" --flex-layout \
    >"$WORKSPACE/features-data-after-write.txt" 2>&1; then
    printf 'FAIL (fresh image after the write boot); see %s\n' \
        "$WORKSPACE/features-data-after-write.txt"
    cat "$WORKSPACE/features-data-after-write.txt"
    exit 1
fi

printf 'ext4-features: verify boot (fresh mount)\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --extra-drive "$DATA_IMAGE" \
    --output "$WORKSPACE/verify.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --wait-for "Powering off."; then
    printf 'FAIL (the verify boot did not shut down; see %s)\n' \
        "$WORKSPACE/verify.txt"
    exit 1
fi

if ! grep -aq 'FS_FEAT_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (contents after the remount); see %s\n' "$WORKSPACE/verify.txt"
    grep -a 'FS_FEAT_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

for image in "$IMAGE" "$DATA_IMAGE"; do
    if ! e2fsck -fn "$image" >"$WORKSPACE/fsck-final-$(basename "$image").txt" 2>&1; then
        printf 'FAIL (e2fsck -fn after the verify boot; see %s)\n' \
            "$WORKSPACE/fsck-final-$(basename "$image").txt"
        exit 1
    fi
done
if ! python3 "$FEATURES_PY" "$IMAGE" >"$WORKSPACE/features-final.txt" 2>&1; then
    printf 'FAIL (root features after the verify boot); see %s\n' \
        "$WORKSPACE/features-final.txt"
    cat "$WORKSPACE/features-final.txt"
    exit 1
fi
if ! python3 "$FEATURES_PY" "$DATA_IMAGE" --flex-layout \
    >"$WORKSPACE/features-data-final.txt" 2>&1; then
    printf 'FAIL (fresh image after the verify boot); see %s\n' \
        "$WORKSPACE/features-data-final.txt"
    cat "$WORKSPACE/features-data-final.txt"
    exit 1
fi

printf 'PASS (%s)\n' "$(grep -a 'FS_FEAT_VERIFY ok=True' "$WORKSPACE/verify.txt" |
    tail -1 | sed 's/.*FS_FEAT_VERIFY //')"
rm -f -- "$IMAGE" "$DATA_IMAGE"
