#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# O_TMPFILE and orphan-list gate for the ext2/ext4 implementation.
#
# Clean mode exercises, in one boot:
#
#   * a linkable O_TMPFILE, written, fsynced and named with
#     linkat(AT_EMPTY_PATH), then closed; the name must survive a remount,
#   * an O_TMPFILE|O_EXCL, whose linkat must fail with ENOENT, then closed;
#     closing it must reclaim the inode (no orphan left behind).
#
# Crash mode keeps an unnamed tmpfile open, fsynced, when the host cuts
# power.  The inode is on the superblock orphan chain with link count zero;
# the next boot must reclaim it during mount (before systemd and before the
# host's e2fsck), leaving a checker-clean image.
#
# The host always ends with `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_tmpfile_check.sh --kernel ELF --initramfs CPIO --base-root IMG
                           --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy per run (required).
  --workspace DIR     Image, scripts and transcripts.
  --crash             Cut power while an unnamed tmpfile is open, then
                      require the recovery boot to reclaim it.
  --settle SECONDS    Guest time before the probe runs (default: 10).
  --timeout SECONDS   Per-boot deadline (default: 900).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Exit status is 0 only if the tmpfile invariants hold and `e2fsck -fn`
accepts the image.
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
CRASH=0
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
        --crash) CRASH=1; shift ;;
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
IMAGE="$WORKSPACE/tmpfile.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"
CHECK_PY="$WORKSPACE/check.py"

cat >"$CHECK_PY" <<'CHECKS'
import os
import sys

ROOT = '/opt/fs-tmpfile'


def main():
    phase = sys.argv[1]
    problems = []
    linked = os.path.join(ROOT, 'linked.bin')
    if not os.path.isfile(linked):
        problems.append('linked.bin is missing after the remount')
    else:
        with open(linked, 'rb') as f:
            if f.read() != b'T' * 4096:
                problems.append('linked.bin has the wrong contents')
        if os.stat(linked).st_nlink != 1:
            problems.append('linked.bin has link count %d' % os.stat(linked).st_nlink)
    with open(os.path.join(ROOT, 'write-result.txt')) as f:
        recorded = dict(line.strip().split('=', 1) for line in f if '=' in line)
    if recorded.get('excl_errno') != str(2):  # ENOENT
        problems.append('O_EXCL tmpfile link errno %s' % recorded.get('excl_errno'))

    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('FS_TMPFILE_%s %s' % (phase.upper(), result.strip()))
    for problem in problems[:20]:
        print('FS_TMPFILE_PROBLEM %s' % problem)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_tmpfile=run' /proc/cmdline || exit 0
echo "FS_TMPFILE_PROBE_BEGIN"
sleep $SETTLE
python3 - <<'PY'
import ctypes
import errno
import os
import sys
import time

ROOT = '/opt/fs-tmpfile'
AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
O_TMPFILE = getattr(os, 'O_TMPFILE', 0o20200000)

libc = ctypes.CDLL('libc.so.6', use_errno=True)
linkat = libc.linkat
linkat.argtypes = [ctypes.c_int, ctypes.c_char_p,
                   ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
linkat.restype = ctypes.c_int


def link_tmpfile(fd, name):
    path = os.path.join(ROOT, name).encode()
    ctypes.set_errno(0)
    rc = linkat(fd, b'', AT_FDCWD, path, AT_EMPTY_PATH)
    return rc, ctypes.get_errno()


# 1. A linkable tmpfile is named with linkat(AT_EMPTY_PATH).
fd = os.open(ROOT, O_TMPFILE | os.O_RDWR, 0o644)
os.write(fd, b'T' * 4096)
os.fsync(fd)
rc, err = link_tmpfile(fd, 'linked.bin')
if rc != 0:
    raise OSError(err, 'linkat of a linkable tmpfile failed')
os.close(fd)

# 2. O_TMPFILE|O_EXCL cannot be linked; closing it reclaims the inode.
fd = os.open(ROOT, O_TMPFILE | os.O_RDWR | os.O_EXCL, 0o644)
os.write(fd, b'X' * 4096)
os.fsync(fd)
rc, err = link_tmpfile(fd, 'excl.bin')
os.close(fd)
if rc == 0:
    raise AssertionError('O_TMPFILE|O_EXCL was linked')
if err != errno.ENOENT:
    raise AssertionError('O_TMPFILE|O_EXCL link errno %d, expected ENOENT' % err)

result = {
    'excl_errno': err,
}
with open(os.path.join(ROOT, 'write-result.txt'), 'w') as f:
    for key, value in sorted(result.items()):
        f.write('%s=%s\\n' % (key, value))
    f.flush()
    os.fsync(f.fileno())

if os.environ.get('FS_TMPFILE_CRASH') == '1':
    # Leave an unnamed tmpfile open and durable: the inode sits on the
    # orphan chain with link count zero when the host cuts power.
    fd = os.open(ROOT, O_TMPFILE | os.O_RDWR, 0o644)
    os.write(fd, b'O' * 4096)
    os.fsync(fd)
    print('FS_TMPFILE_ORPHAN ino=%d' % os.fstat(fd).st_ino, flush=True)
    print('FS_TMPFILE_READY', flush=True)
    while True:
        time.sleep(1)
print('FS_TMPFILE_WRITE ok=True excl_errno=%d' % err, flush=True)
PY
python3 /opt/fs-tmpfile/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.fs_tmpfile=run' /proc/cmdline && exit 0
echo "FS_TMPFILE_VERIFY_BEGIN"
python3 /opt/fs-tmpfile/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<UNIT
[Unit]
Description=O_TMPFILE probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
Environment=FS_TMPFILE_CRASH=$CRASH
ExecStart=/opt/fs-tmpfile/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=O_TMPFILE verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-tmpfile/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-tmpfile\n'
    printf 'write %s /opt/fs-tmpfile/check.py\n' "$CHECK_PY"
    printf 'write %s /opt/fs-tmpfile/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-tmpfile/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-tmpfile/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-tmpfile/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-tmpfile-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-tmpfile-probe.service /etc/systemd/system/fs-tmpfile-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-tmpfile-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-tmpfile-verify.service /etc/systemd/system/fs-tmpfile-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

if (( CRASH )); then
    printf 'tmpfile: crash boot (unnamed tmpfile open)\n'
    mapfile -t argv < <("$BOOT_SCRIPT" --print-argv \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$WORKSPACE/crash.txt" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --append-extra "asterinas.fs_tmpfile=run" \
        --wait-for "Powering off.")
    "${argv[@]}" >"$WORKSPACE/crash.txt" 2>&1 &
    qemu_pid=$!
    waited=0
    while (( waited < TIMEOUT )); do
        grep -aq "FS_TMPFILE_READY" "$WORKSPACE/crash.txt" && break
        if ! kill -0 "$qemu_pid" 2>/dev/null; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if ! grep -aq "FS_TMPFILE_READY" "$WORKSPACE/crash.txt"; then
        printf 'FAIL (the probe never reached READY; see %s)\n' \
            "$WORKSPACE/crash.txt"
        kill -9 "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
        exit 1
    fi
    sleep 1
    kill -9 "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true

    printf 'tmpfile: recovery boot (orphan cleanup)\n'
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$WORKSPACE/recovery.txt" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --wait-for "Powering off."; then
        printf 'FAIL (the recovery boot did not shut down; see %s)\n' \
            "$WORKSPACE/recovery.txt"
        exit 1
    fi
    if ! grep -aq 'FS_TMPFILE_VERIFY ok=True' "$WORKSPACE/recovery.txt"; then
        printf 'FAIL (verification after the power cut); see %s\n' \
            "$WORKSPACE/recovery.txt"
        grep -a 'FS_TMPFILE_PROBLEM' "$WORKSPACE/recovery.txt" | head -20 || true
        exit 1
    fi
else
    printf 'tmpfile: write boot\n'
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$WORKSPACE/write.txt" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --append-extra "asterinas.fs_tmpfile=run" \
        --wait-for "Powering off."; then
        printf 'FAIL (the write boot did not shut down; see %s)\n' \
            "$WORKSPACE/write.txt"
        exit 1
    fi

    printf 'tmpfile: verify boot (fresh mount)\n'
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
    if ! grep -aq 'FS_TMPFILE_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
        printf 'FAIL (verification after the remount); see %s\n' \
            "$WORKSPACE/verify.txt"
        grep -a 'FS_TMPFILE_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
        exit 1
    fi
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

if (( CRASH )); then
    printf 'PASS (orphaned tmpfile reclaimed on the recovery mount)\n'
else
    printf 'PASS (linked tmpfile persisted, O_EXCL unlinkable, no orphan left)\n'
fi
rm -f -- "$IMAGE"
