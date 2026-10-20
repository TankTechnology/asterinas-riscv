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
# A directory with a default ACL checks inheritance:
#
#   * a file created with mode 0666 gets the default ACL clamped to 0660
#     (owner rw, named user r, group r, named group rw, mask rw, other none),
#     and its mode is recomputed from the clamped ACL;
#   * a subdirectory receives the same ACL as both its access and default
#     ACL, with its mode unchanged;
#   * a file created inside that subdirectory inherits the same ACL again;
#   * a symlink carries no ACL.
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
ACL_DEFAULT_ATTR = 'system.posix_acl_default'
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
# ACL_A after `chmod 0604`: owner stays rw, the mask follows the group bits
# (none) and other becomes r.
ACL_C_AFTER_CHMOD = [
    (TAG_USER_OBJ, 0o6, UNDEFINED_ID),
    (TAG_USER, 0o4, 1001),
    (TAG_GROUP_OBJ, 0o6, UNDEFINED_ID),
    (TAG_MASK, 0o0, UNDEFINED_ID),
    (TAG_OTHER, 0o4, UNDEFINED_ID),
]
# A minimal ACL has no mask; chmod rewrites its group entry.
ACL_D = [
    (TAG_USER_OBJ, 0o6, UNDEFINED_ID),
    (TAG_GROUP_OBJ, 0o4, UNDEFINED_ID),
    (TAG_OTHER, 0o4, UNDEFINED_ID),
]
ACL_D_AFTER_CHMOD = [
    (TAG_USER_OBJ, 0o6, UNDEFINED_ID),
    (TAG_GROUP_OBJ, 0o0, UNDEFINED_ID),
    (TAG_OTHER, 0o0, UNDEFINED_ID),
]
# Default ACL of the inheritance directory: owner rwx, named user 1001 r,
# group r-x, named group 1002 rw, mask rwx, other none.
ACL_DEFAULT = [
    (TAG_USER_OBJ, 0o7, UNDEFINED_ID),
    (TAG_USER, 0o4, 1001),
    (TAG_GROUP_OBJ, 0o5, UNDEFINED_ID),
    (TAG_GROUP, 0o6, 1002),
    (TAG_MASK, 0o7, UNDEFINED_ID),
    (TAG_OTHER, 0o0, UNDEFINED_ID),
]
# A file created with mode 0666 inherits ACL_DEFAULT clamped by the mode:
# owner 7&6, named user 4&6, group 5&6, named group 6&6, mask 7&6, other 0&6.
ACL_INHERITED = [
    (TAG_USER_OBJ, 0o6, UNDEFINED_ID),
    (TAG_USER, 0o4, 1001),
    (TAG_GROUP_OBJ, 0o4, UNDEFINED_ID),
    (TAG_GROUP, 0o6, 1002),
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
    'c_acl_mode_match': True,
    'c_user1001_read': False,
    'c_other_read': True,
    'd_acl_mode_match': True,
    'd_other_read': False,
    'inherit_acl_match': True,
    'inherit_mode_match': True,
    'inherit_subdir_access_match': True,
    'inherit_subdir_default_match': True,
    'inherit_subdir_mode_match': True,
    'inherit_nested_acl_match': True,
    'inherit_symlink_has_no_acl': True,
    'inherit_user1001_read': True,
    'inherit_user1001_write': False,
    'inherit_group1002_write': True,
    'inherit_other_read': False,
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
    for name, marker, acl in (
        ('a.bin', b'A', ACL_A),
        ('b.bin', b'B', ACL_B),
        ('c.bin', b'C', ACL_A),
        ('d.bin', b'D', ACL_D),
    ):
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

    # chmod must rewrite the ACL's mask/group/other entries.
    os.chmod(os.path.join(ROOT, 'c.bin'), 0o604)
    os.chmod(os.path.join(ROOT, 'd.bin'), 0o600)

    # Default-ACL inheritance: the mode passed to create must reach the
    # filesystem unmasked so the ACL clamping is what decides the result.
    inherit = os.path.join(ROOT, 'inherit')
    os.mkdir(inherit, 0o777)
    os.setxattr(inherit, ACL_DEFAULT_ATTR, acl_bytes(ACL_DEFAULT))
    os.umask(0)
    fd = os.open(os.path.join(inherit, 'f.bin'), os.O_CREAT | os.O_WRONLY, 0o666)
    os.close(fd)
    subdir = os.path.join(inherit, 'sub')
    os.mkdir(subdir, 0o777)
    fd = os.open(os.path.join(subdir, 'child.bin'), os.O_CREAT | os.O_WRONLY, 0o666)
    os.close(fd)
    os.symlink('f.bin', os.path.join(inherit, 'link'))
    os.umask(0o022)


def observe():
    a = os.path.join(ROOT, 'a.bin')
    b = os.path.join(ROOT, 'b.bin')
    inherit = os.path.join(ROOT, 'inherit')
    inherited = os.path.join(inherit, 'f.bin')
    subdir = os.path.join(inherit, 'sub')
    try:
        os.getxattr(os.path.join(inherit, 'link'), ACL_ATTR, follow_symlinks=False)
        symlink_has_no_acl = False
    except OSError:
        symlink_has_no_acl = True
    observed = {
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
        'c_acl_mode_match': os.getxattr(os.path.join(ROOT, 'c.bin'), ACL_ATTR)
            == acl_bytes(ACL_C_AFTER_CHMOD),
        'c_user1001_read': run_as(1001, 1001, READ, os.path.join(ROOT, 'c.bin')),
        'c_other_read': run_as(1002, 1002, READ, os.path.join(ROOT, 'c.bin')),
        'd_acl_mode_match': os.getxattr(os.path.join(ROOT, 'd.bin'), ACL_ATTR)
            == acl_bytes(ACL_D_AFTER_CHMOD),
        'd_other_read': run_as(1002, 1002, READ, os.path.join(ROOT, 'd.bin')),
        'inherit_acl_match': os.getxattr(inherited, ACL_ATTR)
            == acl_bytes(ACL_INHERITED),
        'inherit_mode_match': (os.stat(inherited).st_mode & 0o777) == 0o660,
        'inherit_subdir_access_match': os.getxattr(subdir, ACL_ATTR)
            == acl_bytes(ACL_DEFAULT),
        'inherit_subdir_default_match': os.getxattr(subdir, ACL_DEFAULT_ATTR)
            == acl_bytes(ACL_DEFAULT),
        'inherit_subdir_mode_match': (os.stat(subdir).st_mode & 0o777) == 0o777,
        'inherit_nested_acl_match': os.getxattr(os.path.join(subdir, 'child.bin'), ACL_ATTR)
            == acl_bytes(ACL_INHERITED),
        'inherit_symlink_has_no_acl': symlink_has_no_acl,
        'inherit_user1001_read': run_as(1001, 1001, READ, inherited),
        'inherit_user1001_write': run_as(1001, 1001, WRITE, inherited),
        'inherit_group1002_write': run_as(1002, 1002, WRITE, inherited),
        'inherit_other_read': run_as(1003, 1003, READ, inherited),
    }
    return observed


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
