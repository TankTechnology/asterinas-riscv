#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Sticky-bit gate for the ext2/ext4 implementation.
#
# A `1777` directory (the /tmp shape) must only let the directory owner, the
# entry owner or root remove an entry.  The probe creates the tree as root,
# forks a child that drops to uid 1000 and requires `EPERM` from `unlink` and
# `rmdir` (a non-empty directory reports `EPERM`, not `ENOTEMPTY`), requires
# the same child to remove a file it owns, and requires a plain `0777`
# directory to stay unaffected.  The second boot re-checks the tree after the
# remount, and the host ends with `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: fs_sticky_bit_check.sh --kernel ELF --initramfs CPIO --base-root IMG
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

Exit status is 0 only if the sticky-bit rules hold across a remount and
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
IMAGE="$WORKSPACE/sticky.ext2"
PROBE_SH="$WORKSPACE/probe.sh"
VERIFY_SH="$WORKSPACE/verify.sh"
CHECK_PY="$WORKSPACE/check.py"
PROBE_UNIT="$WORKSPACE/probe.service"
VERIFY_UNIT="$WORKSPACE/verify.service"

cat >"$CHECK_PY" <<'CHECKS'
import errno
import os
import sys

ROOT = '/opt/fs-sticky'
NON_OWNER = 1000


def drop_privileges():
    os.setresgid(NON_OWNER, NON_OWNER, NON_OWNER)
    os.setresuid(NON_OWNER, NON_OWNER, NON_OWNER)


def expect(name, function, wanted):
    try:
        function()
    except OSError as error:
        if error.errno == wanted:
            return
        raise AssertionError('%s raised errno %d, wanted %d'
                             % (name, error.errno, wanted))
    raise AssertionError('%s succeeded, wanted errno %d' % (name, wanted))


def run_probe():
    problems = []
    sticky = os.path.join(ROOT, 'sticky')
    plain = os.path.join(ROOT, 'plain')
    for directory in (sticky, plain):
        if os.path.isdir(directory):
            for child in os.listdir(directory):
                path = os.path.join(directory, child)
                if os.path.isdir(path):
                    os.rmdir(path)
                else:
                    os.unlink(path)
            os.rmdir(directory)

    os.mkdir(sticky)
    os.chmod(sticky, 0o1777)
    with open(os.path.join(sticky, 'rootfile'), 'w') as f:
        f.write('root')
    os.mkdir(os.path.join(sticky, 'emptydir'), 0o755)
    os.mkdir(os.path.join(sticky, 'nonemptydir'), 0o755)
    with open(os.path.join(sticky, 'nonemptydir', 'inner'), 'w') as f:
        f.write('inner')

    os.mkdir(plain)
    os.chmod(plain, 0o777)
    with open(os.path.join(plain, 'rootfile'), 'w') as f:
        f.write('plain')

    pid = os.fork()
    if pid == 0:
        status = 0
        try:
            drop_privileges()
            for name, function, wanted in (
                ('unlink owner file in 1777 dir',
                 lambda: os.unlink(os.path.join(sticky, 'rootfile')), errno.EPERM),
                ('rmdir empty dir in 1777 dir',
                 lambda: os.rmdir(os.path.join(sticky, 'emptydir')), errno.EPERM),
                ('rmdir non-empty dir in 1777 dir',
                 lambda: os.rmdir(os.path.join(sticky, 'nonemptydir')), errno.EPERM),
            ):
                try:
                    expect(name, function, wanted)
                except AssertionError as error:
                    print('STICKY_CHILD_PROBLEM %s' % error, flush=True)
                    status = 1
                    break
            if status == 0:
                own = os.path.join(sticky, 'ownfile')
                with open(own, 'w') as f:
                    f.write('own')
                try:
                    os.unlink(own)
                except OSError as error:
                    print('STICKY_CHILD_PROBLEM unlink of own file: %d'
                          % error.errno, flush=True)
                    status = 1
            if status == 0:
                try:
                    os.unlink(os.path.join(plain, 'rootfile'))
                except OSError as error:
                    print('STICKY_CHILD_PROBLEM unlink in 0777 dir: %d'
                          % error.errno, flush=True)
                    status = 1
        except BaseException as error:  # noqa: BLE001 - reported to the host
            print('STICKY_CHILD_PROBLEM %r' % (error,), flush=True)
            status = 1
        os._exit(status)

    _, child_status = os.waitpid(pid, 0)
    if not os.WIFEXITED(child_status) or os.WEXITSTATUS(child_status) != 0:
        problems.append('the non-owner child reported a failure')

    for entry in ('rootfile', 'emptydir', 'nonemptydir'):
        if not os.path.lexists(os.path.join(sticky, entry)):
            problems.append('%s disappeared although removal was refused' % entry)
    if os.path.lexists(os.path.join(plain, 'rootfile')):
        problems.append('a plain 0777 directory still holds rootfile')

    mode = os.stat(sticky).st_mode & 0o7777
    if mode != 0o1777:
        problems.append('sticky directory mode is %o' % mode)

    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open(os.path.join(ROOT, 'result.txt'), 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('FS_STICKY_WRITE %s' % result.strip(), flush=True)
    for problem in problems[:20]:
        print('FS_STICKY_PROBLEM %s' % problem, flush=True)
    return 0 if not problems else 1


def run_verify():
    problems = []
    sticky = os.path.join(ROOT, 'sticky')
    for entry in ('rootfile', 'emptydir', 'nonemptydir'):
        if not os.path.lexists(os.path.join(sticky, entry)):
            problems.append('%s is missing after the remount' % entry)
    mode = os.stat(sticky).st_mode & 0o7777
    if mode != 0o1777:
        problems.append('sticky directory mode is %o after the remount' % mode)
    try:
        with open(os.path.join(ROOT, 'result.txt')) as f:
            recorded = f.read().strip()
    except OSError as error:
        problems.append('result.txt is unreadable: %d' % error.errno)
        recorded = ''
    if 'ok=True' not in recorded:
        problems.append('the first boot did not report ok=True: %r' % recorded)
    result = 'ok=%s problems=%d\n' % (not problems, len(problems))
    with open(os.path.join(ROOT, 'verify.txt'), 'w') as f:
        f.write(result)
        f.flush()
        os.fsync(f.fileno())
    print('FS_STICKY_VERIFY %s' % result.strip(), flush=True)
    for problem in problems[:20]:
        print('FS_STICKY_PROBLEM %s' % problem, flush=True)
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(run_probe() if sys.argv[1] == 'write' else run_verify())
CHECKS

cat >"$PROBE_SH" <<PROBE
#!/bin/sh
grep -q 'asterinas.fs_sticky=run' /proc/cmdline || exit 0
echo "FS_STICKY_PROBE_BEGIN"
sleep $SETTLE
python3 /opt/fs-sticky/check.py write
sync
systemctl poweroff --no-block
PROBE

cat >"$VERIFY_SH" <<VERIFY
#!/bin/sh
grep -q 'asterinas.fs_sticky=run' /proc/cmdline && exit 0
echo "FS_STICKY_VERIFY_BEGIN"
python3 /opt/fs-sticky/check.py verify
sync
systemctl poweroff --no-block
VERIFY

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=Sticky-bit probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-sticky/probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

cat >"$VERIFY_UNIT" <<'UNIT'
[Unit]
Description=Sticky-bit verification
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/fs-sticky/verify.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'mkdir /opt/fs-sticky\n'
    printf 'write %s /opt/fs-sticky/check.py\n' "$CHECK_PY"
    printf 'write %s /opt/fs-sticky/probe.sh\n' "$PROBE_SH"
    printf 'sif /opt/fs-sticky/probe.sh mode 0100755\n'
    printf 'write %s /opt/fs-sticky/verify.sh\n' "$VERIFY_SH"
    printf 'sif /opt/fs-sticky/verify.sh mode 0100755\n'
    printf 'write %s /etc/systemd/system/fs-sticky-probe.service\n' "$PROBE_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-sticky-probe.service /etc/systemd/system/fs-sticky-probe.service\n'
    printf 'write %s /etc/systemd/system/fs-sticky-verify.service\n' "$VERIFY_UNIT"
    printf 'symlink /etc/systemd/system/multi-user.target.wants/fs-sticky-verify.service /etc/systemd/system/fs-sticky-verify.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s)\n' \
        "$WORKSPACE/fsck-prepared.txt"
    exit 1
fi

printf 'sticky: write boot\n'
if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/write.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" \
    --append-extra "asterinas.fs_sticky=run" \
    --wait-for "Powering off."; then
    printf 'FAIL (the write boot did not shut down; see %s)\n' \
        "$WORKSPACE/write.txt"
    exit 1
fi
if ! grep -aq 'FS_STICKY_WRITE ok=True' "$WORKSPACE/write.txt"; then
    printf 'FAIL (sticky rules); see %s\n' "$WORKSPACE/write.txt"
    grep -a 'FS_STICKY_PROBLEM\|FS_STICKY_CHILD_PROBLEM' "$WORKSPACE/write.txt" |
        head -20 || true
    exit 1
fi

printf 'sticky: verify boot (fresh mount)\n'
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
if ! grep -aq 'FS_STICKY_VERIFY ok=True' "$WORKSPACE/verify.txt"; then
    printf 'FAIL (verification after the remount); see %s\n' \
        "$WORKSPACE/verify.txt"
    grep -a 'FS_STICKY_PROBLEM' "$WORKSPACE/verify.txt" | head -20 || true
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s)\n' "$WORKSPACE/fsck-after.txt"
    exit 1
fi
rm -f -- "$IMAGE"

printf 'PASS (non-owner removal refused with EPERM, own entry removable, clean image)\n'
