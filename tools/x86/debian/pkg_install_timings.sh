#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Measure what a user actually waits for when installing a package on the
# x86_64 Debian image: one boot, one apt transaction per package, wall-clock
# total plus the dpkg unpack/configure phases read out of /var/log/dpkg.log.
#
# This is the T3 probe. It does not replace apt_acceptance.sh: that gate proves
# a package survives a reboot, this one produces the "package -> seconds" table.
#
# The image is copied once. The guest probe runs as a oneshot unit after
# multi-user.target, points DNS at QEMU's slirp resolver, runs `apt-get update`
# once (outside every measurement), then for each package:
#
#   apt-get clean                 # make the download part of the measurement
#   apt-get install -y --no-install-recommends [--reinstall] PKG
#
# Packages already present in the m6-userspace image are measured with
# --reinstall and reported as mode=reinstall: their dependencies are already
# satisfied, so those rows are a local unpack/configure lower bound, not a
# first-install time. A package absent from the image is measured fresh and is
# the directly comparable row.
#
# The anchor row `sl` is installed last. PR #210 recorded `apt-get install sl`
# at 14.9 s on this kernel line; re-running it keeps the new table tied to the
# old number.
#
# Usage:
#   tools/x86/debian/pkg_install_timings.sh \
#     --kernel target/prime/kernels/x86.elf \
#     --initramfs target/x86-debian/m4/stage1/initramfs.cpio \
#     --base-root target/systemd-health/daily.ext2 \
#     --workspace target/t3/run1 \
#     --accel kvm \
#     --packages "git curl vim python3"
#
# Outputs under --workspace:
#   pkg-timings.ext2        the guest image after the run
#   transcript.txt          serial console with PKGT_* markers
#   timings.tsv             one row per package, scraped from the markers
#   guest/                  apt, dpkg and result logs dumped from the image
#
# --accel kvm is the default and is required for numbers comparable to the
# existing 14.9 s / 22.4 s baselines. TCG numbers are valid but much larger.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: pkg_install_timings.sh --kernel ELF --initramfs CPIO --base-root IMG
                              --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF built with
                      `--boot-method vmm-direct` (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy once (required).
  --workspace DIR     Directory for the image, transcript and result tables.
  --packages "LIST"   Space-separated packages (default: "git curl vim python3").
  --anchor NAME       Extra package measured last (default: sl). Empty disables.
  --settle SECONDS    Guest time before `apt-get update` (default: 10).
  --install-timeout S Per-package apt deadline (default: 900).
  --boot-timeout S    Whole-run deadline (default: 3600).
  --loglevel N        Kernel log level (default: 4).
  --accel MODE        kvm (default) or tcg.
  --qemu PATH         QEMU binary (default: qemu-system-x86_64).
  --keep-image        Do not delete the image on failure (always kept anyway;
                      this flag only documents the intent).
  --print-plan        Print the resolved plan and exit without booting.

Exit status is 0 when every requested package produced a PKGT marker and
PKGT_DONE appeared.
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
PACKAGES="git curl vim python3"
ANCHOR="sl"
SETTLE=10
INSTALL_TIMEOUT=900
BOOT_TIMEOUT=3600
LOGLEVEL=4
ACCEL="kvm"
QEMU="qemu-system-x86_64"
PRINT_PLAN=0

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --packages | \
            --anchor | --settle | --install-timeout | --boot-timeout | \
            --loglevel | --accel | --qemu)
            [[ $# -ge 2 ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --packages) PACKAGES="$2" ;;
                --anchor) ANCHOR="$2" ;;
                --settle) SETTLE="$2" ;;
                --install-timeout) INSTALL_TIMEOUT="$2" ;;
                --boot-timeout) BOOT_TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --accel) ACCEL="$2" ;;
                --qemu) QEMU="$2" ;;
            esac
            shift 2
            ;;
        --keep-image) shift ;;
        --print-plan) PRINT_PLAN=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$INITRAMFS" && -n "$BASE_ROOT" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --initramfs, --base-root and --workspace are required"
[[ "$SETTLE" =~ ^[0-9]+$ ]] || die_usage "--settle must be a non-negative integer"
[[ "$INSTALL_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--install-timeout must be positive"
[[ "$BOOT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--boot-timeout must be positive"
[[ "$ACCEL" == "kvm" || "$ACCEL" == "tcg" ]] || die_usage "--accel must be kvm or tcg"
[[ "$PACKAGES" =~ ^[a-z0-9][a-z0-9+.-]*(\ [a-z0-9][a-z0-9+.-]*)*$ ]] ||
    die_usage "unsafe package list: $PACKAGES"
[[ "$WORKSPACE" != *' '* && "$WORKSPACE" != *$'\n'* && "$WORKSPACE" != *$'\r'* ]] ||
    die_usage "--workspace must not contain whitespace"
[[ -z "$ANCHOR" || "$ANCHOR" =~ ^[a-z0-9][a-z0-9+.-]*$ ]] ||
    die_usage "unsafe anchor package: $ANCHOR"

for tool in debugfs e2fsck awk sed grep "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done
for path in "$KERNEL" "$INITRAMFS" "$BASE_ROOT"; do
    [[ -f "$path" ]] || die_usage "input is not a file: $path"
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BOOT_SCRIPT="$SCRIPT_DIR/boot_debian.sh"
[[ -f "$BOOT_SCRIPT" ]] || {
    printf 'error: boot script is missing: %s\n' "$BOOT_SCRIPT" >&2
    exit 1
}

IMAGE="$WORKSPACE/pkg-timings.ext2"
TRANSCRIPT="$WORKSPACE/transcript.txt"
TIMINGS="$WORKSPACE/timings.tsv"

if (( PRINT_PLAN )); then
    printf 'kernel      %s\n' "$KERNEL"
    printf 'initramfs   %s\n' "$INITRAMFS"
    printf 'base-root   %s\n' "$BASE_ROOT"
    printf 'workspace   %s\n' "$WORKSPACE"
    printf 'packages    %s\n' "$PACKAGES"
    printf 'anchor      %s\n' "${ANCHOR:-<none>}"
    printf 'accel       %s\n' "$ACCEL"
    exit 0
fi

mkdir -p -- "$WORKSPACE/guest"
rm -f -- "$IMAGE"
printf 'pkg-timings: copying %s -> %s\n' "$BASE_ROOT" "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"

PROBE_SCRIPT="$WORKSPACE/pkg-timings.sh"
PROBE_UNIT="$WORKSPACE/pkg-timings.service"

cat >"$PROBE_SCRIPT" <<PROBE
#!/bin/sh
# Guest side of tools/x86/debian/pkg_install_timings.sh.
set -u
export DEBIAN_FRONTEND=noninteractive
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PKG_TIMINGS_DIR=/opt/pkg-timings
PKG_LIST='$PACKAGES'
ANCHOR='$ANCHOR'
INSTALL_TIMEOUT=$INSTALL_TIMEOUT
mkdir -p "\$PKG_TIMINGS_DIR"
: >"\$PKG_TIMINGS_DIR/results.txt"

marker() {
    printf '%s\n' "\$*" >/dev/console
    printf '%s\n' "\$*" >>"\$PKG_TIMINGS_DIR/results.txt"
}

# Phase length in whole seconds. dpkg logs second-resolution timestamps, so the
# value carries +/-1 s.
#
# dpkg.log has no " unpack " action line: the extraction window runs from the
# install/upgrade action to the last status-unpacked line before the
# startup-packages-configure line. Configuration runs from that startup line
# to the last status-installed line. Matching the bare words unpack or
# configure silently yields "na", which is why the first T3b run reported no
# unpack time.
#
# Do not use backticks in a comment here: this block is inside an unquoted
# heredoc, so backticks would run on the host while the probe is generated.
phase_seconds() {
    file=\$1
    key=\$2
    case "\$key" in
    unpack)
        first=\$(grep -m1 -E ' (install|upgrade) ' "\$file" 2>/dev/null |
            cut -d' ' -f1,2)
        last=\$(awk '/ startup packages configure/ { exit }
            / status unpacked / { stamp = \$1 " " \$2 }
            END { print stamp }' "\$file" 2>/dev/null)
        ;;
    configure)
        first=\$(grep -m1 ' startup packages configure' "\$file" 2>/dev/null |
            cut -d' ' -f1,2)
        last=\$(grep ' status installed ' "\$file" 2>/dev/null | tail -1 |
            cut -d' ' -f1,2)
        ;;
    *)
        printf 'na'
        return
        ;;
    esac
    if [ -z "\$first" ] || [ -z "\$last" ]; then
        printf 'na'
        return
    fi
    a=\$(date -d "\$first" +%s 2>/dev/null) || { printf 'na'; return; }
    b=\$(date -d "\$last" +%s 2>/dev/null) || { printf 'na'; return; }
    printf '%s' \$((b - a))
}

# One DNS answer for the whole run. QEMU user networking always forwards DNS
# to 10.0.2.3, and the image cannot carry a host-specific resolver.
printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf
sleep $SETTLE

marker "PKGT_BEGIN packages=\$PKG_LIST anchor=\$ANCHOR install_timeout=\$INSTALL_TIMEOUT"
echo "PKGT_UPDATE_BEGIN" >/dev/console
update_ok=0
for attempt in 1 2 3; do
    if timeout 300 apt-get update >"\$PKG_TIMINGS_DIR/apt-update-\$attempt.log" 2>&1 </dev/null; then
        update_ok=1
        break
    fi
    echo "PKGT_UPDATE_RETRY attempt=\$attempt" >/dev/console
    sleep 5
done
echo "PKGT_UPDATE_RC rc=\$update_ok" >/dev/console

measure_one() {
    pkg=\$1
    pre=absent
    if dpkg-query -W -f='\${Status}' "\$pkg" 2>/dev/null | grep -q 'install ok installed'; then
        pre=installed
    fi
    if [ "\$pre" = installed ]; then
        mode=reinstall
        reinstall=--reinstall
    else
        mode=fresh
        reinstall=
    fi

    # A user installing a package for the first time pays the download. Do the
    # same: empty the apt archive before each transaction.
    apt-get clean >/dev/null 2>&1 || true
    offset=\$(wc -l </var/log/dpkg.log 2>/dev/null || echo 0)

    start=\$(date +%s.%N)
    # shellcheck disable=SC2086 # reinstall is intentionally word-split
    timeout "\$INSTALL_TIMEOUT" apt-get install -y --no-install-recommends \\
        \$reinstall "\$pkg" >"\$PKG_TIMINGS_DIR/\$pkg.apt.log" 2>&1 </dev/null
    rc=\$?
    end=\$(date +%s.%N)
    total=\$(awk -v a="\$start" -v b="\$end" 'BEGIN{printf "%.2f", b-a}')

    tail -n +"\$((offset + 1))" /var/log/dpkg.log >"\$PKG_TIMINGS_DIR/\$pkg.dpkg.log" 2>/dev/null || true
    unpack=\$(phase_seconds "\$PKG_TIMINGS_DIR/\$pkg.dpkg.log" unpack)
    configure=\$(phase_seconds "\$PKG_TIMINGS_DIR/\$pkg.dpkg.log" configure)

    status=\$(dpkg-query -W -f='\${Status} \${Version}' "\$pkg" 2>&1 | tr '\n' ' ' | tr -s ' ' | tr ' ' '_')
    fetched=\$(grep -E '^Fetched ' "\$PKG_TIMINGS_DIR/\$pkg.apt.log" 2>/dev/null | tail -1 | tr ' ' '_')
    err=\$(grep -E '^(E:|dpkg:|Err:)' "\$PKG_TIMINGS_DIR/\$pkg.apt.log" 2>/dev/null | tail -1 | tr ' ' '_')

    marker "PKGT pkg=\$pkg pre=\$pre mode=\$mode rc=\$rc total=\$total dpkg_unpack=\$unpack dpkg_configure=\$configure fetched=\${fetched:-none} status=\$status err=\${err:-none}"

    if command -v "\$pkg" >/dev/null 2>&1; then
        smoke=\$(timeout 20 "\$pkg" --version 2>&1 | head -1 | tr ' ' '_')
        marker "PKGT_SMOKE pkg=\$pkg out=\$(printf '%s' "\$smoke" | cut -c1-100)"
    fi
}

for pkg in \$PKG_LIST; do
    measure_one "\$pkg"
done
if [ -n "\$ANCHOR" ]; then
    measure_one "\$ANCHOR"
fi

marker "PKGT_DONE"
sync
systemctl poweroff --no-block
PROBE

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=T3 package install timing probe
After=multi-user.target network-online.target
Wants=network-online.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/pkg-timings.sh
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
UNIT

printf 'pkg-timings: injecting probe into %s\n' "$IMAGE"
debugfs -w -f - "$IMAGE" >"$WORKSPACE/inject.log" 2>&1 <<DEBUG
rm /opt/pkg-timings.sh
rm /etc/systemd/system/pkg-timings.service
rm /etc/systemd/system/multi-user.target.wants/pkg-timings.service
write $PROBE_SCRIPT /opt/pkg-timings.sh
sif /opt/pkg-timings.sh mode 0100755
sif /opt/pkg-timings.sh uid 0
sif /opt/pkg-timings.sh gid 0
write $PROBE_UNIT /etc/systemd/system/pkg-timings.service
sif /etc/systemd/system/pkg-timings.service mode 0100644
symlink /etc/systemd/system/multi-user.target.wants/pkg-timings.service /etc/systemd/system/pkg-timings.service
rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service
rm /etc/systemd/system/asterinas-debian-ext4.service
DEBUG

if grep -q 'Ext2 file already exists' "$WORKSPACE/inject.log"; then
    printf 'error: debugfs could not replace a file; see %s\n' "$WORKSPACE/inject.log" >&2
    exit 1
fi
e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1 || {
    printf 'error: image is not clean after injection; see %s\n' "$WORKSPACE/fsck-prepared.txt" >&2
    exit 1
}

printf 'pkg-timings: booting (%s)\n' "$ACCEL"
bash "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$TRANSCRIPT" \
    --wait-for "PKGT_DONE" \
    --timeout "$BOOT_TIMEOUT" \
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$QEMU" || {
    printf 'error: the guest did not reach PKGT_DONE; see %s\n' "$TRANSCRIPT" >&2
    printf 'last PKGT lines:\n' >&2
    grep -a '^PKGT' "$TRANSCRIPT" | tail -20 >&2 || true
    exit 1
}

# Pull the per-package logs back so a row can always be traced to apt/dpkg.
for pkg in $PACKAGES $ANCHOR; do
    for suffix in apt.log dpkg.log; do
        debugfs -R "dump /opt/pkg-timings/$pkg.$suffix $WORKSPACE/guest/$pkg.$suffix" "$IMAGE" \
            >/dev/null 2>&1 || true
    done
done
debugfs -R "dump /opt/pkg-timings/results.txt $WORKSPACE/guest/results.txt" "$IMAGE" \
    >/dev/null 2>&1 || true
debugfs -R "dump /var/log/dpkg.log $WORKSPACE/guest/dpkg.log" "$IMAGE" \
    >/dev/null 2>&1 || true

{
    printf 'package\tpre\tmode\trc\ttotal_s\tdpkg_unpack_s\tdpkg_configure_s\tfetched\tstatus\terr\n'
    grep -a '^PKGT pkg=' "$TRANSCRIPT" | sed -e 's/^PKGT //' -e 's/ /\t/g'
} >"$TIMINGS"

printf 'pkg-timings: %s\n' "$TIMINGS"
cat "$TIMINGS"
