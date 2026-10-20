#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# ext4 extent allocation gate.
#
# The write boot appends to `/etc/hostname`, which the Debian image creates
# with an inline (depth-zero) extent tree. Each append at a page boundary adds
# one extent record:
#
#   * the first four records fill the inode's inline extent array,
#   * the fifth append must promote the tree to depth one (a leaf block plus
#     an in-index entry),
#   * the sixth append exercises allocation inside that depth-one leaf.
#
# The guest then reads the whole file back, checks the preserved prefix, the
# hole between the original end and the first append, and every appended page.
# The host requires `debugfs dump_extents` to report a depth-one tree, then
# remounts and repeats the content check, and finally runs `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_extent_alloc_check.sh --kernel ELF --initramfs CPIO --base-root IMG
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

Exit status is 0 only if the depth-one tree is on disk, the contents survive
a remount and `e2fsck -fn` accepts the image.
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
IMAGE="$WORKSPACE/extents.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"
CHECK_PY="$WORKSPACE/check.py"
TARGET=/etc/hostname

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_extent=write' /proc/cmdline || exit 0
echo "FS_EXTENT_PROBE_BEGIN"
sleep $SETTLE
python3 - <<'PY'
import hashlib
import os

TARGET = '$TARGET'
with open(TARGET, 'rb') as f:
    original = f.read()
original_sha = hashlib.sha256(original).hexdigest()

fd = os.open(TARGET, os.O_RDWR)
# Five appends at page boundaries: the fifth overflows the inline array.
for index in range(5):
    data = bytes([0x41 + index]) * 4096
    os.pwrite(fd, data, 4096 * (index + 1))
    os.fsync(fd)
os.close(fd)

with open('/opt/fs-extent/write-result.txt', 'w') as f:
    f.write('original_len=%d\n' % len(original))
    f.write('original_sha=%s\n' % original_sha)
    f.flush()
    os.fsync(f.fileno())
print('FS_EXTENT_WRITE original_len=%d' % len(original))
PY
python3 /opt/fs-extent/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.fs_extent=write' /proc/cmdline && exit 0
echo "FS_EXTENT_VERIFY_BEGIN"
python3 /opt/fs-extent/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$CHECK_PY" <<'CHECKS'
import hashlib
import os
import sys

TARGET = '/etc/hostname'
PAGES = 5


def main():
    phase = sys.argv[1]
    with open('/opt/fs-extent/write-result.txt') as f:
        recorded = dict(line.strip().split('=', 1) for line in f if '=' in line)
    original_len = int(recorded['original_len'])

    problems = []
    with open(TARGET, 'rb') as f:
        data = f.read()
    expected_len = 4096 * (PAGES + 1)
    if len(data) != expected_len:
        problems.append('size %d, expected %d' % (len(data), expected_len))
    else:
        prefix = data[:original_len]
        if hashlib.sha256(prefix).hexdigest() != recorded['original_sha']:
            problems.append('original prefix changed')
        if data[original_len:4096] != b'\0' * (4096 - original_len):
            problems.append('the hole after the original data is not zeroed')
        for index in range(PAGES):
            start = 4096 * (index + 1)
            if data[start:start + 4096] != bytes([0x41 + index]) * 4096:
                problems.append('appended page %d has the wrong contents' % index)

    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open('/opt/fs-extent/result.txt', 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('FS_EXTENT_%s %s' % (phase.upper(), result.strip()))
    for problem in problems[:20]:
        print('FS_EXTENT_PROBLEM %s' % problem)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=ext4 extent allocation probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-extent/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=ext4 extent allocation verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-extent/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-extent\n'
    printf 'write %s /opt/fs-extent/check.py\n' "$CHECK_PY"
    printf 'write %s /opt/fs-extent/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-extent/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-extent/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-extent/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-extent-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-extent-probe.service /etc/systemd/system/fs-extent-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-extent-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-extent-verify.service /etc/systemd/system/fs-extent-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'extents: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_extent=write" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'FS_EXTENT_WRITE ok=True' "$WORKSPACE/write.txt"; then
    printf 'FAIL (contents after the appends); see %s\n' "$WORKSPACE/write.txt"
    grep -a 'FS_EXTENT_PROBLEM' "$WORKSPACE/write.txt" | head -20 || true
    exit 1
fi

debugfs -R "dump_extents $TARGET" "$IMAGE" \
    >"$WORKSPACE/dump-extents.txt" 2>&1 || true
if ! grep -q '^ 0/ 1' "$WORKSPACE/dump-extents.txt"; then
    printf 'FAIL (the tree did not become depth one; see %s)\n' \
        "$WORKSPACE/dump-extents.txt"
    exit 1
fi

printf 'extents: verify boot (fresh mount)\n'
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

if ! grep -aq 'FS_EXTENT_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (contents after remount); see %s\n' "$WORKSPACE/verify.txt"
    grep -a 'FS_EXTENT_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

printf 'PASS (depth-one extent tree, %s)\n' \
    "$(sed -n '3p' "$WORKSPACE/dump-extents.txt" | tr -s ' ')"
rm -f -- "$IMAGE"
