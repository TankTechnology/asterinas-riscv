#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Interactive login gate for the x86_64 Debian image.
#
# The kernel exposes the 16550 UART as `ttyS0`, but its ISA interrupt line is
# not routed on this platform; the TTY read path therefore polls the UART
# (`TtyDriver::poll_input`) while it waits.  The gate boots with
# `console=ttyS0 earlycon` and relies on the image the builder produces:
# `console-getty.service` is pinned to `/dev/ttyS0` (avoiding the
# `dev-hvc0.device` dependency that never becomes ready), `login(1)` — whose
# controlling-terminal handoff the serial tty cannot complete — is replaced
# by a small password helper, and the helper drops to the provisioned
# `asterinas` account.
#
# The harness types the user name and password over a QEMU serial socket,
# runs `id`, and requires the unprivileged shell output and a clean image.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: interactive_login_check.sh --kernel ELF --initramfs CPIO
                                  --base-root IMG --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy for the run (required).
  --workspace DIR     Image, transcripts and helper files.
  --timeout SECONDS   Deadline for the boot and the login flow (default: 600).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

The guest account is `asterinas` (uid 1000) with password `asterinas`.

Exit status is 0 only if the login prompt and password prompt appear, the
shell runs `id` as uid 1000, the guest powers off and `e2fsck -fn` accepts the
image.
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
IMAGE="$WORKSPACE/login.ext2"
WRAPPER="$WORKSPACE/qemu-serial-sock.sh"
CLIENT="$WORKSPACE/login_client.py"
CONSOLE_LOG="$WORKSPACE/console.txt"
SOCKET="$WORKSPACE/serial.sock"

cat >"$WRAPPER" <<WRAPPER
#!/usr/bin/env bash
args=()
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        -device)
            case "\$2" in
                virtio-serial-pci|virtconsole,chardev=mux) shift 2; continue ;;
            esac
            args+=("\$1" "\$2"); shift 2 ;;
        -chardev)
            if [[ "\$2" == stdio,* ]]; then shift 2; continue; fi
            args+=("\$1" "\$2"); shift 2 ;;
        -serial)
            shift 2 ;;
        -append)
            args+=("\$1" "\${2/console=hvc0/console=ttyS0 earlycon}"); shift 2 ;;
        *)
            args+=("\$1"); shift ;;
    esac
done
exec "$QEMU" "\${args[@]}" -serial "unix:$SOCKET,server=on,wait=off"
WRAPPER
chmod +x "$WRAPPER"

cat >"$CLIENT" <<'PY'
import socket
import sys
import time

out_path, socket_path, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
deadline = time.time() + timeout
sock = None
while time.time() < deadline:
    try:
        sock = socket.socket(socket.AF_UNIX)
        sock.connect(socket_path)
        break
    except OSError:
        time.sleep(0.05)
if sock is None:
    print('LOGIN_CLIENT no-socket', flush=True)
    sys.exit(2)

sock.settimeout(0.5)
buffer = b''
sent_user = sent_pass = sent_command = False
pass_at = 0.0
with open(out_path, 'wb') as out:
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            chunk = b''
        if chunk:
            buffer += chunk
            out.write(chunk)
            out.flush()
        if not sent_user and b'login:' in buffer:
            sock.sendall(b'asterinas\n')
            sent_user = True
        if sent_user and not sent_pass and b'Password:' in buffer:
            sock.sendall(b'asterinas\n')
            sent_pass = True
            pass_at = time.time()
        if sent_pass and not sent_command and time.time() - pass_at > 1.0:
            sock.sendall(b'echo LOGIN_OK; id; poweroff -f\n')
            sent_command = True
        if b'LOGIN_OK' in buffer and b'uid=1000(asterinas)' in buffer:
            break
print('LOGIN_CLIENT ok=%s user=%s pass=%s command=%s'
      % (b'LOGIN_OK' in buffer and b'uid=1000(asterinas)' in buffer,
         sent_user, sent_pass, sent_command), flush=True)
sys.exit(0 if (b'LOGIN_OK' in buffer and b'uid=1000(asterinas)' in buffer) else 1)
PY

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"

# The login experience ships in the image: the builder installs the helper
# and its console-getty drop-in, enables console-getty and provisions the
# account the helper drops to.  The gate only removes the build's own
# evidence unit so it cannot power the guest off mid-login.
for path in /usr/local/sbin/asterinas-login \
    /etc/systemd/system/console-getty.service.d/asterinas-serial.conf \
    /etc/systemd/system/getty.target.wants/console-getty.service; do
    if ! debugfs -R "stat $path" "$IMAGE" 2>/dev/null | grep -q '^Inode:'; then
        printf 'FAIL (the image does not provide %s; rebuild the rootfs with the serial login profile)\n' \
            "$path"
        exit 1
    fi
done
{
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' "$WORKSPACE"
    exit 1
fi

rm -f -- "$SOCKET" "$CONSOLE_LOG"
timeout "$TIMEOUT" python3 "$CLIENT" "$CONSOLE_LOG" "$SOCKET" "$TIMEOUT" &
CLIENT_PID=$!

set +e
timeout "$((TIMEOUT + 30))" "$BOOT_SCRIPT" \
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
    printf 'FAIL (the login flow did not complete; see %s and %s)\n' \
        "$CONSOLE_LOG" "$WORKSPACE/boot.log"
    exit 1
fi

for marker in 'login:' 'Password:' 'LOGIN_OK' 'uid=1000(asterinas)'; do
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

printf 'PASS (login prompt, password, unprivileged shell and clean image)\n'
