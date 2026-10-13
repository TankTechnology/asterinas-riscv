#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Boot a Debian root image on the x86_64 Asterinas kernel through the Stage1
# root handoff.
#
# The RISC-V workflow boots `-kernel`/`-initrd` through U-Boot. x86 has no
# equivalent pre-UEFI handoff here, but QEMU can load a multiboot2 ELF itself:
# `-kernel` takes the Asterinas ELF (OSDK patches its `e_machine` to EM_386 for
# exactly this) and `-initrd` becomes the first multiboot2 module, which the
# kernel magic-sniffs as a plain cpio. Stage1 then finds the Debian disk by its
# ext4 volume label, so the disk may sit on any virtio-blk slot.
#
# The boot command line is passed here rather than through the OSDK scheme:
# `[run.boot]` in OSDK.toml always appends `sh -l` to the init arguments for
# the standard test initramfs, and Stage1's argument parser rejects it.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: boot_debian.sh --kernel ELF --initramfs CPIO --root-image IMG [OPTIONS]
       boot_debian.sh --print-argv [OPTIONS]
       boot_debian.sh --print-append [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --root-image PATH   Debian ext2/ext4 root image (required).
  --output PATH       Transcript file (default: target/x86-debian/boot.transcript).
  --wait-for TEXT     Exit successfully once TEXT appears in the transcript.
  --timeout SECONDS   Deadline for --wait-for (default: 1800).
  --memory SIZE       Guest memory (default: 4G).
  --smp N             Guest CPUs (default: 4).
  --loglevel N        Kernel log level (default: 4; the guest prints a line per
                      syscall above this and the boot slows down sharply).
  --qemu PATH         QEMU binary (default: qemu-system-x86_64).

Exit status is 0 when --wait-for is satisfied, 1 when the deadline passes
first, and QEMU's own status when the guest exits.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
INITRAMFS=""
ROOT_IMAGE=""
OUTPUT="target/x86-debian/boot.transcript"
WAIT_FOR=""
TIMEOUT=1800
MEMORY="4G"
SMP=4
LOGLEVEL=4
QEMU="qemu-system-x86_64"
PRINT_MODE=""

while (( $# > 0 )); do
    case "$1" in
        --print-argv | --print-append)
            [[ -z "$PRINT_MODE" ]] || die_usage "duplicate print argument: $1"
            PRINT_MODE="$1"
            shift
            ;;
        --wait-for | --timeout | --output | --kernel | --initramfs | --root-image | \
            --memory | --smp | --loglevel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --root-image) ROOT_IMAGE="$2" ;;
                --output) OUTPUT="$2" ;;
                --wait-for) WAIT_FOR="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --memory) MEMORY="$2" ;;
                --smp) SMP="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --qemu) QEMU="$2" ;;
            esac
            shift 2
            ;;
        *)
            die_usage "unknown argument: $1"
            ;;
    esac
done

[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$SMP" =~ ^[1-9][0-9]*$ ]] || die_usage "--smp must be a positive integer"
[[ "$LOGLEVEL" =~ ^[0-9]+$ ]] || die_usage "--loglevel must be a non-negative integer"
[[ "$MEMORY" =~ ^[1-9][0-9]*[KMG]?$ ]] || die_usage "unsafe --memory value: $MEMORY"

[[ -n "$KERNEL" ]] || die_usage "--kernel is required"
[[ -n "$INITRAMFS" ]] || die_usage "--initramfs is required"
[[ -n "$ROOT_IMAGE" ]] || die_usage "--root-image is required"
for path in "$KERNEL" "$INITRAMFS" "$ROOT_IMAGE" "$OUTPUT"; do
    [[ "$path" != *$'\n'* && "$path" != *$'\r'* && "$path" != *' '* ]] ||
        die_usage "path must not contain whitespace or newlines: $path"
done

#: The guest console the kernel is told to use. QEMU wires `hdvc0` to the same
#: chardev as the transcript, so the guest's kernel log and Stage1's own
#: `DEBIAN_STAGE1_PROGRESS` markers arrive on one stream.
KERNEL_APPEND="console=hvc0 loglevel=$LOGLEVEL init=/init -- --root-init=systemd --root-fs=ext4"

qemu_argv() {
    printf '%s\n' \
        -cpu Icelake-Server \
        -smp "$SMP" \
        -m "$MEMORY" \
        --no-reboot \
        -display none \
        -monitor none \
        -serial none \
        -chardev stdio,id=mux,mux=on,signal=off \
        -device virtio-serial-pci \
        -device virtconsole,chardev=mux \
        -netdev user,id=net01 \
        -device virtio-net-pci,netdev=net01,disable-legacy=on,disable-modern=off \
        -drive "if=none,format=raw,id=rootdisk,file=$ROOT_IMAGE" \
        -device virtio-blk-pci,drive=rootdisk \
        -kernel "$KERNEL" \
        -initrd "$INITRAMFS" \
        -append "$KERNEL_APPEND"
}

if [[ "$PRINT_MODE" == "--print-append" ]]; then
    printf '%s\n' "$KERNEL_APPEND"
    exit 0
fi

if [[ "$PRINT_MODE" == "--print-argv" ]]; then
    mapfile -t argv < <(qemu_argv)
    printf '%s\n' "$QEMU" "${argv[@]}"
    exit 0
fi

command -v "$QEMU" >/dev/null 2>&1 || {
    printf 'error: required tool not found: %s\n' "$QEMU" >&2
    exit 1
}
[[ -f "$KERNEL" ]] || die_usage "kernel image is missing: $KERNEL"
[[ -f "$INITRAMFS" ]] || die_usage "initramfs is missing: $INITRAMFS"
[[ -f "$ROOT_IMAGE" ]] || die_usage "root image is missing: $ROOT_IMAGE"

OUTPUT_DIRECTORY="$(dirname -- "$OUTPUT")"
mkdir -p -- "$OUTPUT_DIRECTORY"
: >"$OUTPUT"

mapfile -t argv < <(qemu_argv)
"$QEMU" "${argv[@]}" >"$OUTPUT" 2>&1 &
qemu_pid=$!

# The transcript is the only channel: the guest root filesystem does not
# durably retain what the guest writes, so nothing is read back from the disk.
stop_qemu() {
    if kill -0 "$qemu_pid" 2>/dev/null; then
        kill -TERM "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
}
trap stop_qemu EXIT INT TERM

if [[ -z "$WAIT_FOR" ]]; then
    wait "$qemu_pid"
    status=$?
    trap - EXIT
    exit "$status"
fi

deadline=$((SECONDS + TIMEOUT))
while (( SECONDS < deadline )); do
    if ! kill -0 "$qemu_pid" 2>/dev/null; then
        wait "$qemu_pid" 2>/dev/null || true
        printf 'error: the guest exited before "%s" appeared\n' "$WAIT_FOR" >&2
        exit 1
    fi
    if grep -aFq -- "$WAIT_FOR" "$OUTPUT"; then
        printf 'boot_debian: transcript %s satisfied "%s"\n' "$OUTPUT" "$WAIT_FOR"
        trap - EXIT
        stop_qemu
        exit 0
    fi
    sleep 2
done

printf 'error: "%s" did not appear within %s seconds\n' "$WAIT_FOR" "$TIMEOUT" >&2
exit 1
