#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Daily-use gate for the x86_64 Debian image.
#
# One boot exercises the user-visible flow on the image exactly as it ships:
#
#   * log in on the serial console (`ttyS0`) as the provisioned account,
#   * check identity and the wall clock,
#   * edit a file in the home directory,
#   * use passwordless sudo to configure the QEMU slirp resolver and run
#     `apt-get update` (network + package metadata),
#   * press the ACPI power button through the QEMU monitor and require the
#     guest to power off.
#
# The host requires the expected console markers and `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: daily_use_check.sh --kernel ELF --initramfs CPIO --base-root IMG
                          --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy for the run (required).
  --workspace DIR     Image, scripts and transcripts.
  --timeout SECONDS   Deadline for the boot, login and command flow
                      (default: 600).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

The guest account is `asterinas` (uid 1000) with password `asterinas` and
passwordless sudo. `apt-get update` points at the QEMU slirp resolver
(10.0.2.3), which is the network the gate runs on.

Exit status is 0 only if the login, identity, file edit, apt update and
power-button power-off all succeed and `e2fsck -fn` accepts the image.
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
TIMEOUT=600
LOGLEVEL=4
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --timeout | \
            --loglevel | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
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
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$ACCEL" == "tcg" || "$ACCEL" == "kvm" ]] ||
    die_usage "--accel must be 'tcg' or 'kvm'"
for tool in debugfs e2fsck python3 timeout "$QEMU"; do
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
IMAGE="$WORKSPACE/daily.ext2"
WRAPPER="$WORKSPACE/qemu-daily.sh"
CLIENT="$WORKSPACE/daily_client.py"
CONSOLE_LOG="$WORKSPACE/console.txt"
SERIAL_SOCK="$WORKSPACE/serial.sock"
MONITOR_SOCK="$WORKSPACE/monitor.sock"
PIDFILE="$WORKSPACE/qemu.pid"

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
exec "$QEMU" "\${args[@]}" \\
    -serial "unix:$SERIAL_SOCK,server=on,wait=off" \\
    -monitor "unix:$MONITOR_SOCK,server=on,wait=off"
WRAPPER
chmod +x "$WRAPPER"

cat >"$CLIENT" <<'PY'
import os
import socket
import sys
import time

console_path, serial_path, monitor_path, pid_path, timeout = sys.argv[1:6]
timeout = float(timeout)
deadline = time.time() + timeout

COMMAND = (
    'echo DAILY_BEGIN; '
    'id; '
    'uname -m; '
    'date -u +%Y-%m-%d; '
    'echo daily-ok > /home/asterinas/daily.txt; '
    'cat /home/asterinas/daily.txt; '
    'sudo -n true && echo SUDO_OK; '
    "sudo sh -c 'printf \"nameserver 10.0.2.3\\n\" > /etc/resolv.conf'; "
    'sudo apt-get update >/tmp/apt-update.log 2>&1; rc=$?; echo APT_RC=$rc; '
    'if [ $rc -ne 0 ]; then echo APT_LOG_BEGIN; sudo tail -20 /tmp/apt-update.log; '
    'echo APT_LOG_END; fi; '
    'printf \'DAILY_%s_END\\n\' ok'
)


def connect(path, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.connect(path)
            return sock
        except OSError:
            time.sleep(0.05)
    return None


serial = connect(serial_path, 30)
if serial is None:
    print('DAILY_CLIENT no-serial', flush=True)
    sys.exit(2)
serial.settimeout(0.5)

buffer = b''
sent_user = sent_pass = sent_command = sent_power = False
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
            serial.sendall((COMMAND + '\n').encode())
            sent_command = True
        if sent_command and b'DAILY_ok_END' in buffer and not sent_power:
            monitor = connect(monitor_path, 10)
            if monitor is None:
                print('DAILY_CLIENT no-monitor', flush=True)
                break
            monitor.sendall(b'system_powerdown\n')
            monitor.close()
            sent_power = True
        if sent_power:
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
            except (OSError, ValueError):
                break
print('DAILY_CLIENT user=%s pass=%s command=%s power=%s'
      % (sent_user, sent_pass, sent_command, sent_power), flush=True)
ok = (sent_user and sent_pass and sent_command and sent_power and
      b'DAILY_ok_END' in buffer)
sys.exit(0 if ok else 1)
PY

rm -f -- "$IMAGE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$PIDFILE" "$CONSOLE_LOG"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' \
        "$WORKSPACE"
    exit 1
fi

timeout "$((TIMEOUT + 30))" python3 "$CLIENT" "$CONSOLE_LOG" "$SERIAL_SOCK" \
    "$MONITOR_SOCK" "$PIDFILE" "$TIMEOUT" &
CLIENT_PID=$!

set +e
timeout "$((TIMEOUT + 60))" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/boot.txt" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$WRAPPER" < /dev/null >"$WORKSPACE/boot.log" 2>&1 &
BOOT_PID=$!

wait "$CLIENT_PID"
CLIENT_STATUS=$?
kill "$BOOT_PID" 2>/dev/null
wait "$BOOT_PID" 2>/dev/null
set -e
if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the daily-use flow did not complete; see %s and %s)\n' \
        "$CONSOLE_LOG" "$WORKSPACE/boot.log"
    exit 1
fi

for marker in 'uid=1000(asterinas)' 'x86_64' 'daily-ok' 'SUDO_OK' \
    'APT_RC=0' 'DAILY_ok_END'; do
    if ! grep -aqF -- "$marker" "$CONSOLE_LOG"; then
        printf 'FAIL (console is missing "%s"); see %s\n' "$marker" "$CONSOLE_LOG"
        exit 1
    fi
done

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s/fsck-after.txt)\n' "$WORKSPACE"
    exit 1
fi
rm -f -- "$IMAGE"

printf 'PASS (serial login, identity, file edit, apt over slirp and power-button power-off)\n'
