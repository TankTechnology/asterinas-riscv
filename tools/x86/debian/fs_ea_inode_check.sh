#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# ext4 EA-inode gate for large xattr values.
#
# The base image is converted with `tune2fs -O ea_inode`.  Boot 1 creates a
# file and stores three attributes: a 32 KiB value that cannot fit in the
# xattr block, a 20 KiB value that is kept for the remount check, and a
# 100-byte value that must stay inline.  It reads all three back.
#
# The host parses the xattr block and requires the two large values to carry a
# non-zero `e_value_inum` and the small one to stay inline, then `e2fsck -fn`
# validates the EA inode reference count, hash, mode, size, blocks and the
# owner's `i_blocks`.
#
# Boot 2 (a fresh mount) verifies the values from their EA inodes, removes the
# 32 KiB attribute and checks that it is gone, then the host requires a clean
# image again.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_ea_inode_check.sh --kernel ELF --initramfs CPIO --base-root IMG
                            --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy per run (required).
  --workspace DIR     Image, scripts and transcripts.
  --settle SECONDS    Guest time before the probe runs (default: 5).
  --timeout SECONDS   Per-boot deadline (default: 1800).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

The base image must not enable another gate's probe unit.

Exit status is 0 only if both boots verify their values, the xattr block uses
EA inodes for the large values, and `e2fsck -fn` accepts the image after each
boot.
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
for tool in debugfs e2fsck python3 tune2fs "$QEMU"; do
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
IMAGE="$WORKSPACE/ea.ext2"
PROBE_PY="$WORKSPACE/probe.py"
PROBE_SH="$WORKSPACE/probe.sh"
PROBE_UNIT="$WORKSPACE/fs-ea.service"
PARSE_PY="$WORKSPACE/parse_xattr.py"

cat >"$PROBE_PY" <<'PY'
import errno
import os
import sys

DIR = '/opt/fs-ea'
PATH = DIR + '/f.bin'
BIG = bytes((i * 7) & 0xFF for i in range(32 * 1024))
KEEP = bytes((i * 11) & 0xFF for i in range(20 * 1024))
SMALL = b'S' * 100


def expect(condition, message, problems):
    if not condition:
        problems.append(message)


def write_phase(problems):
    os.makedirs(DIR, exist_ok=True)
    with open(PATH, 'wb') as file:
        file.write(b'\0' * 4096)
        file.flush()
        os.fsync(file.fileno())
    os.setxattr(PATH, 'user.big', BIG)
    os.setxattr(PATH, 'user.keep', KEEP)
    os.setxattr(PATH, 'user.small', SMALL)
    expect(os.getxattr(PATH, 'user.big') == BIG, 'big read-back', problems)
    expect(os.getxattr(PATH, 'user.keep') == KEEP, 'keep read-back', problems)
    expect(os.getxattr(PATH, 'user.small') == SMALL, 'small read-back', problems)


def verify_phase(problems):
    expect(os.getxattr(PATH, 'user.big') == BIG, 'big after remount', problems)
    expect(os.getxattr(PATH, 'user.keep') == KEEP, 'keep after remount', problems)
    expect(os.getxattr(PATH, 'user.small') == SMALL, 'small after remount', problems)
    os.removexattr(PATH, 'user.big')
    try:
        os.getxattr(PATH, 'user.big')
        problems.append('big still exists after removexattr')
    except OSError as err:
        expect(err.errno == errno.ENODATA, 'removexattr errno %s' % err.errno, problems)
    expect(os.getxattr(PATH, 'user.keep') == KEEP, 'keep after removal', problems)
    expect(os.getxattr(PATH, 'user.small') == SMALL, 'small after removal', problems)


def main():
    phase = None
    with open('/proc/cmdline') as file:
        for token in file.read().split():
            if token.startswith('asterinas.fs_ea='):
                phase = token.split('=', 1)[1]
    if phase not in ('write', 'verify'):
        print('FS_EA_SKIP phase=%s' % phase, flush=True)
        return 0

    problems = []
    if phase == 'write':
        write_phase(problems)
    else:
        verify_phase(problems)

    result = 'ok=%s phase=%s problems=%d\n' % (not problems, phase, len(problems))
    os.makedirs(DIR, exist_ok=True)
    path = DIR + '/result-%s.txt' % phase
    with open(path, 'w') as file:
        file.write(result)
        file.flush()
        os.fsync(file.fileno())
    print('FS_EA_%s %s' % (phase.upper(), result.strip()), flush=True)
    for problem in problems[:20]:
        print('FS_EA_PROBLEM %s' % problem, flush=True)
    os.sync()
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
PY

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_ea=' /proc/cmdline || exit 0
echo "FS_EA_PROBE_BEGIN"
sleep $SETTLE
python3 /opt/fs-ea/probe.py
sync
echo "FS_EA_PROBE_DONE"
sync
systemctl poweroff --no-block
PROBE

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=ext4 EA inode gate
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-ea/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$PARSE_PY" <<'PY'
import struct
import subprocess
import sys

image, path = sys.argv[1], sys.argv[2]
stat = subprocess.run(
    ['debugfs', '-R', 'stat %s' % path, image],
    check=False, capture_output=True, text=True,
).stdout
acl = 0
for line in stat.splitlines():
    if line.startswith('File ACL:'):
        acl = int(line.split()[2])
if acl == 0:
    sys.exit(0)
with open(image, 'rb') as file:
    file.seek(acl * 4096)
    block = file.read(4096)
offset = 32
while offset + 16 <= 4096:
    name_len = block[offset]
    name_index = block[offset + 1]
    value_offset = struct.unpack_from('<H', block, offset + 2)[0]
    value_inum, value_size, entry_hash = struct.unpack_from('<III', block, offset + 4)
    if name_len == 0 and name_index == 0 and value_offset == 0 and \
            value_inum == 0 and value_size == 0 and entry_hash == 0:
        break
    name = block[offset + 16:offset + 16 + name_len].decode('latin1')
    print('%s inum=%d size=%d' % (name, value_inum, value_size))
    offset += (16 + name_len + 3) & ~3
PY

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
tune2fs -O ea_inode "$IMAGE" >"$WORKSPACE/tune2fs.txt" 2>&1 || true
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-converted.txt" 2>&1; then
    printf 'FAIL (the converted image is not clean; see %s/fsck-converted.txt)\n' "$WORKSPACE"
    exit 1
fi
{
    printf 'mkdir /opt/fs-ea\n'
    printf 'write %s /opt/fs-ea/probe.py\n' "$PROBE_PY"
    printf 'write %s /opt/fs-ea/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-ea/probe.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-ea.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-ea.service /etc/systemd/system/fs-ea.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' "$WORKSPACE"
    exit 1
fi

run_phase() {
    local phase="$1"
    local transcript="$WORKSPACE/$phase.txt"
    printf 'ea-inode: %s boot\n' "$phase"
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$transcript" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --append-extra "asterinas.fs_ea=$phase" \
        --wait-for "Powering off."; then
        printf 'FAIL (%s boot did not shut down; see %s)\n' "$phase" "$transcript"
        exit 1
    fi
    if ! debugfs -R "dump /opt/fs-ea/result-$phase.txt $WORKSPACE/result-$phase.txt" \
        "$IMAGE" >/dev/null 2>&1 || [[ ! -f "$WORKSPACE/result-$phase.txt" ]]; then
        printf 'FAIL (%s wrote no result; see %s)\n' "$phase" "$transcript"
        exit 1
    fi
    if ! grep -q '^ok=True' "$WORKSPACE/result-$phase.txt"; then
        printf 'FAIL (%s verification): %s\n' "$phase" \
            "$(cat "$WORKSPACE/result-$phase.txt")"
        exit 1
    fi
    if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-$phase.txt" 2>&1; then
        printf 'FAIL (%s left the image unclean; see %s/fsck-%s.txt)\n' \
            "$phase" "$WORKSPACE" "$phase"
        grep -a 'Inode\|EA' "$WORKSPACE/fsck-$phase.txt" | head -10 || true
        exit 1
    fi
    python3 "$PARSE_PY" "$IMAGE" /opt/fs-ea/f.bin >"$WORKSPACE/xattr-$phase.txt" 2>/dev/null || true
    printf 'ea-inode: %s PASS (%s)\n' "$phase" \
        "$(tr '\n' ';' <"$WORKSPACE/xattr-$phase.txt")"
}

run_phase write
if ! grep -q '^big inum=[1-9]' "$WORKSPACE/xattr-write.txt" ||
    ! grep -q '^keep inum=[1-9]' "$WORKSPACE/xattr-write.txt" ||
    ! grep -q '^small inum=0' "$WORKSPACE/xattr-write.txt"; then
    printf 'FAIL (large values did not use EA inodes or the small value did): %s\n' \
        "$(cat "$WORKSPACE/xattr-write.txt")"
    exit 1
fi

run_phase verify
if grep -q '^big ' "$WORKSPACE/xattr-verify.txt"; then
    printf 'FAIL (the removed value still has an xattr entry): %s\n' \
        "$(cat "$WORKSPACE/xattr-verify.txt")"
    exit 1
fi
if ! grep -q '^keep inum=[1-9]' "$WORKSPACE/xattr-verify.txt" ||
    ! grep -q '^small inum=0' "$WORKSPACE/xattr-verify.txt"; then
    printf 'FAIL (unexpected xattr layout after removal): %s\n' \
        "$(cat "$WORKSPACE/xattr-verify.txt")"
    exit 1
fi

printf 'PASS (EA inode create, read across a remount, remove, fsck clean)\n'
