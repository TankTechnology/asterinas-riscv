#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Storage persistence acceptance: boot the same Debian image several times,
# write a known workload from inside the guest on every boot, shut down
# cleanly, and verify from the host that
#
#   * the new writes are on disk with the expected contents,
#   * everything written by earlier boots is still intact,
#   * the cumulative log and boot counter survived, and
#   * `e2fsck -fn` still accepts the image.
#
# Unlike fsck_acceptance.sh (which starts every run from a fresh copy and only
# checks consistency), this harness boots one image repeatedly, so a write that
# silently disappears or comes back with different bytes fails the gate.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: persistence_acceptance.sh --kernel ELF --initramfs CPIO --base-root IMG
                                 --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext2/ext4 image to copy once (required).
  --workspace DIR     Directory for the image, manifests and transcripts.
  --boots N           Number of consecutive boots (default: 3).
  --timeout SECONDS   Per-boot deadline (default: 1800).
  --loglevel N        Kernel log level (default: 5).
  --settle SECONDS    Guest time before writing (default: 15).
  --accel MODE        'kvm' or 'tcg' (default: tcg).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

Exit status is 0 only if every boot's writes are durable, all earlier writes
are intact, and the image passes `e2fsck -fn`.
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
BOOTS=3
TIMEOUT=1800
LOGLEVEL=5
SETTLE=15
ACCEL="tcg"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --boots | \
            --timeout | --loglevel | --settle | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --boots) BOOTS="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --settle) SETTLE="$2" ;;
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
IMAGE="$WORKSPACE/persist.ext2"
EXPECTED="$WORKSPACE/expected.txt"
: >"$EXPECTED"

PROBE_SCRIPT="$WORKSPACE/persist-probe.sh"
PROBE_UNIT="$WORKSPACE/persist-probe.service"

cat >"$PROBE_SCRIPT" <<PROBE
#!/bin/sh
echo "PERSIST_BEGIN"
sleep $SETTLE
mkdir -p /opt/persist
N=\$(cat /opt/persist/boot-count 2>/dev/null || echo 0)
case "\$N" in ''|*[!0-9]*) N=0 ;; esac
N=\$((N + 1))
echo "\$N" > /opt/persist/boot-count
D=/opt/persist/boot-\$N
mkdir -p "\$D/sub"
printf 'boot %s\\n' "\$N" > "\$D/small.txt"
dd if=/dev/urandom of="\$D/random.bin" bs=1 count=4097 2>/dev/null
dd if=/dev/urandom of="\$D/large.bin" bs=1024 count=1024 2>/dev/null
printf 'nested %s\\n' "\$N" > "\$D/sub/nested.txt"
ln -sf small.txt "\$D/link-soft"
ln "\$D/small.txt" "\$D/link-hard"
chmod 640 "\$D/small.txt"
echo "line \$N" >> /opt/persist/log.txt
python3 - "\$D" <<'PY' 2>/dev/null || true
import os, sys
d = sys.argv[1]
for root, dirs, files in os.walk(d):
    for name in files + dirs:
        try:
            fd = os.open(os.path.join(root, name), os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
        except OSError:
            pass
for path in (d, '/opt/persist'):
    try:
        fd = os.open(path, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass
PY
sync
{
    echo "PERSIST_COUNT \$N"
    for f in small.txt random.bin large.bin sub/nested.txt; do
        printf 'PERSIST_FILE %s %s %s\\n' "\$N" "\$f" "\$(sha256sum "\$D/\$f" | cut -d' ' -f1)"
    done
    echo "PERSIST_LOG_LINES \$N"
} > "\$D/manifest.txt"
sync
cat "\$D/manifest.txt"
echo "PERSIST_DONE"
sync
sleep 2
systemctl poweroff --no-block
PROBE

cat >"$PROBE_UNIT" <<'UNIT'
[Unit]
Description=Storage persistence probe
After=multi-user.target

[Service]
Type=oneshot
KeyringMode=inherit
ExecStart=/opt/persist-probe.sh
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

# Prepare the one image every boot shares.
rm -f -- "$IMAGE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
debugfs -w -f - "$IMAGE" >/dev/null 2>&1 <<DEBUG
rm /opt/persist-probe.sh
rm /etc/systemd/system/persist-probe.service
rm /etc/systemd/system/multi-user.target.wants/persist-probe.service
write $PROBE_SCRIPT /opt/persist-probe.sh
sif /opt/persist-probe.sh mode 0100755
sif /opt/persist-probe.sh uid 0
sif /opt/persist-probe.sh gid 0
write $PROBE_UNIT /etc/systemd/system/persist-probe.service
symlink /etc/systemd/system/multi-user.target.wants/persist-probe.service /etc/systemd/system/persist-probe.service
rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service
rm /etc/systemd/system/asterinas-debian-ext4.service
DEBUG

dump_file() {
    # dump_file <image> <guest path> <local path>
    local image="$1" guest="$2" local_path="$3"
    rm -f -- "$local_path"
    debugfs -R "dump $guest $local_path" "$image" >/dev/null 2>&1 || return 1
    [[ -f "$local_path" ]]
}

guest_stat() {
    debugfs -R "stat $2" "$1" 2>/dev/null
}

verify_expected() {
    # verify_expected <boot> <manifest of this boot> <dir>
    local boot="$1" manifest="$2" dir="$3"
    local failures=0 line count rel sha local_path got
    local image="$IMAGE"

    count=$(sed -nE 's/^PERSIST_COUNT ([0-9]+).*/\1/p' "$manifest" | tail -1)
    if [[ "$count" != "$boot" ]]; then
        printf 'boot %d: FAIL (guest counter is %s, expected %d)\n' \
            "$boot" "${count:-missing}" "$boot"
        return 1
    fi

    # This boot's files, and every file an earlier boot reported.
    while read -r b rel sha; do
        local_path="$dir/dump.bin"
        if ! dump_file "$image" "/opt/persist/boot-$b/$rel" "$local_path"; then
            printf 'boot %d: FAIL (/opt/persist/boot-%s/%s is missing)\n' \
                "$boot" "$b" "$rel"
            failures=$((failures + 1))
            continue
        fi
        got=$(sha256sum "$local_path" | cut -d' ' -f1)
        if [[ "$got" != "$sha" ]]; then
            printf 'boot %d: FAIL (/opt/persist/boot-%s/%s checksum %s, expected %s)\n' \
                "$boot" "$b" "$rel" "$got" "$sha"
            failures=$((failures + 1))
        fi
    done <"$EXPECTED"

    # The cumulative log must contain one line per boot, in order.
    if dump_file "$image" /opt/persist/log.txt "$dir/dump-log.txt"; then
        local lines
        lines=$(wc -l <"$dir/dump-log.txt")
        if [[ "$lines" != "$boot" ]]; then
            printf 'boot %d: FAIL (/opt/persist/log.txt has %s lines)\n' \
                "$boot" "$lines"
            failures=$((failures + 1))
        fi
        if ! grep -qx "line $boot" "$dir/dump-log.txt"; then
            printf 'boot %d: FAIL (/opt/persist/log.txt is missing "line %d")\n' \
                "$boot" "$boot"
            failures=$((failures + 1))
        fi
    else
        printf 'boot %d: FAIL (/opt/persist/log.txt is missing)\n' "$boot"
        failures=$((failures + 1))
    fi

    # The inode and link structure of this boot's directory.
    local stat_out
    stat_out=$(guest_stat "$image" "/opt/persist/boot-$boot/small.txt")
    if ! grep -q "Mode:  0640" <<<"$stat_out"; then
        printf 'boot %d: FAIL (small.txt mode is not 0640)\n' "$boot"
        failures=$((failures + 1))
    fi
    local inode_small inode_hard
    inode_small=$(sed -n 's/^Inode: \([0-9]*\).*/\1/p' <<<"$stat_out" | head -1)
    inode_hard=$(sed -n 's/^Inode: \([0-9]*\).*/\1/p' \
        <<<"$(guest_stat "$image" "/opt/persist/boot-$boot/link-hard")" | head -1)
    if [[ -z "$inode_small" || "$inode_small" != "$inode_hard" ]]; then
        printf 'boot %d: FAIL (hard link does not share the inode)\n' "$boot"
        failures=$((failures + 1))
    fi
    if ! grep -q "small.txt" \
        <<<"$(guest_stat "$image" "/opt/persist/boot-$boot/link-soft")"; then
        printf 'boot %d: FAIL (symlink target changed)\n' "$boot"
        failures=$((failures + 1))
    fi

    return $(( failures == 0 ? 0 : 1 ))
}

failures=0
for ((boot = 1; boot <= BOOTS; boot++)); do
    dir="$WORKSPACE/boot-$boot"
    transcript="$dir/transcript.txt"
    mkdir -p -- "$dir"

    printf 'boot %d: starting\n' "$boot"
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

    sed -E 's/\x1b\[[0-9;]*m//g' "$transcript" \
        | grep -aE "PERSIST_(COUNT|FILE|LOG_LINES|DONE)" \
        | sed -E 's/^.*(PERSIST_[A-Z_]+)/\1/' >"$dir/manifest.txt" || true
    if ! grep -q '^PERSIST_DONE' "$dir/manifest.txt"; then
        printf 'boot %d: FAIL (the guest never completed the workload)\n' "$boot"
        failures=$((failures + 1))
        continue
    fi

    # The console interleaves kernel messages with the service output, so the
    # numbers there cannot be parsed reliably. Read the manifest the guest
    # wrote instead; it is what the persistence check is about anyway.
    if ! dump_file "$IMAGE" "/opt/persist/boot-$boot/manifest.txt" \
        "$dir/manifest_image.txt"; then
        printf 'boot %d: FAIL (the guest manifest file is missing)\n' "$boot"
        failures=$((failures + 1))
        continue
    fi

    # Fold this boot's file hashes into the cumulative expectation.
    sed -nE 's/^PERSIST_FILE ([0-9]+) ([^ ]+) ([0-9a-f]{64}).*/\1 \2 \3/p' \
        "$dir/manifest_image.txt" >>"$EXPECTED"

    if verify_expected "$boot" "$dir/manifest_image.txt" "$dir"; then
        printf 'boot %d: writes durable, earlier boots intact\n' "$boot"
    else
        failures=$((failures + 1))
    fi

    if e2fsck -fn "$IMAGE" >"$dir/fsck-after.txt" 2>&1; then
        printf 'boot %d: e2fsck -fn clean\n' "$boot"
    else
        printf 'boot %d: FAIL (e2fsck -fn reported problems; see %s)\n' \
            "$boot" "$dir/fsck-after.txt"
        failures=$((failures + 1))
    fi
done

if (( failures != 0 )); then
    printf '%d failure(s) across %d boot(s)\n' "$failures" "$BOOTS" >&2
    exit 1
fi
printf 'all %d boot(s) passed\n' "$BOOTS"
