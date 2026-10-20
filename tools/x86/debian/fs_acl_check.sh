#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# POSIX ACL gate for the ext2/ext4 implementation.
#
# The write boot stores `system.posix_acl_access` on two files whose mode
# bits alone would deny every non-root access, then checks the access rules
# by running short reads and writes as different users:
#
#   * file A: owner rw, named user 1001 r, mask r, other none
#       - uid 1001 may read but not write (the mask also applies to named
#         users), uid 1002 may not read, root bypasses the ACL.
#   * file B: owner rw, named user 1001 r, group 1002 r, mask rw, other none
#       - uid 1002 (gid 1002) may read but not write; uid 1003 may not read;
#         uid 1001 still may not write because its own entry lacks w.
#
# The second boot re-reads the ACL xattr and repeats every check after a
# remount; the host then requires `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_acl_check.sh --kernel ELF --initramfs CPIO --base-root IMG
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

Exit status is 0 only if every ACL decision and the stored attribute agree
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
IMAGE="$WORKSPACE/acl.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"
CHECK_PY="$WORKSPACE/check.py"

cat >"$CHECK_PY" <<'CHECKS'
import os
import subprocess
import sys

ROOT = '/opt/fs-acl'
ACL_ATTR = 'system.posix_acl_access'
TAG_USER_OBJ = 0x01
TAG_USER = 0x02
TAG_GROUP_OBJ = 0x04
TAG_GROUP = 0x08
TAG_MASK = 0x10
TAG_OTHER = 0x20
UNDEFINED_ID = 0xFFFFFFFF

# owner rw, user 1001 r, group rw, mask r, other none.
ACL_A = [
    (TAG_USER_OBJ, 0o6, UNDEFINED_ID),
    (TAG_USER, 0o4, 1001),
    (TAG_GROUP_OBJ, 0o6, UNDEFINED_ID),
    (TAG_MASK, 0o4, UNDEFINED_ID),
    (TAG_OTHER, 0o0, UNDEFINED_ID),
]
# owner rw, user 1001 r, group none, group 1002 r, mask rw, other none.
ACL_B = [
    (TAG_USER_OBJ, 0o6, UNDEFINED_ID),
    (TAG_USER, 0o4, 1001),
    (TAG_GROUP_OBJ, 0o0, UNDEFINED_ID),
    (TAG_GROUP, 0o4, 1002),
    (TAG_MASK, 0o6, UNDEFINED_ID),
    (TAG_OTHER, 0o0, UNDEFINED_ID),
]

READ = "open(__import__('sys').argv[1], 'rb').read()"
WRITE = "open(__import__('sys').argv[1], 'r+b')"

EXPECTED = {
    'a_root_read': True,
    'a_root_write': True,
    'a_user1001_read': True,
    'a_user1001_write': False,
    'a_other_read': False,
    'b_group1002_read': True,
    'b_group1002_write': False,
    'b_other_read': False,
    'b_user1001_write': False,
    'acl_bytes_match': True,
}


def acl_bytes(entries):
    out = bytearray((2).to_bytes(4, 'little'))
    for tag, perm, ident in entries:
        out += tag.to_bytes(2, 'little')
        out += perm.to_bytes(2, 'little')
        out += ident.to_bytes(4, 'little')
    return bytes(out)


def run_as(uid, gid, code, path):
    result = subprocess.run(
        [sys.executable, '-c', code, path],
        user=uid,
        group=gid,
        extra_groups=[],
        capture_output=True,
    )
    return result.returncode == 0


def setup():
    os.makedirs(ROOT, exist_ok=True)
    os.chmod(ROOT, 0o777)
    for name, marker, acl in (('a.bin', b'A', ACL_A), ('b.bin', b'B', ACL_B)):
        path = os.path.join(ROOT, name)
        with open(path, 'wb') as f:
            f.write(marker * 4096)
            f.flush()
            os.fsync(f.fileno())
        # Mode bits alone would deny every non-root access; the ACL must be
        # what decides.
        os.chmod(path, 0o600)
        os.setxattr(path, ACL_ATTR, acl_bytes(acl))
        fd = os.open(path, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)


def observe():
    a = os.path.join(ROOT, 'a.bin')
    b = os.path.join(ROOT, 'b.bin')
    return {
        'a_root_read': run_as(0, 0, READ, a),
        'a_root_write': run_as(0, 0, WRITE, a),
        'a_user1001_read': run_as(1001, 1001, READ, a),
        'a_user1001_write': run_as(1001, 1001, WRITE, a),
        'a_other_read': run_as(1002, 1002, READ, a),
        'b_group1002_read': run_as(1002, 1002, READ, b),
        'b_group1002_write': run_as(1002, 1002, WRITE, b),
        'b_other_read': run_as(1003, 1003, READ, b),
        'b_user1001_write': run_as(1001, 1001, WRITE, b),
        'acl_bytes_match': os.getxattr(a, ACL_ATTR) == acl_bytes(ACL_A),
    }


def main():
    phase = sys.argv[1]
    if phase == 'write':
        setup()
    problems = []
    observed = observe()
    for key, expected in EXPECTED.items():
        if observed.get(key) != expected:
            problems.append('%s=%s expected %s' % (key, observed.get(key), expected))
    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('FS_ACL_%s %s' % (phase.upper(), result.strip()), flush=True)
    for problem in problems[:20]:
        print('FS_ACL_PROBLEM %s' % problem, flush=True)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_acl=run' /proc/cmdline || exit 0
echo "FS_ACL_PROBE_BEGIN"
sleep $SETTLE
python3 /opt/fs-acl/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.fs_acl=run' /proc/cmdline && exit 0
echo "FS_ACL_VERIFY_BEGIN"
python3 /opt/fs-acl/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=POSIX ACL probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-acl/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=POSIX ACL verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-acl/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-acl\n'
    printf 'write %s /opt/fs-acl/check.py\n' "$CHECK_PY"
    printf 'write %s /opt/fs-acl/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-acl/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-acl/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-acl/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-acl-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-acl-probe.service /etc/systemd/system/fs-acl-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-acl-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-acl-verify.service /etc/systemd/system/fs-acl-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'acl: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_acl=run" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'FS_ACL_WRITE ok=True' "$WORKSPACE/write.txt"; then
    printf 'FAIL (ACL decisions in the write boot); see %s\n' "$WORKSPACE/write.txt"
    grep -a 'FS_ACL_PROBLEM' "$WORKSPACE/write.txt" | head -20 || true
    exit 1
fi

printf 'acl: verify boot (fresh mount)\n'
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

if ! grep -aq 'FS_ACL_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (ACL decisions after remount); see %s\n' "$WORKSPACE/verify.txt"
    grep -a 'FS_ACL_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

printf 'PASS (%s)\n' "$(grep -a 'FS_ACL_VERIFY ok=True' "$WORKSPACE/verify.txt" |
    tail -1 | sed 's/.*FS_ACL_VERIFY //')"
rm -f -- "$IMAGE"
