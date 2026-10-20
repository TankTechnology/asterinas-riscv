#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Nanosecond timestamp gate for the ext2/ext4 implementation.
#
# ext4 inodes larger than 128 bytes store sub-second timestamps in the
# `i_*time_extra` words.  The gate has two phases:
#
#   1. The guest creates files, sets an explicit `utimensat` value with a
#      non-zero nanosecond part, checks that `stat` reports it back, records
#      the expected values, syncs and powers off.
#   2. A second boot mounts the image afresh and re-reads the timestamps.  If
#      the seconds are stored but the extras are lost, this phase fails even
#      though the first one passed.
#
# The host then reads the raw `i_*time_extra` fraction from the image with
# `debugfs` and requires `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_timestamp_check.sh --kernel ELF --initramfs CPIO --base-root IMG
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

Exit status is 0 only if the nanosecond values survive the remount, the
on-disk extras are non-zero and `e2fsck -fn` accepts the image.
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
IMAGE="$WORKSPACE/timestamps.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
# Only the write boot runs the probe; the verify boot skips it.
grep -q 'asterinas.fs_timestamp=write' /proc/cmdline || exit 0
echo "FS_TIMESTAMP_PROBE_BEGIN"
sleep $SETTLE
python3 - <<'PY'
import json
import os

root = '/opt/fs-timestamp'
os.makedirs(root, exist_ok=True)
path = os.path.join(root, 'stamped.bin')
with open(path, 'wb') as f:
    f.write(b'S' * 4096)
    f.flush()
    os.fsync(f.fileno())

# 2023-11-14T22:13:20.123456789Z / 2023-11-14T22:13:20.987654321Z
atime_ns = 1_700_000_000_123_456_789
mtime_ns = 1_700_000_000_987_654_321
os.utime(path, ns=(atime_ns, mtime_ns))

st = os.stat(path)
ok = st.st_atime_ns == atime_ns and st.st_mtime_ns == mtime_ns
# A second file gets its mtime from the kernel clock; record it so the
# seconds-only case is visible in the transcript.
kpath = os.path.join(root, 'kernel-stamped.bin')
with open(kpath, 'wb') as f:
    f.write(b'K' * 4096)
    f.flush()
    os.fsync(f.fileno())
kst = os.stat(kpath)

result = {
    'ok': ok,
    'expected_atime_ns': atime_ns,
    'expected_mtime_ns': mtime_ns,
    'observed_atime_ns': st.st_atime_ns,
    'observed_mtime_ns': st.st_mtime_ns,
    'kernel_mtime_ns': kst.st_mtime_ns,
}
with open(os.path.join(root, 'expected.json'), 'w') as f:
    json.dump(result, f, sort_keys=True)
    f.flush()
    os.fsync(f.fileno())
print('FS_TIMESTAMP_WRITE ok=%s atime_ns=%d mtime_ns=%d kernel_mtime_ns=%d'
      % (ok, st.st_atime_ns, st.st_mtime_ns, kst.st_mtime_ns))
PY
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
# The write boot must not verify and power off before the probe runs.
grep -q 'asterinas.fs_timestamp=write' /proc/cmdline && exit 0
echo "FS_TIMESTAMP_VERIFY_BEGIN"
python3 - <<'PY'
import json
import os

root = '/opt/fs-timestamp'
with open(os.path.join(root, 'expected.json')) as f:
    expected = json.load(f)
path = os.path.join(root, 'stamped.bin')
st = os.stat(path)
ok = (st.st_atime_ns == expected['expected_atime_ns']
      and st.st_mtime_ns == expected['expected_mtime_ns'])
with open(os.path.join(root, 'result.txt'), 'w') as f:
    f.write('ok=%s atime_ns=%d mtime_ns=%d\n'
            % (ok, st.st_atime_ns, st.st_mtime_ns))
    f.flush()
    os.fsync(f.fileno())
print('FS_TIMESTAMP_VERIFY ok=%s atime_ns=%d mtime_ns=%d'
      % (ok, st.st_atime_ns, st.st_mtime_ns))
PY
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=Filesystem nanosecond timestamp probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-timestamp/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=Filesystem nanosecond timestamp verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-timestamp/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-timestamp\n'
    printf 'write %s /opt/fs-timestamp/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-timestamp/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-timestamp/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-timestamp/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-timestamp-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-timestamp-probe.service /etc/systemd/system/fs-timestamp-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-timestamp-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-timestamp-verify.service /etc/systemd/system/fs-timestamp-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'timestamps: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_timestamp=write" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'FS_TIMESTAMP_WRITE ok=True' "$WORKSPACE/write.txt"; then
    printf 'FAIL (stat did not report the values that utimensat set; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

printf 'timestamps: verify boot (fresh mount)\n'
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

if ! grep -aq 'FS_TIMESTAMP_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (nanoseconds did not survive the remount; see %s)\n' \
        "$WORKSPACE/verify.txt"
    exit 1
fi

# The nanoseconds live in the raw inode, so check the image itself as well.
debugfs -R "stat /opt/fs-timestamp/stamped.bin" "$IMAGE" \
    >"$WORKSPACE/debugfs-stat.txt" 2>&1 || true
for field in atime mtime; do
    fraction="$(awk -v field="$field" '$1 == field ":" { split($2, parts, ":"); print parts[2] }' \
        "$WORKSPACE/debugfs-stat.txt")"
    if [[ -z "$fraction" || "$fraction" == "00000000" ]]; then
        printf 'FAIL (%s has no sub-second part in the on-disk inode)\n' "$field"
        exit 1
    fi
done

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

printf 'PASS (%s)\n' \
    "$(grep -a 'FS_TIMESTAMP_VERIFY ok=True' "$WORKSPACE/verify.txt" |
        tail -1 | sed 's/.*FS_TIMESTAMP_VERIFY //')"
printf 'on-disk: %s\n' \
    "$(grep -aE '^ (atime|mtime):' "$WORKSPACE/debugfs-stat.txt" | tr '\n' ' ')"
rm -f -- "$IMAGE"
