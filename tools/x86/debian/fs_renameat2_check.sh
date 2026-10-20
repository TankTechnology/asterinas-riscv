#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# renameat2 semantics gate for the ext2/ext4 implementation.
#
# The write boot exercises, inside the guest:
#
#   * RENAME_EXCHANGE of two files in one directory,
#   * RENAME_EXCHANGE of a directory with a file (allowed by Linux; the
#     entries may have different types),
#   * RENAME_EXCHANGE across directories, including both directories and
#     files, and the resulting `..` entries and parent link counts,
#   * RENAME_NOREPLACE success and EEXIST,
#   * ENOENT for an exchange whose target does not exist,
#   * EINVAL for NOREPLACE|EXCHANGE.
#
# It records the resulting tree (types, contents, inode relationships) and
# powers off.  A second boot mounts the image afresh and re-checks the same
# invariants, which proves they were persisted and not just held in memory.
# The host then requires `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_renameat2_check.sh --kernel ELF --initramfs CPIO --base-root IMG
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

Exit status is 0 only if every renameat2 invariant holds after a remount and
`e2fsck -fn` accepts the image.
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
IMAGE="$WORKSPACE/renameat2.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"

# Shared by both phases: the same checks, run once after the exchange
# operations and once after a fresh mount.
cat >"$WORKSPACE/checks.py" <<'CHECKS'
import json
import os
import sys

ROOT = '/opt/fs-rename'


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def check_exchange_layout():
    problems = []
    base = os.path.join(ROOT, 'exchange')
    expect = {
        'f1': b'B' * 4096,
        'f2': b'A' * 4096,
    }
    for name, data in expect.items():
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            problems.append('%s is not a file' % path)
        elif read(path) != data:
            problems.append('%s has the wrong contents' % path)

    # d1 and f5 exchanged: d1 became the file, f5 the directory.
    d1 = os.path.join(base, 'd1')
    f5 = os.path.join(base, 'f5')
    if not os.path.isfile(d1) or read(d1) != b'F' * 4096:
        problems.append('d1 did not become the exchanged file')
    if not os.path.isdir(f5):
        problems.append('f5 did not become the exchanged directory')
    else:
        child = os.path.join(f5, 'child')
        if not os.path.isfile(child) or read(child) != b'C' * 4096:
            problems.append('the exchanged directory lost its child')
        if os.stat(os.path.join(f5, '..')).st_ino != os.stat(base).st_ino:
            problems.append("the exchanged directory's .. does not point to its parent")

    # NOREPLACE moved f3 to f4.
    if os.path.exists(os.path.join(base, 'f3')):
        problems.append('f3 still exists after a successful NOREPLACE')
    if not os.path.isfile(os.path.join(base, 'f4')) or \
            read(os.path.join(base, 'f4')) != b'N' * 4096:
        problems.append('f4 is missing or has the wrong contents')
    return problems


def check_cross_dir():
    problems = []
    base = os.path.join(ROOT, 'cross')
    a = os.path.join(base, 'a')
    b = os.path.join(base, 'b')
    # The exchange swaps the two paths: a/d1 now holds the directory that
    # was b/d2 (with child2), and b/d2 holds the directory that was a/d1.
    if not os.path.isdir(os.path.join(a, 'd1')):
        problems.append('a/d1 is missing')
    elif read(os.path.join(a, 'd1', 'child2')) != b'2' * 4096:
        problems.append('a/d1/child2 has the wrong contents')
    if not os.path.isdir(os.path.join(b, 'd2')):
        problems.append('b/d2 is missing')
    elif read(os.path.join(b, 'd2', 'child1')) != b'1' * 4096:
        problems.append('b/d2/child1 has the wrong contents')
    if os.path.isdir(os.path.join(a, 'd2')):
        problems.append('a/d2 exists although the exchange should have moved it')
    if os.path.isdir(os.path.join(b, 'd1')):
        problems.append('b/d1 exists although the exchange should have moved it')
    if os.path.isdir(os.path.join(a, 'd1')) and \
            os.stat(os.path.join(a, 'd1', '..')).st_ino != os.stat(a).st_ino:
        problems.append("a/d1's .. does not point to a")
    if os.path.isdir(os.path.join(b, 'd2')) and \
            os.stat(os.path.join(b, 'd2', '..')).st_ino != os.stat(b).st_ino:
        problems.append("b/d2's .. does not point to b")

    # A directory exchanged with a file keeps its own name slot: p1/sub is
    # now the file and p2/plain is the directory.
    if not os.path.isfile(os.path.join(ROOT, 'cross', 'p1', 'sub')):
        problems.append('p1/sub did not become the exchanged file')
    elif read(os.path.join(ROOT, 'cross', 'p1', 'sub')) != b'P' * 4096:
        problems.append('p1/sub has the wrong contents')
    plain = os.path.join(ROOT, 'cross', 'p2', 'plain')
    if not os.path.isdir(plain):
        problems.append('p2/plain did not become the exchanged directory')
    elif os.stat(os.path.join(plain, '..')).st_ino != \
            os.stat(os.path.join(ROOT, 'cross', 'p2')).st_ino:
        problems.append("p2/plain's .. does not point to p2")
    # Files exchanged across directories.
    if read(os.path.join(a, 'fa')) != b'Y' * 4096:
        problems.append('a/fa has the wrong contents')
    if read(os.path.join(b, 'fb')) != b'X' * 4096:
        problems.append('b/fb has the wrong contents')
    return problems


def main():
    phase = sys.argv[1]
    problems = check_exchange_layout() + check_cross_dir()
    result = {'ok': not problems, 'phase': phase, 'problems': problems[:20]}
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        json.dump(result, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    print('FS_RENAMEAT2_%s ok=%s problems=%d'
          % (phase.upper(), not problems, len(problems)))
    for problem in problems[:20]:
        print('FS_RENAMEAT2_PROBLEM %s' % problem)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
CHECKS

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
# Only the write boot performs the exchanges.
grep -q 'asterinas.fs_renameat2=write' /proc/cmdline || exit 0
echo "FS_RENAMEAT2_PROBE_BEGIN"
sleep $SETTLE
python3 - <<'PY'
import ctypes
import errno
import os

ROOT = '/opt/fs-rename'
AT_FDCWD = -100
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2

libc = ctypes.CDLL('libc.so.6', use_errno=True)
renameat2 = libc.renameat2
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p,
                      ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
renameat2.restype = ctypes.c_int


def rn(old, new, flags=0):
    ctypes.set_errno(0)
    rc = renameat2(AT_FDCWD, old.encode(), AT_FDCWD, new.encode(), flags)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), old)
    return rc


def expect_error(err, fn, *args):
    try:
        fn(*args)
    except OSError as exc:
        if exc.errno != err:
            raise AssertionError('expected errno %d, got %d for %r'
                                 % (err, exc.errno, args))
        return
    raise AssertionError('expected errno %d, call succeeded: %r' % (err, args))


def write(path, data):
    with open(path, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


os.makedirs(ROOT, exist_ok=True)
exchange = os.path.join(ROOT, 'exchange')
os.makedirs(exchange, exist_ok=True)
write(os.path.join(exchange, 'f1'), b'A' * 4096)
write(os.path.join(exchange, 'f2'), b'B' * 4096)
rn(os.path.join(exchange, 'f1'), os.path.join(exchange, 'f2'), RENAME_EXCHANGE)

# NOREPLACE: existing target must be EEXIST, missing target must succeed.
expect_error(errno.EEXIST, rn, os.path.join(exchange, 'f1'),
             os.path.join(exchange, 'f2'), RENAME_NOREPLACE)
write(os.path.join(exchange, 'f3'), b'N' * 4096)
rn(os.path.join(exchange, 'f3'), os.path.join(exchange, 'f4'), RENAME_NOREPLACE)

# Directory <-> file exchange in one directory.
os.makedirs(os.path.join(exchange, 'd1'))
write(os.path.join(exchange, 'd1', 'child'), b'C' * 4096)
write(os.path.join(exchange, 'f5'), b'F' * 4096)
rn(os.path.join(exchange, 'd1'), os.path.join(exchange, 'f5'), RENAME_EXCHANGE)

# Exchange with a missing target is ENOENT.
expect_error(errno.ENOENT, rn, os.path.join(exchange, 'f1'),
             os.path.join(exchange, 'missing'), RENAME_EXCHANGE)
# NOREPLACE and EXCHANGE are mutually exclusive.
expect_error(errno.EINVAL, rn, os.path.join(exchange, 'f1'),
             os.path.join(exchange, 'f2'),
             RENAME_NOREPLACE | RENAME_EXCHANGE)

# Cross-directory exchange of two non-empty directories and of two files.
cross = os.path.join(ROOT, 'cross')
for sub in ('a', 'b'):
    os.makedirs(os.path.join(cross, sub), exist_ok=True)
os.makedirs(os.path.join(cross, 'a', 'd1'))
os.makedirs(os.path.join(cross, 'b', 'd2'))
write(os.path.join(cross, 'a', 'd1', 'child1'), b'1' * 4096)
write(os.path.join(cross, 'b', 'd2', 'child2'), b'2' * 4096)
write(os.path.join(cross, 'a', 'fa'), b'X' * 4096)
write(os.path.join(cross, 'b', 'fb'), b'Y' * 4096)
link_a = os.stat(os.path.join(cross, 'a')).st_nlink
link_b = os.stat(os.path.join(cross, 'b')).st_nlink

rn(os.path.join(cross, 'a', 'd1'), os.path.join(cross, 'b', 'd2'), RENAME_EXCHANGE)
rn(os.path.join(cross, 'a', 'fa'), os.path.join(cross, 'b', 'fb'), RENAME_EXCHANGE)

# Both parents still own one subdirectory, so their link counts must be
# unchanged. Directory <-> file exchange above moved a subdirectory into
# the exchange directory from itself, so no count change there either.
assert os.stat(os.path.join(cross, 'a')).st_nlink == link_a, 'a link count changed'
assert os.stat(os.path.join(cross, 'b')).st_nlink == link_b, 'b link count changed'

# Directory <-> file exchange across parents must move one subdirectory.
os.makedirs(os.path.join(cross, 'p1', 'sub'))
os.makedirs(os.path.join(cross, 'p2'), exist_ok=True)
write(os.path.join(cross, 'p2', 'plain'), b'P' * 4096)
p1_before = os.stat(os.path.join(cross, 'p1')).st_nlink
p2_before = os.stat(os.path.join(cross, 'p2')).st_nlink
rn(os.path.join(cross, 'p1', 'sub'), os.path.join(cross, 'p2', 'plain'),
   RENAME_EXCHANGE)
assert os.stat(os.path.join(cross, 'p1')).st_nlink == p1_before - 1, \
    'p1 did not lose the subdirectory'
assert os.stat(os.path.join(cross, 'p2')).st_nlink == p2_before + 1, \
    'p2 did not gain the subdirectory'
assert os.path.isfile(os.path.join(cross, 'p1', 'sub')), 'p1/sub missing'
assert os.path.isdir(os.path.join(cross, 'p2', 'plain')), 'p2/plain missing'
assert os.stat(os.path.join(cross, 'p2', 'plain', '..')).st_ino == \
    os.stat(os.path.join(cross, 'p2')).st_ino, 'moved subdir .. is wrong'
PY
python3 /opt/fs-rename/checks.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
# The write boot must not verify and power off before the probe runs.
grep -q 'asterinas.fs_renameat2=write' /proc/cmdline && exit 0
echo "FS_RENAMEAT2_VERIFY_BEGIN"
python3 /opt/fs-rename/checks.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=renameat2 semantics probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-rename/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=renameat2 semantics verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-rename/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-rename\n'
    printf 'write %s /opt/fs-rename/checks.py\n' "$WORKSPACE/checks.py"
    printf 'write %s /opt/fs-rename/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-rename/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-rename/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-rename/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-renameat2-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-renameat2-probe.service /etc/systemd/system/fs-renameat2-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-renameat2-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-renameat2-verify.service /etc/systemd/system/fs-renameat2-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'renameat2: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_renameat2=write" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi

if ! grep -aq 'FS_RENAMEAT2_WRITE ok=True' "$WORKSPACE/write.txt"; then
    printf 'FAIL (renameat2 invariants); see %s\n' "$WORKSPACE/write.txt"
    grep -a 'FS_RENAMEAT2_PROBLEM' "$WORKSPACE/write.txt" | head -20 || true
    exit 1
fi

printf 'renameat2: verify boot (fresh mount)\n'
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

if ! grep -aq 'FS_RENAMEAT2_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (renameat2 invariants after remount); see %s\n' \
        "$WORKSPACE/verify.txt"
    grep -a 'FS_RENAMEAT2_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi

printf 'PASS (exchange, noreplace and error semantics survived a remount)\n'
rm -f -- "$IMAGE"
