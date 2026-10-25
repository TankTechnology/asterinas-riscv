#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Daily-use measurement gate for the x86_64 Debian image.
#
# Boots the image on the Asterinas kernel, logs in over the serial console,
# and runs `daily_use_checklist.sh` inside the guest. The checklist reports one
# line per domain (desktop+DRI3, application, apt, clipboard, input method,
# audio, USB hotplug, suspend/wake); the host answers its two WAIT markers by
# hot-adding a USB stick and waking the guest from suspend.
#
# This gate measures the current state. A `fail` or `missing` domain does not
# fail the run; the gate fails only when the checklist itself did not complete,
# so the matrix it prints is a faithful snapshot.
#
# `--popcon` swaps the checklist for the L1 package-ecosystem probe
# (`popcon_packages.sh`): the same QEMU boot, login and serial plumbing, but the
# guest installs the popcon top-50 with apt and prints one `POPCON pkg=` line
# per package. The host writes `<workspace>/popcon.tsv` and prints a summary.
#
# `--systemd-health` swaps it for the L2 probe (`systemd_health.sh`): the guest
# reports failed units, startup phases, blame, critical chain, unit verification
# and this boot's error journal as `SYSD ` lines; the host writes the tables in
# the workspace and prints a summary.
#
# `--ltp` swaps it for the L3 probe (`ltp_subset.sh`). LTP_PHASE=check reports
# feasibility as `LTPCHK ` lines; LTP_PHASE=run builds a bounded syscall subset
# in the guest and reports one `LTP case=` line per test.
#
# `--source-build` swaps it for the L4 probe (`dpkg_build.sh`): the guest
# installs each test package's Build-Depends, fetches its source with
# `apt-get source` and runs `dpkg-buildpackage -b -us -uc`, reporting one
# `DPKGB pkg=` line per stage and artifact. The host writes
# `<workspace>/source-build/` and prints a summary.
#
# `--debootstrap` swaps it for the L5 probe (`debootstrap_rootfs.sh`): the
# guest installs debootstrap, runs the `--foreign` first stage into /target and
# then `chroot /target /debootstrap/debootstrap --second-stage`, reporting one
# `DBST stage=` line per stage plus disk figures. The host writes
# `<workspace>/debootstrap/` and prints a summary.
#
# `--ext4-e2big` swaps it for the L5 minimal reproducer
# (`ext4_e2big_probe.sh`): ldconfig, a large data write, and metadata
# backlogs on the root ext2, looking for the E2BIG that aborted debootstrap.
# The host writes `<workspace>/ext4-e2big/`.
#
# `--full-upgrade` swaps it for the L6 probe (`full_upgrade.sh`): the image is
# copied first (both sha256 sums are recorded), the guest rewrites its apt
# sources from trixie to forky, runs update and full-upgrade, and the gate then
# boots that same image a second time to check systemd, serial login and
# /etc/debian_version. The host writes `<workspace>/upgrade/`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: daily_use_gate.sh --kernel ELF --initramfs CPIO --base-root IMG
                         --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy for the run (required).
  --workspace DIR     Image, checklist, transcripts and the matrix.
  --timeout SECONDS   Deadline for login, checklist and power-off.
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).
  --popcon            Run the L1 package probe instead of the daily checklist.
  --popcon-list PATH  Package list for --popcon
                      (default: tools/x86/debian/popcon_top50.txt).
  --systemd-health    Run the L2 systemd health probe instead of the
                      daily checklist.
  --ltp               Run the L3 LTP probe instead of the daily checklist.
                      LTP_PHASE=check (default) reports feasibility;
                      LTP_PHASE=run builds and runs the bounded subset.
  --source-build      Run the L4 source-package probe instead of the daily
                      checklist: build hello, coreutils and python3-minimal
                      with dpkg-buildpackage inside the guest.
  --source-build-packages LIST
                      Override the L4 package list (space separated); default
                      is the probe's own "hello coreutils python3-minimal".
  --source-build-selftest
                      L4 only: report m4/grep/autom4te sanity checks instead of
                      building, to attribute a toolchain-level failure.
  --debootstrap       Run the L5 probe instead of the daily checklist: build a
                      minbase trixie rootfs in /target and run the second stage
                      through chroot.
  --debootstrap-target PATH
                      L5 only: absolute directory to build into (default
                      /target, on the root ext2). /tmp/target puts it on tmpfs.
  --ext4-e2big        Run the L5 E2BIG reproducer on the root ext2 instead of
                      the daily checklist.
  --full-upgrade      Run the L6 trixie -> forky full-upgrade on an image copy,
                      then reboot that copy to check systemd and login.

The guest account is `asterinas` (uid 1000, password `asterinas`, passwordless
sudo). Results land in `<workspace>/matrix.tsv` (checklist),
`<workspace>/popcon.tsv` (--popcon), `<workspace>/systemd-health/`
(--systemd-health), `<workspace>/ltp/` (--ltp) or
`<workspace>/source-build/` (--source-build), `<workspace>/debootstrap/`
(--debootstrap), `<workspace>/ext4-e2big/` (--ext4-e2big),
`<workspace>/upgrade/` (--full-upgrade) and on stdout.
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
TIMEOUT=""
ACCEL="kvm"
QEMU="qemu-system-x86_64"
POPCON=0
POPCON_LIST=""
SYSTEMD_HEALTH=0
LTP=0
LTP_PHASE="${LTP_PHASE:-check}"
SOURCE_BUILD=0
SOURCE_BUILD_PKGS=""
SOURCE_BUILD_SELFTEST=0
DEBOOTSTRAP=0
DEBOOTSTRAP_TARGET=""
EXT4_E2BIG=0
FULL_UPGRADE=0

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --timeout | \
            --accel | --qemu | --popcon-list | --source-build-packages | \
            --debootstrap-target)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --accel) ACCEL="$2" ;;
                --qemu) QEMU="$2" ;;
                --popcon-list) POPCON_LIST="$2" ;;
                --source-build-packages) SOURCE_BUILD_PKGS="$2" ;;
                --debootstrap-target) DEBOOTSTRAP_TARGET="$2" ;;
            esac
            shift 2
            ;;
        --popcon) POPCON=1; shift ;;
        --systemd-health) SYSTEMD_HEALTH=1; shift ;;
        --ltp) LTP=1; shift ;;
        --source-build) SOURCE_BUILD=1; shift ;;
        --source-build-selftest) SOURCE_BUILD_SELFTEST=1; shift ;;
        --debootstrap) DEBOOTSTRAP=1; shift ;;
        --ext4-e2big) EXT4_E2BIG=1; shift ;;
        --full-upgrade) FULL_UPGRADE=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

(( POPCON + SYSTEMD_HEALTH + LTP + SOURCE_BUILD + DEBOOTSTRAP + EXT4_E2BIG + FULL_UPGRADE > 1 )) &&
    die_usage "--popcon, --systemd-health, --ltp, --source-build, --debootstrap, --ext4-e2big and --full-upgrade are mutually exclusive"
if (( LTP )); then
    [[ "$LTP_PHASE" == "check" || "$LTP_PHASE" == "run" ]] ||
        die_usage "LTP_PHASE must be 'check' or 'run'"
fi

# The package probe runs 50 apt transactions and the LTP run compiles a subset;
# give both a longer default deadline than the checklist.
if [[ -z "$TIMEOUT" ]]; then
    if (( POPCON )); then
        TIMEOUT=3600
    elif (( EXT4_E2BIG )); then
        TIMEOUT=1200
    elif (( FULL_UPGRADE )); then
        TIMEOUT=4200
    elif (( SOURCE_BUILD )) || (( DEBOOTSTRAP )); then
        TIMEOUT=4200
    elif (( LTP )) && [[ "$LTP_PHASE" == "run" ]]; then
        TIMEOUT=1800
    else
        TIMEOUT=900
    fi
fi
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
# The package list is interpolated into the guest's shell command; keep it to
# what a Debian package name (and the separating spaces) can contain.
[[ -z "$SOURCE_BUILD_PKGS" || "$SOURCE_BUILD_PKGS" =~ ^[A-Za-z0-9.+:-]+(\ [A-Za-z0-9.+:-]+)*$ ]] ||
    die_usage "--source-build-packages must be space-separated package names"
# The target is interpolated into the guest command; require an absolute path
# made of path characters. The probe itself additionally rejects "/".
[[ -z "$DEBOOTSTRAP_TARGET" || "$DEBOOTSTRAP_TARGET" =~ ^/[A-Za-z0-9._/-]+$ ]] ||
    die_usage "--debootstrap-target must be an absolute path"

[[ -n "$KERNEL" && -n "$INITRAMFS" && -n "$BASE_ROOT" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --initramfs, --base-root and --workspace are required"
for path in "$KERNEL" "$INITRAMFS" "$BASE_ROOT"; do
    [[ -f "$path" ]] || die_usage "input does not exist: $path"
done
[[ "$ACCEL" == "kvm" || "$ACCEL" == "tcg" ]] ||
    die_usage "--accel must be 'kvm' or 'tcg'"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
BOOT_SCRIPT="$REPO_ROOT/tools/x86/debian/boot_debian.sh"
CHECKLIST="$REPO_ROOT/tools/x86/debian/daily_use_checklist.sh"
POPCON_SCRIPT="$REPO_ROOT/tools/x86/debian/popcon_packages.sh"
SYSTEMD_HEALTH_SCRIPT="$REPO_ROOT/tools/x86/debian/systemd_health.sh"
LTP_SCRIPT="$REPO_ROOT/tools/x86/debian/ltp_subset.sh"
SOURCE_BUILD_SCRIPT="$REPO_ROOT/tools/x86/debian/dpkg_build.sh"
DEBOOTSTRAP_SCRIPT="$REPO_ROOT/tools/x86/debian/debootstrap_rootfs.sh"
EXT4_E2BIG_SCRIPT="$REPO_ROOT/tools/x86/debian/ext4_e2big_probe.sh"
FULL_UPGRADE_SCRIPT="$REPO_ROOT/tools/x86/debian/full_upgrade.sh"
[[ -n "$POPCON_LIST" ]] || POPCON_LIST="$REPO_ROOT/tools/x86/debian/popcon_top50.txt"
MSGTEST_SRC="$REPO_ROOT/tools/x86/debian/msgtest.c"
IOTEST_SRC="$REPO_ROOT/tools/x86/debian/iotest.c"
if (( POPCON )); then
    for path in "$POPCON_SCRIPT" "$POPCON_LIST"; do
        [[ -f "$path" ]] || die_usage "popcon input does not exist: $path"
    done
fi
if (( SYSTEMD_HEALTH )); then
    [[ -f "$SYSTEMD_HEALTH_SCRIPT" ]] ||
        die_usage "systemd probe does not exist: $SYSTEMD_HEALTH_SCRIPT"
fi
if (( LTP )); then
    [[ -f "$LTP_SCRIPT" ]] || die_usage "LTP probe does not exist: $LTP_SCRIPT"
fi
if (( SOURCE_BUILD )); then
    [[ -f "$SOURCE_BUILD_SCRIPT" ]] ||
        die_usage "source-build probe does not exist: $SOURCE_BUILD_SCRIPT"
fi
if (( SOURCE_BUILD_SELFTEST )) && (( ! SOURCE_BUILD )); then
    die_usage "--source-build-selftest requires --source-build"
fi
if (( DEBOOTSTRAP )); then
    [[ -f "$DEBOOTSTRAP_SCRIPT" ]] ||
        die_usage "debootstrap probe does not exist: $DEBOOTSTRAP_SCRIPT"
fi
if (( EXT4_E2BIG )); then
    [[ -f "$EXT4_E2BIG_SCRIPT" ]] ||
        die_usage "E2BIG probe does not exist: $EXT4_E2BIG_SCRIPT"
fi
if (( FULL_UPGRADE )); then
    [[ -f "$FULL_UPGRADE_SCRIPT" ]] ||
        die_usage "full-upgrade probe does not exist: $FULL_UPGRADE_SCRIPT"
fi
for tool in debugfs e2fsck mke2fs python3 timeout "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done

mkdir -p -- "$WORKSPACE"
IMAGE="$WORKSPACE/daily.ext2"
USB_IMAGE="$WORKSPACE/usbstick.img"
WRAPPER="$WORKSPACE/qemu-daily.sh"
CLIENT="$WORKSPACE/daily_client.py"
CONSOLE_LOG="$WORKSPACE/console.txt"
SCREENSHOT="$WORKSPACE/screen.ppm"
SERIAL_SOCK="$WORKSPACE/serial.sock"
MONITOR_SOCK="$WORKSPACE/monitor.sock"
PIDFILE="$WORKSPACE/qemu.pid"
MATRIX="$WORKSPACE/matrix.tsv"
POPCON_TSV="$WORKSPACE/popcon.tsv"
POPCON_JSON="$WORKSPACE/popcon-result.json"
POPCON_GUEST_LIST="/usr/local/share/$(basename -- "$POPCON_LIST")"
SYSTEMD_HEALTH_DIR="$WORKSPACE/systemd-health"
LTP_DIR="$WORKSPACE/ltp"
LTP_TSV="$LTP_DIR/ltp.tsv"
LTP_JSON="$LTP_DIR/ltp.json"
SOURCE_BUILD_DIR="$WORKSPACE/source-build"
SOURCE_BUILD_TSV="$SOURCE_BUILD_DIR/packages.tsv"
SOURCE_BUILD_JSON="$SOURCE_BUILD_DIR/source-build.json"
DEBOOTSTRAP_DIR="$WORKSPACE/debootstrap"
DEBOOTSTRAP_TSV="$DEBOOTSTRAP_DIR/stages.tsv"
DEBOOTSTRAP_JSON="$DEBOOTSTRAP_DIR/debootstrap.json"
EXT4_E2BIG_DIR="$WORKSPACE/ext4-e2big"
EXT4_E2BIG_TSV="$EXT4_E2BIG_DIR/steps.tsv"
EXT4_E2BIG_JSON="$EXT4_E2BIG_DIR/ext4-e2big.json"
FULL_UPGRADE_DIR="$WORKSPACE/upgrade"
FULL_UPGRADE_TSV="$FULL_UPGRADE_DIR/stages.tsv"
FULL_UPGRADE_JSON="$FULL_UPGRADE_DIR/upgrade.json"
REBOOT_CONSOLE="$WORKSPACE/reboot-console.txt"

# The X server configuration the checklist starts with (same options as the
# Xorg gate: no udev, no automatic input devices).
XORG_CONF="$WORKSPACE/xorg-noudev.conf"
MSGTEST_BIN="$WORKSPACE/msgtest"
IOTEST_BIN="$WORKSPACE/iotest"
cat >"$XORG_CONF" <<'CONF'
Section "ServerFlags"
    Option "AutoAddDevices" "false"
    Option "AutoEnableDevices" "false"
EndSection
CONF

# The socket/wait unit checks are compiled on the host and injected as a
# static binary, since the image has no compiler.
if command -v cc >/dev/null 2>&1; then
    cc -static -O2 -o "$MSGTEST_BIN" "$MSGTEST_SRC" 2>>"$WORKSPACE/build.log" || rm -f "$MSGTEST_BIN"
    cc -static -O2 -o "$IOTEST_BIN" "$IOTEST_SRC" 2>>"$WORKSPACE/build.log" || rm -f "$IOTEST_BIN"
fi

# An ext2 stick image with one file: the guest must mount and read it.
USB_ROOT="$WORKSPACE/usbstick-root"
rm -rf -- "$USB_ROOT"
mkdir -p -- "$USB_ROOT"
printf 'ASTERINAS-USB-OK\n' >"$USB_ROOT/hello.txt"
truncate -s 16M "$USB_IMAGE"
# The kernel ext2 driver needs 4 KiB blocks and has no ext_attr, dir_index,
# or resize inode.
mke2fs -q -F -t ext2 -b 4096 -O ^ext_attr,^resize_inode,^dir_index -d "$USB_ROOT" "$USB_IMAGE"

cat >"$WRAPPER" <<WRAPPER
#!/usr/bin/env bash
args=()
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        -chardev)
            case "\$2" in
                stdio,id=mux*) shift 2; continue ;;
            esac
            args+=("\$1" "\$2"); shift 2 ;;
        -device)
            case "\$2" in
                virtio-serial-pci|virtconsole,chardev=mux) shift 2; continue ;;
            esac
            args+=("\$1" "\$2"); shift 2 ;;
        -serial | -monitor) shift 2 ;;
        -append)
            args+=("\$1" "\${2/console=hvc0/console=ttyS0 earlycon}"); shift 2 ;;
        *)
            args+=("\$1"); shift ;;
    esac
done
printf '%s\n' "\$\$" >"$PIDFILE"
exec "$QEMU" "\${args[@]}" -vga none -device virtio-gpu-pci \\
    -device intel-hda -device hda-output,audiodev=audio0 \\
    -audiodev none,id=audio0 \\
    -device qemu-xhci -drive "if=none,id=usbstick,format=raw,file=$USB_IMAGE" \\
    -serial "unix:$SERIAL_SOCK,server=on,wait=off" \\
    -monitor "unix:$MONITOR_SOCK,server=on,wait=off"
WRAPPER
chmod +x "$WRAPPER"

cat >"$CLIENT" <<'PY'
import os
import socket
import sys
import time

(console_path, serial_path, monitor_path, pid_path, shot_path, timeout,
 command, done_marker, mode) = sys.argv[1:10]
timeout = float(timeout)
deadline = time.time() + timeout
done_marker = done_marker.encode()

monitor = None


def connect(path, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.connect(path)
            return sock
        except OSError:
            time.sleep(0.05)
    return None


serial = connect(serial_path, 60)
if serial is None:
    print('DAILY_CLIENT no-serial', flush=True)
    sys.exit(2)
serial.settimeout(0.5)

buffer = b''
sent_user = sent_pass = sent_command = done = False
answered_usb = answered_suspend = shot = False
with open(console_path, 'wb') as out:
    while time.time() < deadline:
        try:
            chunk = serial.recv(4096)
        except socket.timeout:
            chunk = b''
        if chunk:
            buffer += chunk
            out.write(chunk)
            out.flush()

        if not sent_user and b'login:' in buffer:
            serial.sendall(b'asterinas\n')
            sent_user = True
        if sent_user and not sent_pass and b'Password:' in buffer:
            serial.sendall(b'asterinas\n')
            sent_pass = True
        if sent_pass and not sent_command and b'asterinas@' in buffer:
            time.sleep(0.2)
            serial.sendall((command + '\n').encode())
            sent_command = True

        if sent_command and monitor is None:
            monitor = connect(monitor_path, 5)
        if monitor is not None and mode == 'checklist':
            if not answered_usb and b'DAILY_USB_WAIT' in buffer:
                monitor.sendall(b'device_add usb-storage,drive=usbstick\n')
                answered_usb = True
            if not answered_suspend and b'DAILY_SUSPEND_WAIT' in buffer:
                time.sleep(3)
                monitor.sendall(b'system_wakeup\n')
                answered_suspend = True
            if not shot and b'DAILY_DESKTOP=' in buffer:
                time.sleep(1)
                monitor.sendall(('screendump %s\n' % shot_path).encode())
                shot = True

        # The shell echoes the typed command, so the completion marker must be
        # the checklist's (or probe's) own last line.
        if sent_command and done_marker in buffer and not done:
            if monitor is not None:
                monitor.sendall(b'system_powerdown\n')
            done = True

        if done:
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
            except (OSError, ValueError):
                break

print('DAILY_CLIENT user=%s pass=%s command=%s usb=%s suspend=%s done=%s'
      % (sent_user, sent_pass, sent_command, answered_usb, answered_suspend,
         done), flush=True)
ok = sent_command and done and done_marker in buffer
sys.exit(0 if ok else 1)
PY

# What the guest runs after login, and the line that ends the session.
if (( POPCON )); then
    GUEST_COMMAND="sudo sh -c '/usr/local/bin/popcon-packages --list $POPCON_GUEST_LIST > /dev/console 2>&1'; echo POPCON_SHELL_END"
    DONE_MARKER="POPCON_DONE count="
    MODE="popcon"
elif (( SYSTEMD_HEALTH )); then
    GUEST_COMMAND="sudo sh -c '/usr/local/bin/systemd-health > /dev/console 2>&1'; echo SYSD_SHELL_END"
    DONE_MARKER="SYSD_DONE"
    MODE="systemd-health"
elif (( LTP )); then
    GUEST_COMMAND="sudo sh -c 'LTP_PHASE=$LTP_PHASE /usr/local/bin/ltp-subset > /dev/console 2>&1'; echo LTP_SHELL_END"
    DONE_MARKER="LTP_DONE"
    MODE="ltp"
elif (( SOURCE_BUILD )); then
    GUEST_COMMAND="sudo sh -c 'DPKGB_PKGS=\"$SOURCE_BUILD_PKGS\" DPKGB_SELFTEST=$SOURCE_BUILD_SELFTEST /usr/local/bin/dpkg-build > /dev/console 2>&1'; echo DPKGB_SHELL_END"
    DONE_MARKER="DPKGB_DONE"
    MODE="source-build"
elif (( DEBOOTSTRAP )); then
    GUEST_COMMAND="sudo sh -c 'DBST_TARGET=\"$DEBOOTSTRAP_TARGET\" /usr/local/bin/debootstrap-rootfs > /dev/console 2>&1'; echo DBST_SHELL_END"
    DONE_MARKER="DBST_DONE"
    MODE="debootstrap"
elif (( EXT4_E2BIG )); then
    GUEST_COMMAND="sudo sh -c '/usr/local/bin/ext4-e2big-probe > /dev/console 2>&1'; echo E2BIG_SHELL_END"
    DONE_MARKER="E2BIG_DONE"
    MODE="ext4-e2big"
elif (( FULL_UPGRADE )); then
    GUEST_COMMAND="sudo sh -c '/usr/local/bin/full-upgrade > /dev/console 2>&1'; echo UPG_SHELL_END"
    DONE_MARKER="UPG_DONE"
    MODE="full-upgrade"
else
    GUEST_COMMAND="sudo sh -c '/usr/local/bin/daily-use-checklist > /dev/console 2>&1'; echo DAILY_SHELL_END"
    DONE_MARKER="DAILY_DONE=ok"
    MODE="checklist"
fi

rm -f -- "$IMAGE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$PIDFILE" "$CONSOLE_LOG" \
    "$SCREENSHOT" "$MATRIX" "$POPCON_TSV" "$POPCON_JSON" "$LTP_TSV" "$LTP_JSON" \
    "$SOURCE_BUILD_TSV" "$SOURCE_BUILD_JSON" \
    "$DEBOOTSTRAP_TSV" "$DEBOOTSTRAP_JSON" \
    "$EXT4_E2BIG_TSV" "$EXT4_E2BIG_JSON" \
    "$FULL_UPGRADE_TSV" "$FULL_UPGRADE_JSON" "$REBOOT_CONSOLE"
copy_t0=$(cut -d' ' -f1 /proc/uptime)
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
if (( FULL_UPGRADE )); then
    # The base image is never touched. Record both sums before anything can
    # write to the copy, and keep them in the workspace.
    copy_dur=$(awk -v a="$copy_t0" -v b="$(cut -d' ' -f1 /proc/uptime)" \
        'BEGIN { printf "%.2f", b - a }')
    base_sha=$(sha256sum -- "$BASE_ROOT" | awk '{ print $1 }')
    copy_sha=$(sha256sum -- "$IMAGE" | awk '{ print $1 }')
    mkdir -p "$FULL_UPGRADE_DIR"
    printf 'base_path=%s\nbase_sha256=%s\ncopy_path=%s\ncopy_sha256=%s\ncopy_dur=%s\n' \
        "$BASE_ROOT" "$base_sha" "$IMAGE" "$copy_sha" "$copy_dur" \
        >"$FULL_UPGRADE_DIR/copy.txt"
    printf 'UPG stage=copy rc=0 dur=%s sha_base=%s sha_copy=%s\n' \
        "$copy_dur" "$base_sha" "$copy_sha"
fi
{
    # Retire units an earlier gate left enabled in the base image.
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/xorgprobe.service\n'
    printf 'rm /etc/systemd/system/xorgprobe.service\n'
    # The daily checklist starts its own Xorg on :0 with the gate's config.
    # A desktop image that also boots a session service would take :0 first
    # and leave the checklist talking to a server it did not configure (or
    # fail to start one at all), so retire those units too.
    printf 'rm /etc/systemd/system/graphical.target.wants/asterinas-desktop-drm.service\n'
    printf 'rm /etc/systemd/system/graphical.target.wants/asterinas-desktop-drm-evidence.service\n'
    printf 'rm /etc/systemd/system/asterinas-desktop-drm.service\n'
    printf 'rm /etc/systemd/system/asterinas-desktop-drm-evidence.service\n'
    # debugfs `write` refuses to replace an existing file, so a base image that
    # already carries a probe would silently keep the stale copy. Remove each
    # target first; `rm` on an absent path is harmless.
    printf 'rm /usr/local/bin/daily-use-checklist\n'
    printf 'rm /etc/X11/xorg-noudev.conf\n'
    printf 'write %s /usr/local/bin/daily-use-checklist\n' "$CHECKLIST"
    printf 'sif /usr/local/bin/daily-use-checklist mode 0100755\n'
    printf 'write %s /etc/X11/xorg-noudev.conf\n' "$XORG_CONF"
    if (( POPCON )); then
        printf 'rm /usr/local/bin/popcon-packages\n'
        printf 'rm %s\n' "$POPCON_GUEST_LIST"
        printf 'write %s /usr/local/bin/popcon-packages\n' "$POPCON_SCRIPT"
        printf 'sif /usr/local/bin/popcon-packages mode 0100755\n'
        printf 'write %s %s\n' "$POPCON_LIST" "$POPCON_GUEST_LIST"
        printf 'sif %s mode 0100644\n' "$POPCON_GUEST_LIST"
    fi
    if (( SYSTEMD_HEALTH )); then
        printf 'rm /usr/local/bin/systemd-health\n'
        printf 'write %s /usr/local/bin/systemd-health\n' "$SYSTEMD_HEALTH_SCRIPT"
        printf 'sif /usr/local/bin/systemd-health mode 0100755\n'
    fi
    if (( LTP )); then
        printf 'rm /usr/local/bin/ltp-subset\n'
        printf 'write %s /usr/local/bin/ltp-subset\n' "$LTP_SCRIPT"
        printf 'sif /usr/local/bin/ltp-subset mode 0100755\n'
    fi
    if (( SOURCE_BUILD )); then
        printf 'rm /usr/local/bin/dpkg-build\n'
        printf 'write %s /usr/local/bin/dpkg-build\n' "$SOURCE_BUILD_SCRIPT"
        printf 'sif /usr/local/bin/dpkg-build mode 0100755\n'
    fi
    if (( DEBOOTSTRAP )); then
        printf 'rm /usr/local/bin/debootstrap-rootfs\n'
        printf 'write %s /usr/local/bin/debootstrap-rootfs\n' "$DEBOOTSTRAP_SCRIPT"
        printf 'sif /usr/local/bin/debootstrap-rootfs mode 0100755\n'
    fi
    if (( EXT4_E2BIG )); then
        printf 'rm /usr/local/bin/ext4-e2big-probe\n'
        printf 'write %s /usr/local/bin/ext4-e2big-probe\n' "$EXT4_E2BIG_SCRIPT"
        printf 'sif /usr/local/bin/ext4-e2big-probe mode 0100755\n'
    fi
    if (( FULL_UPGRADE )); then
        printf 'rm /usr/local/bin/full-upgrade\n'
        printf 'write %s /usr/local/bin/full-upgrade\n' "$FULL_UPGRADE_SCRIPT"
        printf 'sif /usr/local/bin/full-upgrade mode 0100755\n'
    fi
    if [[ -f "$MSGTEST_BIN" ]]; then
        printf 'rm /usr/local/bin/msgtest\n'
        printf 'write %s /usr/local/bin/msgtest\n' "$MSGTEST_BIN"
        printf 'sif /usr/local/bin/msgtest mode 0100755\n'
    fi
    if [[ -f "$IOTEST_BIN" ]]; then
        printf 'rm /usr/local/bin/iotest\n'
        printf 'write %s /usr/local/bin/iotest\n' "$IOTEST_BIN"
        printf 'sif /usr/local/bin/iotest mode 0100755\n'
    fi
} | debugfs -w -f - "$IMAGE" >"$WORKSPACE/inject.log" 2>&1
# A refused write would leave the image with an older probe than the one this
# checkout carries, so treat it as a gate failure rather than debugging a
# phantom.
if grep -q 'Ext2 file already exists' "$WORKSPACE/inject.log"; then
    printf 'FAIL (an injection target already existed; see %s/inject.log)\n' "$WORKSPACE"
    exit 1
fi
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' "$WORKSPACE"
    exit 1
fi

timeout "$((TIMEOUT + 30))" python3 "$CLIENT" "$CONSOLE_LOG" "$SERIAL_SOCK" \
    "$MONITOR_SOCK" "$PIDFILE" "$SCREENSHOT" "$TIMEOUT" \
    "$GUEST_COMMAND" "$DONE_MARKER" "$MODE" &
CLIENT_PID=$!

set +e
timeout "$((TIMEOUT + 60))" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/boot.txt" \
    --loglevel 4 \
    --accel "$ACCEL" \
    --qemu "$WRAPPER" < /dev/null >"$WORKSPACE/boot.log" 2>&1 &
BOOT_PID=$!

wait "$CLIENT_PID"
CLIENT_STATUS=$?
kill "$BOOT_PID" 2>/dev/null
wait "$BOOT_PID" 2>/dev/null
set -e

if (( FULL_UPGRADE )); then
    # Stage 5: boot the *upgraded copy* a second time and check systemd, serial
    # login and /etc/debian_version. This runs even when the first boot did not
    # reach UPG_DONE, because "the upgraded image no longer boots" is itself a
    # result worth recording.
    DONE_SEEN=0
    if grep -aq -- "$DONE_MARKER" "$CONSOLE_LOG"; then
        DONE_SEEN=1
    fi

    # Split the marker with quotes so the shell echoes the command as
    # "UPG""_REBOOT_DONE" while the output contains the contiguous marker. A
    # literal marker in the echoed command would let the client stop before
    # the check actually runs.
    # sudo: the client logs in as `asterinas`; a plain `systemctl` cannot reach
    # the system bus from that account here.
    REBOOT_COMMAND='v=$(cat /etc/debian_version 2>/dev/null); s=$(sudo systemctl is-system-running 2>&1); echo "UPG""_REBOOT debian_version=$v state=$s"; echo "UPG""_REBOOT_DONE"'
    rm -f -- "$REBOOT_CONSOLE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$PIDFILE"
    reboot_t0=$(cut -d' ' -f1 /proc/uptime)
    set +e
    timeout 660 python3 "$CLIENT" "$REBOOT_CONSOLE" "$SERIAL_SOCK" \
        "$MONITOR_SOCK" "$PIDFILE" "$SCREENSHOT" 600 \
        "$REBOOT_COMMAND" "UPG_REBOOT_DONE" "reboot" &
    REBOOT_CLIENT_PID=$!
    timeout 720 "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$WORKSPACE/boot-reboot.txt" \
        --loglevel 4 \
        --accel "$ACCEL" \
        --qemu "$WRAPPER" < /dev/null >"$WORKSPACE/boot-reboot.log" 2>&1 &
    REBOOT_BOOT_PID=$!
    wait "$REBOOT_CLIENT_PID"
    REBOOT_STATUS=$?
    kill "$REBOOT_BOOT_PID" 2>/dev/null
    wait "$REBOOT_BOOT_PID" 2>/dev/null
    set -e
    reboot_dur=$(awk -v a="$reboot_t0" -v b="$(cut -d' ' -f1 /proc/uptime)" \
        'BEGIN { printf "%.2f", b - a }')

    mkdir -p -- "$FULL_UPGRADE_DIR"
    python3 - "$CONSOLE_LOG" "$REBOOT_CONSOLE" "$FULL_UPGRADE_DIR" \
        "$FULL_UPGRADE_DIR/copy.txt" "$CLIENT_STATUS" "$REBOOT_STATUS" \
        "$DONE_SEEN" "$reboot_dur" <<'PY'
import json
import os
import re
import sys

(console, reboot_console, outdir, copy_path, client_status, reboot_status,
 done_seen, reboot_dur) = sys.argv[1:9]


def read(path):
    if not os.path.exists(path):
        return ""
    text = open(path, "rb").read().decode("utf-8", "replace")
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


text = read(console)
rtext = read(reboot_console)

env = {}
for m in re.finditer(r"UPG env ([A-Za-z0-9_]+)=([^\r\n]*)", text):
    env[m.group(1)] = m.group(2).strip()
env["debian_after"] = env.get("debian_after", "")

disk = {}
for m in re.finditer(r"UPG disk_free_kb=(\d+) stage=(\S+) phase=(\S+)", text):
    disk[(m.group(2), m.group(3))] = int(m.group(1))

stages = {}
for m in re.finditer(
        r"UPG stage=(source|update|upgrade) rc=(-?\d+) dur=([0-9.]+)", text):
    stages[m.group(1)] = {"stage": m.group(1), "rc": int(m.group(2)),
                          "dur": float(m.group(3))}

sources = [m.group(1).strip() for m in
           re.finditer(r"UPG source line=([^\r\n]*)", text)]
errors = [m.group(1).strip() for m in
          re.finditer(r"UPG err=([^\r\n]*)", text)]
logs = [m.group(1).strip() for m in
        re.finditer(r"UPG log upgrade=([^\r\n]*)", text)]
m = re.search(r"UPG update upgradable=(\d+)", text)
upgradable = int(m.group(1)) if m else ""
m = re.search(r"UPG upgrade summary=([^\r\n]*)", text)
upgrade_summary = m.group(1).strip() if m else ""

copy = {}
if os.path.exists(copy_path):
    for line in open(copy_path):
        if "=" in line:
            key, value = line.rstrip("\n").split("=", 1)
            copy[key] = value

m = re.search(r"UPG_REBOOT debian_version=([^\r\n]*)", rtext)
reboot_version = m.group(1).strip() if m else ""
m = re.search(r"UPG_REBOOT state=([^\r\n]*)", rtext)
reboot_state = m.group(1).strip() if m else ""
# The client's DAILY_CLIENT line goes to the gate's stdout, not the serial;
# the client's exit status already encodes login + command + marker.
reboot_done = "UPG_REBOOT_DONE" in rtext
login_ok = str(reboot_status) == "0"
reboot_rc = 0 if (reboot_done and login_ok) else 1
login = {"client_status": int(reboot_status), "marker": reboot_done}

err_text = " ".join(errors).lower().replace("_", " ")


def category(row):
    if row["rc"] == 0:
        return "ok"
    if row["rc"] == 124:
        return "timeout"
    if row["stage"] == "reboot":
        return "boot-failure"
    e = err_text
    if any(t in e for t in ("disk low", "no space", "enospc",
                            "argument list too long")):
        return "disk"
    if any(t in e for t in ("unmet dependencies", "not installable",
                            "broken packages", "depends")):
        return "dependency"
    if any(t in e for t in ("dpkg: error", "conflict",
                            "trying to overwrite", "conffile")):
        return "dpkg-conflict"
    if any(t in e for t in ("function not implemented", "enotsup",
                            "operation not supported", "unimplemented")):
        return "syscall-missing"
    return "other"


rows = []
copy_ok = bool(copy.get("copy_sha256"))
rows.append({
    "stage": "copy", "rc": 0 if copy_ok else -1,
    "dur": float(copy.get("copy_dur", 0)),
    "free_before": "", "free_after": "",
    "detail": "base_sha256=%s copy_sha256=%s" % (
        copy.get("base_sha256", ""), copy.get("copy_sha256", "")),
})
for name in ("source", "update", "upgrade"):
    rec = stages.get(name)
    if rec is None:
        rows.append({"stage": name, "rc": -1, "dur": 0.0,
                     "free_before": "", "free_after": "",
                     "detail": "missing"})
        continue
    rec = dict(rec)
    rec["free_before"] = disk.get((name, "before"), "")
    rec["free_after"] = disk.get((name, "after"), "")
    if name == "update":
        rec["detail"] = "upgradable=%s" % upgradable
    elif name == "upgrade":
        rec["detail"] = upgrade_summary
    else:
        rec["detail"] = sources[0] if sources else ""
    rows.append(rec)
rows.append({
    "stage": "reboot", "rc": reboot_rc, "dur": float(reboot_dur),
    "free_before": "", "free_after": "",
    "detail": "debian_version=%s state=%s login=%s" % (
        reboot_version, reboot_state, login_ok),
})
for row in rows:
    row["category"] = category(row)

with open(os.path.join(outdir, "stages.tsv"), "w") as out:
    out.write("stage\trc\tcategory\tdur\tfree_before_kb\tfree_after_kb\t"
              "detail\n")
    for row in rows:
        out.write("%s\t%d\t%s\t%.2f\t%s\t%s\t%s\n" % (
            row["stage"], row["rc"], row["category"], row["dur"],
            row["free_before"], row["free_after"], row["detail"]))
with open(os.path.join(outdir, "upgrade.json"), "w") as out:
    json.dump({
        "env": env, "copy": copy, "stages": rows,
        "source_lines": sources, "upgradable": upgradable,
        "upgrade_summary": upgrade_summary, "errors": errors,
        "upgrade_log_tail": logs,
        "probe_done": str(done_seen) == "1",
        "first_client_status": int(client_status),
        "reboot": {"debian_version": reboot_version,
                   "state": reboot_state, "login": login,
                   "marker": reboot_done,
                   "client_status": int(reboot_status),
                   "dur": float(reboot_dur)},
    }, out, indent=2)
    out.write("\n")

ok = sum(1 for row in rows if row["rc"] == 0)
print("UPGRADE stages=%d ok=%d fail=%d"
      % (len(rows), ok, len(rows) - ok))
for row in rows:
    print("  stage=%-7s rc=%-3d %-13s dur=%6.2fs %s"
          % (row["stage"], row["rc"], row["category"], row["dur"],
             row["detail"][:90]))
if upgrade_summary:
    print("  upgrade summary: %s" % upgrade_summary)
if errors:
    for msg in errors:
        print("  err=%s" % msg)
for msg in logs:
    print("  log %s" % msg)
print("  sha base=%s" % copy.get("base_sha256", ""))
print("  sha copy=%s" % copy.get("copy_sha256", ""))
print("  reboot debian_version=%s state=%s login=%s"
      % (reboot_version, reboot_state, login_ok))
if not stages:
    print("FAIL (no guest UPG stage line; see %s)" % console)
    raise SystemExit(1)
PY
    printf 'UPGRADE (tables written to %s)\n' "$FULL_UPGRADE_DIR"
    exit 0
fi

if ! grep -aq -- "$DONE_MARKER" "$CONSOLE_LOG"; then
    printf 'FAIL (the guest workload did not complete; see %s)\n' "$CONSOLE_LOG"
    exit 1
fi
if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the client did not finish; see %s)\n' "$CONSOLE_LOG"
    exit 1
fi

if (( LTP )); then
    mkdir -p -- "$LTP_DIR"
    python3 - "$CONSOLE_LOG" "$LTP_DIR" "$LTP_PHASE" <<'PY'
import json
import os
import re
import sys

console, outdir, phase = sys.argv[1:4]
text = open(console, "rb").read().decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;?]*m", "", text)

if phase == "check":
    lines = [m.group(1).strip() for m in re.finditer(r"LTPCHK ([^\r\n]*)", text)]
    with open(os.path.join(outdir, "ltp-check.txt"), "w") as out:
        out.write("\n".join(lines) + "\n")
    for line in lines:
        print("LTPCHK %s" % line)
    print("LTP check done (%d lines, written to %s)" % (len(lines), outdir))
    raise SystemExit(0)

setup = [m.group(0).strip() for m in
         re.finditer(r"LTP setup [^\r\n]*", text)]
builds = [(m.group(1), int(m.group(2))) for m in
          re.finditer(r"LTP build dir=(\S+) rc=(-?\d+)", text)]
stopped = [m.group(1) for m in
           re.finditer(r"LTP stopped reason=([^\r\n]*)", text)]
diag = [m.group(1).strip() for m in
        re.finditer(r"LTP diag ([^\r\n]*)", text)]
cases = {}
for m in re.finditer(
        r"LTP case=(\S+) suite=(\S+) rc=(-?\d+) status=(pass|fail|skip) "
        r"dur=([0-9.]+) reason=([^\r\n]*)", text):
    name, suite, rc, status, dur, reason = m.groups()
    cases[name] = {"case": name, "suite": suite, "rc": int(rc),
                   "status": status, "dur": float(dur),
                   "reason": reason.strip()}


def attribute(rec):
    """Coarse cause; the doc keeps the reviewed wording."""
    if rec["status"] == "pass":
        return "ok"
    reason = rec["reason"].lower().replace("_", " ")
    if rec["status"] == "skip":
        if "binary missing" in reason or "tconf" in reason:
            return "test-limitation"
        return "skip"
    if "sigsegv" in reason:
        return "test-crash"
    if any(t in reason for t in ("succeeded", "mismatched", "expected")):
        return "semantics-mismatch"
    if any(t in reason for t in ("enotsup", "function not implemented",
                                 "unimplemented", "operation not supported",
                                 "eafnosupport", "protocol not supported",
                                 "enosys")):
        return "syscall-missing"
    if any(t in reason for t in ("permission denied", "eacces", "eperm",
                                 "operation not permitted")):
        return "permission-env"
    if any(t in reason for t in ("einval", "eopnotsupp", "invalid argument",
                                 "not supported")):
        return "semantics-mismatch"
    return "other"


for rec in cases.values():
    rec["category"] = attribute(rec)

with open(os.path.join(outdir, "ltp.tsv"), "w") as out:
    out.write("case\tsuite\trc\tstatus\tdur\treason\tcategory\n")
    for rec in cases.values():
        out.write("%s\t%s\t%d\t%s\t%.2f\t%s\t%s\n" % (
            rec["case"], rec["suite"], rec["rc"], rec["status"],
            rec["dur"], rec["reason"], rec["category"]))
with open(os.path.join(outdir, "ltp.json"), "w") as out:
    json.dump({"phase": "run", "setup": setup,
               "builds": [{"dir": d, "rc": r} for d, r in builds],
               "stopped": stopped, "diagnostics": diag,
               "cases": list(cases.values())},
              out, indent=2)
    out.write("\n")

passed = sum(1 for r in cases.values() if r["status"] == "pass")
failed = sum(1 for r in cases.values() if r["status"] == "fail")
skipped = sum(1 for r in cases.values() if r["status"] == "skip")
print("LTP cases=%d pass=%d fail=%d skip=%d"
      % (len(cases), passed, failed, skipped))
for msg in setup:
    print("  " + msg)
for rec in cases.values():
    if rec["status"] != "pass":
        print("  %-12s %-6s %-14s %s"
              % (rec["case"], rec["status"], rec["category"],
                 rec["reason"][:80]))
if "setup-failed" in text:
    print("FAIL (LTP setup failed; see %s)" % console)
    raise SystemExit(1)
if not cases:
    print("FAIL (no LTP case completed; see %s)" % console)
    raise SystemExit(1)
PY
    printf 'LTP (tables written to %s)\n' "$LTP_DIR"
    exit 0
fi

if (( SOURCE_BUILD )); then
    mkdir -p -- "$SOURCE_BUILD_DIR"
    python3 - "$CONSOLE_LOG" "$SOURCE_BUILD_DIR" <<'PY'
import json
import os
import re
import sys

console, outdir = sys.argv[1:3]
text = open(console, "rb").read().decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)

env = {}
for m in re.finditer(r"DPKGB env ([A-Za-z0-9_]+)=([^\r\n]*)", text):
    env[m.group(1)] = m.group(2).strip()
setup = [(m.group(1), int(m.group(2))) for m in
         re.finditer(r"DPKGB setup step=(\S+) rc=(-?\d+)", text)]
fatal = [m.group(1).strip() for m in
         re.finditer(r"DPKGB fatal ([^\r\n]*)", text)]
selftest = [m.group(1).strip() for m in
            re.finditer(r"DPKGB selftest ([^\r\n]*)", text)]

names = {}
for m in re.finditer(r"DPKGB pkg=(\d+) start name=(\S+)", text):
    names[int(m.group(1))] = m.group(2)


def record(n):
    return packages.setdefault(n, {
        "pkg": n, "name": names.get(n, ""), "stages": [],
        "artifacts": [], "errors": [], "evidence": [],
        "result": "incomplete", "attempts": 0, "dur": 0.0,
    })


packages = {}
for m in re.finditer(
        r"DPKGB pkg=(\d+) stage=(builddeps|source|build) rc=(-?\d+) "
        r"dur=([0-9.]+)(?: attempt=(\d+))?", text):
    n, stage, rc, dur, attempt = m.groups()
    rec = record(int(n))
    rec["stages"].append({
        "stage": stage, "rc": int(rc), "dur": float(dur),
        "attempt": int(attempt) if attempt else 1,
    })
    if attempt:
        rec["attempts"] = max(rec["attempts"], int(attempt))
for m in re.finditer(r"DPKGB pkg=(\d+) artifact=(\S+)", text):
    record(int(m.group(1)))["artifacts"].append(m.group(2))
for m in re.finditer(r"DPKGB pkg=(\d+) err=([^\r\n]*)", text):
    record(int(m.group(1)))["errors"].append(m.group(2).strip())
for m in re.finditer(r"DPKGB pkg=(\d+) evidence=([^\r\n]*)", text):
    record(int(m.group(1)))["evidence"].append(m.group(2).strip())
for m in re.finditer(
        r"DPKGB pkg=(\d+) done name=(\S+) result=(ok|fail|timeout|disk) "
        r"attempts=(\d+) dur=([0-9.]+)", text):
    n, name, result, attempts, dur = m.groups()
    rec = record(int(n))
    rec["name"] = name
    rec["result"] = result
    rec["attempts"] = max(rec["attempts"], int(attempts))
    rec["dur"] = float(dur)


def stage_rec(rec, name):
    # Prefer the attempt that succeeded; with no success the last one is the
    # one that decided the package's result.
    matches = [item for item in rec["stages"] if item["stage"] == name]
    for item in matches:
        if item["rc"] == 0:
            return item
    if matches:
        return matches[-1]
    return {"rc": "", "dur": "", "attempt": ""}


def attribute(rec):
    """Coarse cause; the doc keeps the reviewed wording."""
    if rec["result"] == "ok":
        return "ok"
    err = " ".join(rec["errors"]).lower().replace("_", " ")
    if rec["result"] == "timeout" or "timeout" in err:
        return "timeout"
    # A signal death is neither a compile error nor a missing tool.
    if any(t in err for t in ("killed by signal", "segmentation fault",
                              "sigsegv", "sigabrt")):
        return "other"
    if any(t in err for t in ("disk low", "no space", "enospc", "disk full",
                              "disk_low")):
        return "disk"
    if any(t in err for t in ("not found", "unable to locate", "no such file",
                              "source tree", "toolchain", "command not found")):
        return "missing-tool"
    if any(t in err for t in ("function not implemented", "enotsup",
                              "unimplemented", "operation not supported",
                              "eafnosupport", "protocol not supported")):
        return "syscall-missing"
    if any(t in err for t in ("error:", "fatal error", "make[",
                              "compilation terminated", "undefined reference",
                              "segmentation fault", "recipe for target")):
        return "compile"
    return "other"


order = sorted(packages)
for n in order:
    packages[n]["category"] = attribute(packages[n])

with open(os.path.join(outdir, "packages.tsv"), "w") as out:
    out.write("pkg\tname\tresult\tcategory\tattempts\tdur\t"
              "builddeps_rc\tbuilddeps_dur\tsource_rc\tsource_dur\t"
              "build_rc\tbuild_dur\tartifacts\terrors\tevidence\n")
    for n in order:
        rec = packages[n]
        bd = stage_rec(rec, "builddeps")
        src = stage_rec(rec, "source")
        build = stage_rec(rec, "build")
        out.write("%d\t%s\t%s\t%s\t%d\t%.2f\t%s\t%s\t%s\t%s\t%s\t%s\t"
                  "%s\t%s\t%s\n" % (
                      n, rec["name"], rec["result"], rec["category"],
                      rec["attempts"], rec["dur"],
                      bd["rc"], bd["dur"], src["rc"], src["dur"],
                      build["rc"], build["dur"],
                      ",".join(rec["artifacts"]),
                      " | ".join(rec["errors"]),
                      " | ".join(rec["evidence"])))
with open(os.path.join(outdir, "stages.tsv"), "w") as out:
    out.write("pkg\tname\tattempt\tstage\trc\tdur\n")
    for n in order:
        for item in packages[n]["stages"]:
            out.write("%d\t%s\t%d\t%s\t%d\t%.2f\n" % (
                n, packages[n]["name"], item["attempt"], item["stage"],
                item["rc"], item["dur"]))
with open(os.path.join(outdir, "env.tsv"), "w") as out:
    out.write("key\tvalue\n")
    for key in sorted(env):
        out.write("%s\t%s\n" % (key, env[key]))

ok = sum(1 for n in order if packages[n]["result"] == "ok")
failed = len(order) - ok
with open(os.path.join(outdir, "source-build.json"), "w") as out:
    json.dump({
        "env": env,
        "setup": [{"step": s, "rc": r} for s, r in setup],
        "fatal": fatal,
        "selftest": selftest,
        "packages": [packages[n] for n in order],
        "summary": {"total": len(order), "ok": ok, "fail": failed},
    }, out, indent=2)
    out.write("\n")

print("SOURCE_BUILD packages=%d ok=%d fail=%d" % (len(order), ok, failed))
print("  env gcc=%s dpkg=%s" % (env.get("gcc", "?"),
                                env.get("dpkg_buildpackage", "?")))
for key in ("gcc", "dpkg", "dpkg_buildpackage", "debian", "disk_free_kb"):
    if key in env:
        print("  env %-18s %s" % (key, env[key]))
for n in order:
    rec = packages[n]
    print("  pkg=%d %-16s %-10s %-14s attempts=%d dur=%.2fs artifacts=%d"
          % (n, rec["name"], rec["result"], rec["category"], rec["attempts"],
             rec["dur"], len(rec["artifacts"])))
    for item in rec["stages"]:
        print("      stage=%-9s attempt=%d rc=%-3d dur=%.2fs"
              % (item["stage"], item["attempt"], item["rc"], item["dur"]))
    for artifact in rec["artifacts"]:
        print("      artifact=%s" % artifact)
    for err in rec["errors"]:
        print("      err=%s" % err)
    for ev in rec["evidence"]:
        print("      evidence=%s" % ev)
if selftest:
    with open(os.path.join(outdir, "selftest.txt"), "w") as out:
        out.write("\n".join(selftest) + "\n")
    print("SOURCE_BUILD selftest checks=%d" % len(selftest))
    for line in selftest:
        print("  %s" % line)
    raise SystemExit(0)
if fatal:
    print("FAIL (source-build setup failed: %s; see %s)"
          % (" ".join(fatal), console))
    raise SystemExit(1)
if not order:
    print("FAIL (no DPKGB package line; see %s)" % console)
    raise SystemExit(1)
PY
    printf 'SOURCE_BUILD (tables written to %s)\n' "$SOURCE_BUILD_DIR"
    exit 0
fi

if (( DEBOOTSTRAP )); then
    mkdir -p -- "$DEBOOTSTRAP_DIR"
    python3 - "$CONSOLE_LOG" "$DEBOOTSTRAP_DIR" <<'PY'
import json
import os
import re
import sys

console, outdir = sys.argv[1:3]
text = open(console, "rb").read().decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)

env = {}
for m in re.finditer(r"DBST env ([A-Za-z0-9_]+)=([^\r\n]*)", text):
    env[m.group(1)] = m.group(2).strip()

disk = []
for m in re.finditer(
        r"DBST disk_free_kb=(\d+) stage=(\S+) phase=(\S+)", text):
    disk.append({"stage": m.group(2), "phase": m.group(3),
                 "free_kb": int(m.group(1))})

stage_list = []
for m in re.finditer(
        r"DBST stage=(install|foreign|second) rc=(-?\d+) dur=([0-9.]+)"
        r"(?: preinstalled=(yes|no))?", text):
    stage_list.append({
        "stage": m.group(1), "rc": int(m.group(2)),
        "dur": float(m.group(3)),
        "preinstalled": m.group(4) or "",
    })
errors = [m.group(1).strip() for m in
          re.finditer(r"DBST err=([^\r\n]*)", text)]
log_lines = [(m.group(1), m.group(2).strip()) for m in
             re.finditer(r"DBST log (second|target)=([^\r\n]*)", text)]
chroot_probe = {}
m = re.search(
    r"DBST chroot_probe rc=(-?\d+) dur=([0-9.]+) out=([^\r\n]*)", text)
if m:
    chroot_probe = {"rc": int(m.group(1)), "dur": float(m.group(2)),
                    "out": m.group(3).strip()}
mount_probe = {}
for name in ("mount_probe", "umount_probe", "unshare_probe"):
    m = re.search(r"DBST %s rc=(-?\d+) out=([^\r\n]*)" % name, text)
    if m:
        mount_probe[name] = {"rc": int(m.group(1)),
                             "out": m.group(2).strip()}
# The kernel warning is the reason a stage can fail while its own tools are
# fine, so surface it next to the stage results.
journal_e2big = "ext4 journal transaction is too large" in text
verify = {}
m = re.search(
    r"DBST verify debian_version=(\S*) sh=([^\r\n]*?) ok=([01])", text)
if m:
    verify = {"debian_version": m.group(1), "sh": m.group(2).strip(),
              "ok": int(m.group(3))}
target_kb = {}
for m in re.finditer(r"DBST target_kb=(\d+)", text):
    target_kb["before_cleanup"] = int(m.group(1))
for m in re.finditer(r"DBST target_kb_after_cleanup=(\d+)", text):
    target_kb["after_cleanup"] = int(m.group(1))
summary = {}
m = re.search(
    r"DBST summary install_rc=(\S+) foreign_rc=(\S+) second_rc=(\S+)", text)
if m:
    summary = {"install": m.group(1), "foreign": m.group(2),
               "second": m.group(3)}


def free_of(stage, phase):
    for item in disk:
        if item["stage"] == stage and item["phase"] == phase:
            return item["free_kb"]
    return ""


def attribute(rec):
    """L5 categories: chroot, syscall, network/source, disk, timeout, other."""
    if rec["rc"] == 0:
        return "ok"
    if rec["rc"] == 124:
        return "timeout"
    if rec["stage"] == "second" and chroot_probe.get("rc", 0) != 0:
        return "chroot-missing"
    err = " ".join(errors).lower().replace("_", " ")
    if "disk low" in err or "no space" in err or "enospc" in err:
        return "disk"
    if "timeout" in err:
        return "timeout"
    if any(t in err for t in ("function not implemented", "enotsup",
                              "unimplemented", "operation not supported")):
        if "chroot" in err or rec["stage"] == "second":
            return "chroot-missing"
        return "syscall-missing"
    if any(t in err for t in ("could not resolve", "failed to fetch",
                              "temporary failure", "network is unreachable",
                              "connection refused", "404", "release file",
                              "no installation candidate")):
        return "network-source"
    return "other"


for rec in stage_list:
    rec["category"] = attribute(rec)

with open(os.path.join(outdir, "stages.tsv"), "w") as out:
    out.write("stage\trc\tcategory\tdur\tfree_before_kb\tfree_after_kb\t"
              "preinstalled\n")
    for rec in stage_list:
        out.write("%s\t%d\t%s\t%.2f\t%s\t%s\t%s\n" % (
            rec["stage"], rec["rc"], rec["category"], rec["dur"],
            free_of(rec["stage"], "before"),
            free_of(rec["stage"], "after"), rec["preinstalled"]))
with open(os.path.join(outdir, "disk.tsv"), "w") as out:
    out.write("stage\tphase\tfree_kb\n")
    for item in disk:
        out.write("%s\t%s\t%d\n" % (
            item["stage"], item["phase"], item["free_kb"]))
with open(os.path.join(outdir, "env.tsv"), "w") as out:
    out.write("key\tvalue\n")
    for key in sorted(env):
        out.write("%s\t%s\n" % (key, env[key]))
with open(os.path.join(outdir, "debootstrap.json"), "w") as out:
    json.dump({"env": env, "stages": stage_list, "disk": disk,
               "errors": errors, "logs": log_lines,
               "chroot_probe": chroot_probe,
               "mount_probe": mount_probe,
               "journal_e2big": journal_e2big, "verify": verify,
               "target_kb": target_kb, "summary": summary},
              out, indent=2)
    out.write("\n")

ok = sum(1 for rec in stage_list if rec["rc"] == 0)
print("DEBOOTSTRAP stages=%d ok=%d fail=%d"
      % (len(stage_list), ok, len(stage_list) - ok))
for key in ("debian", "suite", "variant", "target", "debootstrap",
            "root_free_kb"):
    if key in env:
        print("  env %-14s %s" % (key, env[key]))
for rec in stage_list:
    print("  stage=%-8s rc=%-3d %-14s dur=%.2fs free_before=%s after=%s"
          % (rec["stage"], rec["rc"], rec["category"], rec["dur"],
             free_of(rec["stage"], "before"),
             free_of(rec["stage"], "after")))
if chroot_probe:
    print("  chroot_probe rc=%d dur=%.2fs out=%s"
          % (chroot_probe.get("rc", -1), chroot_probe.get("dur", 0),
             chroot_probe.get("out", "")))
for name in ("mount_probe", "umount_probe", "unshare_probe"):
    if name in mount_probe:
        print("  %-13s rc=%d out=%s"
              % (name, mount_probe[name]["rc"], mount_probe[name]["out"]))
for kind, line in log_lines:
    print("  log %-7s %s" % (kind, line))
for msg in errors:
    print("  err=%s" % msg)
if journal_e2big:
    print("  kernel: ext4 journal transaction too large (E2BIG) warnings")
if verify:
    print("  verify debian_version=%s sh=%s ok=%d"
          % (verify.get("debian_version", ""), verify.get("sh", ""),
             verify.get("ok", 0)))
if target_kb:
    print("  target_kb %s" % json.dumps(target_kb, sort_keys=True))
if not stage_list:
    print("FAIL (no DBST stage line; see %s)" % console)
    raise SystemExit(1)
PY
    printf 'DEBOOTSTRAP (tables written to %s)\n' "$DEBOOTSTRAP_DIR"
    exit 0
fi

if (( EXT4_E2BIG )); then
    mkdir -p -- "$EXT4_E2BIG_DIR"
    python3 - "$CONSOLE_LOG" "$EXT4_E2BIG_DIR" <<'PY'
import json
import os
import re
import sys

console, outdir = sys.argv[1:3]
text = open(console, "rb").read().decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)

env = {}
for m in re.finditer(r"E2BIG env ([A-Za-z0-9_]+)=([^\r\n]*)", text):
    env[m.group(1)] = m.group(2).strip()

errors = {}
for m in re.finditer(r"E2BIG err step=(\S+) ([^\r\n]*)", text):
    errors.setdefault(m.group(1), []).append(m.group(2).strip())

steps = []
for m in re.finditer(
        r"E2BIG step=(\S+) rc=(-?\d+) dur=([0-9.]+)(?: files=(\d+))?",
        text):
    steps.append({
        "step": m.group(1), "rc": int(m.group(2)),
        "dur": float(m.group(3)),
        "files": int(m.group(4)) if m.group(4) else "",
        "errors": errors.get(m.group(1), []),
    })

disk = [{"phase": m.group(2), "free_kb": int(m.group(1))} for m in
        re.finditer(r"E2BIG disk_free_kb=(\d+) phase=(\S+)", text)]
cache = {}
m = re.search(r"E2BIG cache_path=(\S+) cache_kb=(\d+)", text)
if m:
    cache = {"path": m.group(1), "bytes": int(m.group(2))}
journal = len(re.findall(r"ext4 journal transaction is too large", text))

with open(os.path.join(outdir, "steps.tsv"), "w") as out:
    out.write("step\trc\tdur\tfiles\terrors\n")
    for rec in steps:
        out.write("%s\t%d\t%.2f\t%s\t%s\n" % (
            rec["step"], rec["rc"], rec["dur"], rec["files"],
            " | ".join(rec["errors"])))
with open(os.path.join(outdir, "disk.tsv"), "w") as out:
    out.write("phase\tfree_kb\n")
    for item in disk:
        out.write("%s\t%d\n" % (item["phase"], item["free_kb"]))
with open(os.path.join(outdir, "env.tsv"), "w") as out:
    out.write("key\tvalue\n")
    for key in sorted(env):
        out.write("%s\t%s\n" % (key, env[key]))
with open(os.path.join(outdir, "ext4-e2big.json"), "w") as out:
    json.dump({"env": env, "steps": steps, "disk": disk,
               "cache": cache, "journal_e2big_warnings": journal},
              out, indent=2)
    out.write("\n")

print("E2BIG steps=%d failed=%d journal_warnings=%d"
      % (len(steps), sum(1 for r in steps if r["rc"] != 0), journal))
for rec in steps:
    print("  step=%-2s rc=%-3d dur=%.2fs files=%s %s"
          % (rec["step"], rec["rc"], rec["dur"], rec["files"],
             " | ".join(rec["errors"])[:100]))
if cache:
    print("  cache %s = %d bytes" % (cache.get("path", ""),
                                     cache.get("bytes", 0)))
for item in disk:
    print("  disk %-6s %d KB free" % (item["phase"], item["free_kb"]))
if not steps:
    print("FAIL (no E2BIG step line; see %s)" % console)
    raise SystemExit(1)
PY
    printf 'EXT4_E2BIG (tables written to %s)\n' "$EXT4_E2BIG_DIR"
    exit 0
fi

if (( SYSTEMD_HEALTH )); then
    mkdir -p -- "$SYSTEMD_HEALTH_DIR"
    python3 - "$CONSOLE_LOG" "$SYSTEMD_HEALTH_DIR" <<'PY'
import json
import os
import re
import sys
from collections import Counter

console, outdir = sys.argv[1:3]
text = open(console, "rb").read().decode("utf-8", "replace")
# Strip colour (SGR) sequences only. The serial stream also contains
# truncated ESC[ sequences followed by our own output ("ESC[SYSD ...");
# a generic CSI regex would eat the leading S of SYSD and drop the line.
text = re.sub(r"\x1b\[[0-9;?]*m", "", text)


def num(name, default=-1):
    m = re.search(r"SYSD %s=(\d+)" % name, text)
    return int(m.group(1)) if m else default


def one(name):
    m = re.search(r"SYSD %s=([^\r\n]*)" % name, text)
    return m.group(1).strip() if m else ""


systemd = re.search(r"SYSD systemd=(yes|no)", text)
systemd = systemd.group(1) if systemd else "unknown"

phases = {}
m = re.search(r"SYSD analyze\b([^\r\n]*)", text)
if m:
    for name, value in re.findall(r"(\w+)=([0-9.]+)", m.group(1)):
        phases[name] = float(value)
for key in ("firmware", "loader", "kernel", "initrd", "userspace", "total"):
    phases.setdefault(key, 0.0)

failed = {}
for m in re.finditer(
        r"SYSD failed unit=(\S+) load=(\S+) active=(\S+) sub=(\S+) desc=(.*)",
        text):
    unit, load, active, sub, desc = m.groups()
    failed[unit] = {"unit": unit, "load": load, "active": active,
                    "sub": sub, "desc": desc.strip(), "result": "",
                    "evidence": "", "category": ""}
for m in re.finditer(r"SYSD failed_result unit=(\S+) res=(.*)", text):
    if m.group(1) in failed:
        failed[m.group(1)]["result"] = m.group(2).strip()
for m in re.finditer(r"SYSD failed_evid unit=(\S+) ev=(.*)", text):
    if m.group(1) in failed:
        failed[m.group(1)]["evidence"] = m.group(2).strip()


def attribute(rec):
    """Coarse cause; the doc keeps the reviewed wording."""
    # The guest sanitizes spaces to underscores in the console token.
    s = (rec["result"] + " " + rec["evidence"]).lower().replace("_", " ")
    if "timeout" in s or "timed out" in s:
        return "unit-timeout"
    if any(t in s for t in ("enotsup", "function not implemented", "unimplemented",
                            "operation not supported", "eafnosupport",
                            "protocol not supported", "einval")):
        return "syscall-missing"
    if any(t in s for t in ("permission denied", "eacces", "read-only",
                            "operation not permitted")):
        return "config-permission"
    if any(t in s for t in ("dependency", "not found", "no such file",
                            "failed to load", "not loaded")):
        return "dependency"
    return "other"


for rec in failed.values():
    rec["category"] = attribute(rec)

blame = []
for idx, m in enumerate(
        re.finditer(r"SYSD blame unit=(\S+) sec=([0-9.]+)", text), 1):
    blame.append((idx, m.group(1), float(m.group(2))))

critical = [(int(r), line.strip()) for r, line in
            re.findall(r"SYSD critical rank=(\d+) line=(.*)", text)]

verify_msgs = {}
for m in re.finditer(r"SYSD verify_msg unit=(\S+) msg=(.*)", text):
    verify_msgs[m.group(1)] = m.group(2).strip()
verify = []
for unit, rc in re.findall(r"SYSD verify unit=(\S+) rc=(\S+)", text):
    verify.append({"unit": unit, "rc": rc,
                   "message": verify_msgs.get(unit, "")})

jerr = [msg.strip() for msg in re.findall(r"SYSD jerr msg=(.*)", text)]
aggregated = Counter(re.sub(r"\d+", "#", msg) for msg in jerr if msg)

startup_total = phases["total"]
running = num("running_services")
units_total = num("units_total")
unit_files = num("unit_files")
jerr_count = num("jerr_count")
state = one("is_system_running")
uptime = one("uptime")
analyze_raw = one("analyze_raw")

with open(os.path.join(outdir, "startup.tsv"), "w") as out:
    out.write("phase\tseconds\n")
    for key in ("firmware", "loader", "kernel", "initrd", "userspace", "total"):
        out.write("%s\t%.3f\n" % (key, phases[key]))
with open(os.path.join(outdir, "failed.tsv"), "w") as out:
    out.write("unit\tload\tactive\tsub\tresult\tevidence\tcategory\tdesc\n")
    for rec in failed.values():
        out.write("%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" % (
            rec["unit"], rec["load"], rec["active"], rec["sub"],
            rec["result"], rec["evidence"], rec["category"], rec["desc"]))
with open(os.path.join(outdir, "blame.tsv"), "w") as out:
    out.write("rank\tunit\tseconds\n")
    for rank, unit, sec in blame:
        out.write("%d\t%s\t%.3f\n" % (rank, unit, sec))
with open(os.path.join(outdir, "critical.tsv"), "w") as out:
    out.write("rank\tline\n")
    for rank, line in critical:
        out.write("%d\t%s\n" % (rank, line))
with open(os.path.join(outdir, "verify.tsv"), "w") as out:
    out.write("unit\trc\tmessage\n")
    for rec in verify:
        out.write("%s\t%s\t%s\n" % (rec["unit"], rec["rc"], rec["message"]))
with open(os.path.join(outdir, "journal-errors.tsv"), "w") as out:
    out.write("occurrences\tmessage\n")
    for msg, count in aggregated.most_common(10):
        out.write("%d\t%s\n" % (count, msg))
with open(os.path.join(outdir, "health.json"), "w") as out:
    json.dump({
        "systemd": systemd, "state": state, "uptime": uptime,
        "analyze_raw": analyze_raw,
        "startup": phases,
        "running_services": running,
        "units_total": units_total,
        "unit_files": unit_files,
        "failed_count": len(failed),
        "journal_errors": jerr_count,
        "failed": list(failed.values()),
        "blame": [{"rank": r, "unit": u, "seconds": s} for r, u, s in blame],
        "critical_chain": [{"rank": r, "line": l} for r, l in critical],
        "verify": verify,
        "journal_top": [{"occurrences": c, "message": m}
                        for m, c in aggregated.most_common(10)],
    }, out, indent=2)
    out.write("\n")

if systemd != "yes":
    print("SYSTEMD systemd=%s (not systemd; nothing measured)" % systemd)
    raise SystemExit(1)
print("SYSTEMD systemd=yes state=%s failed=%d running=%d units=%d files=%d "
      "jerr=%d startup_total=%.3fs"
      % (state, len(failed), running, units_total, unit_files, jerr_count,
         startup_total))
for rec in failed.values():
    print("  %-40s %-16s %s | %s"
          % (rec["unit"], rec["category"], rec["result"][:40],
             rec["evidence"][:60]))
PY
    printf 'SYSTEMD (tables written to %s)\n' "$SYSTEMD_HEALTH_DIR"
    exit 0
fi

if (( POPCON )); then
    # Turn the guest's per-package lines into a TSV plus a JSON array. The
    # attribution column is a first cut; the doc keeps the reviewed reasons.
    python3 - "$CONSOLE_LOG" "$POPCON_TSV" "$POPCON_JSON" "$POPCON_LIST" <<'PY'
import json
import re
import sys

console, tsv, json_path, listing = sys.argv[1:5]
text = open(console, "rb").read().decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)

pattern = re.compile(
    r"POPCON pkg=([A-Za-z0-9.+-]+) rc=(-?[0-9]+) smoke=(ok|cli|lib|no|na) "
    r"dur=([0-9.]+) tries=([0-9]+) pre=(absent|installed) err=([^\r\n]*)"
)
records = {}
for match in pattern.finditer(text):
    pkg, rc, smoke, dur, tries, pre, err = match.groups()
    records[pkg.rstrip("_")] = {
        "pkg": pkg, "rc": int(rc), "smoke": smoke,
        "dur": float(dur), "tries": int(tries), "pre": pre,
        "err": err.strip(),
    }


def attribute(rec):
    """Coarse cause, refined by hand in docs/porting/debian-package-compat.md."""
    err = rec["err"].lower()
    if rec["rc"] == 0:
        return "command-fail" if rec["smoke"] == "no" else "ok"
    if rec["rc"] == 124 or "timeout" in err:
        return "disk-time"          # exceeded the per-install deadline
    if "no space" in err or "enospc" in err or "disk full" in err:
        return "disk-time"
    if "unable to locate" in err or "no installation candidate" in err:
        return "dependency-chain"    # index has no candidate for this release
    if "depends" in err or "dependencies" in err:
        return "dependency-chain"
    if any(t in err for t in ("function not implemented", "enotsup",
                              "unimplemented", "operation not supported")):
        return "syscall"
    return "other"


order = [line.strip() for line in open(listing) if line.strip()
         and not line.startswith("#")]
found = [records[p] for p in order if p in records]
extra = [r for p, r in records.items() if p not in order]
found.extend(extra)

with open(tsv, "w") as out:
    out.write("pkg\trc\tsmoke\tdur\ttries\tpre\tcategory\terr\n")
    for rec in found:
        out.write("%s\t%d\t%s\t%.2f\t%d\t%s\t%s\t%s\n" % (
            rec["pkg"], rec["rc"], rec["smoke"], rec["dur"], rec["tries"],
            rec["pre"], attribute(rec), rec["err"]))
with open(json_path, "w") as out:
    json.dump(found, out, indent=2)
    out.write("\n")

ok = sum(1 for r in found if r["rc"] == 0)
smoke_ok = sum(1 for r in found if r["smoke"] == "ok")
smoke_cli = sum(1 for r in found if r["smoke"] == "cli")
smoke_lib = sum(1 for r in found if r["smoke"] == "lib")
preinstalled = sum(1 for r in found if r["pre"] == "installed")
total = sum(r["dur"] for r in found)
print("POPCON packages=%d/%d install_ok=%d smoke_ok=%d smoke_cli=%d smoke_lib=%d "
      "preinstalled=%d guest_seconds=%.2f"
      % (len(found), len(order), ok, smoke_ok, smoke_cli, smoke_lib,
         preinstalled, total))
for rec in found:
    if rec["rc"] != 0 or rec["smoke"] in ("no", "cli") or rec["pre"] != "absent":
        print("  %-18s rc=%-3d smoke=%-3s pre=%-9s %-16s %s"
              % (rec["pkg"], rec["rc"], rec["smoke"], rec["pre"],
                 attribute(rec), rec["err"][:90]))
if len(found) < len(order):
    missing = [p for p in order if p not in records]
    print("FAIL (no POPCON line for: %s)" % ", ".join(missing))
    raise SystemExit(1)
PY
    printf 'POPCON (results written to %s)\n' "$POPCON_TSV"
    exit 0
fi

# Build the matrix from the guest's markers.
python3 - "$CONSOLE_LOG" "$MATRIX" <<'PY'
import re
import sys

console, matrix = sys.argv[1], sys.argv[2]
text = open(console, "rb").read().decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

domains = [
    "DESKTOP", "APP", "APT", "CLIPBOARD", "INPUT_METHOD", "AUDIO", "USB",
    "SUSPEND",
]
found = {}
for name, value, *detail in re.findall(
    # Kernel log lines are interleaved into the serial stream and can land
    # inside a marker; tolerate a little noise between the name and the value.
    r"DAILY_([A-Z_]+)[^\r\n]{0,80}?=(ok|fail|missing)\s*([^\r\n]*)", text
):
    found[name] = (value, " ".join(detail).strip())

missing = [name for name in domains if name not in found]
if missing:
    print("FAIL (no result for: %s)" % ", ".join(missing))
    raise SystemExit(1)

with open(matrix, "w") as out:
    out.write("domain\tresult\tdetail\n")
    width = max(len(name) for name in domains)
    print("%-*s  %-8s %s" % (width, "DOMAIN", "RESULT", "DETAIL"))
    print("%s  %s %s" % ("-" * width, "-" * 8, "-" * 30))
    for name in domains:
        value, detail = found[name]
        out.write("%s\t%s\t%s\n" % (name, value, detail))
        print("%-*s  %-8s %s" % (width, name, value, detail))
PY

printf 'DAILY (matrix written to %s)\n' "$MATRIX"
