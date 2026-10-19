#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Package-management acceptance: boot a Debian image, install a package with
# apt/dpkg, run its binary, shut down, and boot the same image again to prove
# that the package database and the installed files survived the reboot.
#
# The gate covers the paths package management stresses beyond downloading:
# dpkg unpacking into htree-indexed directories, atomic renames, fsync of
# freshly written files, and updates of /var/lib/dpkg/status.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: apt_acceptance.sh --kernel ELF --initramfs CPIO --base-root IMG
                         --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy once (required).
  --workspace DIR     Directory for the image, manifests and transcripts.
  --package NAME      Package to install (default: hello).
  --command PATH      Binary that the package installs (default: /usr/bin/hello).
  --expect TEXT       Substring the command must print (default: Hello, world!).
  --boots N           Consecutive boots; boot 1 installs (default: 2).
  --settle SECONDS    Guest time before starting (default: 10).
  --timeout SECONDS   Per-boot deadline (default: 1800).
  --loglevel N        Kernel log level (default: 5, which shows warnings).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Boot 1 runs `apt-get update` and `apt-get install -y --no-install-recommends`
for the package, then runs the command and writes a manifest. Every later boot
only runs the command. The host checks, for every boot, that dpkg reports the
package installed, the command exits 0 with the expected output, the binary's
SHA-256 matches, and `e2fsck -fn` still accepts the image.

Exit status is 0 only if every boot passes.
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
PACKAGE="hello"
COMMAND="/usr/bin/hello"
EXPECT="Hello, world!"
BOOTS=2
SETTLE=10
TIMEOUT=1800
LOGLEVEL=5
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --package | \
            --command | --expect | --boots | --settle | --timeout | \
            --loglevel | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --package) PACKAGE="$2" ;;
                --command) COMMAND="$2" ;;
                --expect) EXPECT="$2" ;;
                --boots) BOOTS="$2" ;;
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
[[ "$BOOTS" =~ ^[1-9][0-9]*$ ]] || die_usage "--boots must be a positive integer"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$ACCEL" == "tcg" || "$ACCEL" == "kvm" ]] ||
    die_usage "--accel must be 'tcg' or 'kvm'"
[[ "$PACKAGE" =~ ^[a-z0-9][a-z0-9+.-]*$ ]] || die_usage "unsafe package name"
[[ "$COMMAND" == /* ]] || die_usage "--command must be an absolute path"
[[ "$EXPECT" != *$'\n'* && "$EXPECT" != *"'"* ]] ||
    die_usage "--expect must not contain newlines or single quotes"

for tool in debugfs e2fsck sha256sum "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
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
IMAGE="$WORKSPACE/apt.ext2"
PROBE_SCRIPT="$WORKSPACE/apt-probe.sh"
PROBE_UNIT="$WORKSPACE/apt-probe.service"

rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"

cat >"$PROBE_SCRIPT" <<PROBE
#!/bin/sh
echo "APT_PROBE_BEGIN"
sleep $SETTLE
mkdir -p /opt/apt-verify
N=\$(cat /opt/apt-verify/count 2>/dev/null || echo 0)
case "\$N" in ''|*[!0-9]*) N=0 ;; esac
N=\$((N + 1))
echo "\$N" > /opt/apt-verify/count
printf 'nameserver 10.0.2.3\n' > /etc/resolv.conf
{
    echo "APT_BOOT \$N"
    if [ "\$N" = 1 ]; then
        apt-get update > /tmp/apt-update.log 2>&1
        echo "APT_UPDATE_RC \$?"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
            $PACKAGE > /tmp/apt-install.log 2>&1
        echo "APT_INSTALL_RC \$?"
        tail -5 /tmp/apt-install.log
    fi
    echo "APT_STATUS \$(dpkg-query -W -f='\${Status} \${Version}' $PACKAGE 2>&1)"
    OUT=\$($COMMAND 2>&1)
    echo "APT_RUN_RC \$?"
    echo "APT_RUN_OUT \$OUT"
    if echo "\$OUT" | grep -qF -- '$EXPECT'; then
        echo "APT_EXPECT_OK"
    else
        echo "APT_EXPECT_FAIL"
    fi
    echo "APT_BIN_SHA \$(sha256sum $COMMAND 2>/dev/null | cut -d' ' -f1)"
} > /opt/apt-verify/boot-\$N.txt 2>&1
cat /opt/apt-verify/boot-\$N.txt
echo "APT_PROBE_END"
sync
systemctl poweroff --no-block
PROBE

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=apt install acceptance probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/apt-probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

debugfs -w -f - "$IMAGE" >/dev/null 2>&1 <<DEBUG
rm /opt/apt-probe.sh
rm /etc/systemd/system/apt-probe.service
rm /etc/systemd/system/multi-user.target.wants/apt-probe.service
write $PROBE_SCRIPT /opt/apt-probe.sh
sif /opt/apt-probe.sh mode 0100755
sif /opt/apt-probe.sh uid 0
sif /opt/apt-probe.sh gid 0
write $PROBE_UNIT /etc/systemd/system/apt-probe.service
symlink /etc/systemd/system/multi-user.target.wants/apt-probe.service /etc/systemd/system/apt-probe.service
rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service
rm /etc/systemd/system/asterinas-debian-ext4.service
DEBUG

dump_file() {
    # dump_file <guest path> <local path>
    local guest="$1" local_path="$2"
    rm -f -- "$local_path"
    debugfs -R "dump $guest $local_path" "$IMAGE" >/dev/null 2>&1 || return 1
    [[ -f "$local_path" ]]
}

verify_manifest() {
    # verify_manifest <boot> <manifest>
    local boot="$1" manifest="$2"
    local failures=0
    local expect_boot="APT_BOOT $boot"

    if ! grep -qxF "$expect_boot" "$manifest"; then
        printf 'boot %d: FAIL (manifest does not describe boot %d)\n' "$boot" "$boot"
        return 1
    fi
    if ! grep -q '^APT_STATUS install ok installed ' "$manifest"; then
        printf 'boot %d: FAIL (dpkg does not report the package installed)\n' "$boot"
        grep '^APT_STATUS ' "$manifest" | sed 's/^/    /'
        failures=$((failures + 1))
    fi
    if ! grep -qx 'APT_RUN_RC 0' "$manifest"; then
        printf 'boot %d: FAIL (command did not exit 0)\n' "$boot"
        grep '^APT_RUN_RC ' "$manifest" | sed 's/^/    /'
        failures=$((failures + 1))
    fi
    if ! grep -qx 'APT_EXPECT_OK' "$manifest"; then
        printf 'boot %d: FAIL (command output does not contain "%s")\n' "$boot" "$EXPECT"
        grep '^APT_RUN_OUT ' "$manifest" | sed 's/^/    /'
        failures=$((failures + 1))
    fi
    if [[ "$boot" == 1 ]]; then
        if ! grep -qx 'APT_UPDATE_RC 0' "$manifest"; then
            printf 'boot 1: FAIL (apt-get update failed)\n'
            failures=$((failures + 1))
        fi
        if ! grep -qx 'APT_INSTALL_RC 0' "$manifest"; then
            printf 'boot 1: FAIL (apt-get install failed)\n'
            grep '^APT_INSTALL_RC ' "$manifest" | sed 's/^/    /'
            failures=$((failures + 1))
        fi
    fi

    local guest_sha host_sha
    guest_sha=$(sed -n 's/^APT_BIN_SHA //p' "$manifest" | tail -1)
    if ! dump_file "$COMMAND" "$WORKSPACE/binary.dump"; then
        printf 'boot %d: FAIL (%s is missing from the image)\n' "$boot" "$COMMAND"
        return 1
    fi
    host_sha=$(sha256sum "$WORKSPACE/binary.dump" | cut -d' ' -f1)
    if [[ -z "$guest_sha" || "$guest_sha" != "$host_sha" ]]; then
        printf 'boot %d: FAIL (binary checksum %s, expected %s)\n' \
            "$boot" "${guest_sha:-missing}" "$host_sha"
        failures=$((failures + 1))
    fi
    return "$failures"
}

failures=0
for boot in $(seq 1 "$BOOTS"); do
    printf 'boot %d: starting\n' "$boot"
    transcript="$WORKSPACE/boot-$boot/transcript.txt"
    mkdir -p -- "$WORKSPACE/boot-$boot"
    if ! timeout "$TIMEOUT" "$BOOT_SCRIPT" \
        --kernel "$KERNEL" \
        --initramfs "$INITRAMFS" \
        --root-image "$IMAGE" \
        --output "$transcript" \
        --loglevel "$LOGLEVEL" \
        --accel "$ACCEL" \
        --qemu "$QEMU" \
        --wait-for "Powering off."; then
        printf 'boot %d: FAIL (the guest did not shut down cleanly)\n' "$boot"
        failures=$((failures + 1))
        continue
    fi

    manifest="$WORKSPACE/boot-$boot/manifest.txt"
    if ! dump_file "/opt/apt-verify/boot-$boot.txt" "$manifest"; then
        printf 'boot %d: FAIL (the guest did not write a manifest)\n' "$boot"
        failures=$((failures + 1))
        continue
    fi
    if verify_manifest "$boot" "$manifest"; then
        printf 'boot %d: package installed and runnable\n' "$boot"
    else
        failures=$((failures + 1))
    fi

    if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/boot-$boot/fsck-after.txt" 2>&1; then
        # e2fsck exits 4 for "errors left uncorrected"; anything non-zero is a
        # failure for a cleanly shut-down image.
        printf 'boot %d: FAIL (e2fsck -fn reported problems; see %s)\n' \
            "$boot" "$WORKSPACE/boot-$boot/fsck-after.txt"
        failures=$((failures + 1))
    fi
done

if (( failures > 0 )); then
    printf '%d check(s) failed\n' "$failures" >&2
    exit 1
fi
printf 'all %d boot(s) passed\n' "$BOOTS"
