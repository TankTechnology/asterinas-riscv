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

The guest account is `asterinas` (uid 1000, password `asterinas`, passwordless
sudo). Results land in `<workspace>/matrix.tsv` (checklist),
`<workspace>/popcon.tsv` (--popcon) or `<workspace>/systemd-health/`
(--systemd-health) and on stdout.
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

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --timeout | \
            --accel | --qemu | --popcon-list)
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
            esac
            shift 2
            ;;
        --popcon) POPCON=1; shift ;;
        --systemd-health) SYSTEMD_HEALTH=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

(( POPCON && SYSTEMD_HEALTH )) &&
    die_usage "--popcon and --systemd-health are mutually exclusive"

# The package probe runs 50 apt transactions; give it a longer default deadline
# than the checklist, which is bounded by its own sleeps.
if [[ -z "$TIMEOUT" ]]; then
    if (( POPCON )); then TIMEOUT=3600; else TIMEOUT=900; fi
fi
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"

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
else
    GUEST_COMMAND="sudo sh -c '/usr/local/bin/daily-use-checklist > /dev/console 2>&1'; echo DAILY_SHELL_END"
    DONE_MARKER="DAILY_DONE=ok"
    MODE="checklist"
fi

rm -f -- "$IMAGE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$PIDFILE" "$CONSOLE_LOG" \
    "$SCREENSHOT" "$MATRIX" "$POPCON_TSV" "$POPCON_JSON"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    # Retire units an earlier gate left enabled in the base image.
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/xorgprobe.service\n'
    printf 'rm /etc/systemd/system/xorgprobe.service\n'
    printf 'write %s /usr/local/bin/daily-use-checklist\n' "$CHECKLIST"
    printf 'sif /usr/local/bin/daily-use-checklist mode 0100755\n'
    printf 'write %s /etc/X11/xorg-noudev.conf\n' "$XORG_CONF"
    if (( POPCON )); then
        printf 'write %s /usr/local/bin/popcon-packages\n' "$POPCON_SCRIPT"
        printf 'sif /usr/local/bin/popcon-packages mode 0100755\n'
        printf 'write %s %s\n' "$POPCON_LIST" "$POPCON_GUEST_LIST"
        printf 'sif %s mode 0100644\n' "$POPCON_GUEST_LIST"
    fi
    if (( SYSTEMD_HEALTH )); then
        printf 'write %s /usr/local/bin/systemd-health\n' "$SYSTEMD_HEALTH_SCRIPT"
        printf 'sif /usr/local/bin/systemd-health mode 0100755\n'
    fi
    if [[ -f "$MSGTEST_BIN" ]]; then
        printf 'write %s /usr/local/bin/msgtest\n' "$MSGTEST_BIN"
        printf 'sif /usr/local/bin/msgtest mode 0100755\n'
    fi
    if [[ -f "$IOTEST_BIN" ]]; then
        printf 'write %s /usr/local/bin/iotest\n' "$IOTEST_BIN"
        printf 'sif /usr/local/bin/iotest mode 0100755\n'
    fi
} | debugfs -w -f - "$IMAGE" >"$WORKSPACE/inject.log" 2>&1
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

if ! grep -aq -- "$DONE_MARKER" "$CONSOLE_LOG"; then
    printf 'FAIL (the guest workload did not complete; see %s)\n' "$CONSOLE_LOG"
    exit 1
fi
if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the client did not finish; see %s)\n' "$CONSOLE_LOG"
    exit 1
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
