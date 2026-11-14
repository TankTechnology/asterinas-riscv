#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Refuse a kernel image that would leave the board dead before it deploys.
#
#   tools/riscv/board/preflight_kernel.sh <image> [--no-qemu] [--timeout s]
#
# Three classes of mistake have actually happened on this board, and each one
# costs a physical power cycle to recover from when it is discovered on the
# board instead of here:
#
#   * an image too large for the load region -- U-Boot loads the kernel at
#     0x80200000 and the DTB at 0x88000000 (boot.scr's as_kernel_addr and
#     as_dtb_addr), so anything past 126 MiB silently runs into the DTB;
#   * an image that is not a raw RISC-V Image at all (an ELF, a truncated
#     copy, a kernel built for another architecture);
#   * an image that panics before userspace -- boots, prints nothing useful,
#     and only the hardware watchdog brings the board back.
#
# The QEMU boot catches the third class for build and logic errors. It cannot
# catch everything: a change that is fine in QEMU but pathological on the
# EIC7700 (misaligned accesses trap and drop to 7 MiB/s) passes here, which is
# why deployment also keeps a rollback image. Use --no-qemu only when QEMU is
# not available; the structural checks still run.
#
# Exit status: 0 the image may be deployed, 1 it must not be.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)

# From boot.scr: `as_kernel_addr 0x80200000`, `as_dtb_addr 0x88000000`.
KERNEL_ADDR=$((0x80200000))
DTB_ADDR=$((0x88000000))
MAX_BYTES=$((DTB_ADDR - KERNEL_ADDR))
MIN_BYTES=$((1 << 20))

IMAGE=${1:?usage: preflight_kernel.sh <image> [--no-qemu] [--timeout seconds]}
shift
USE_QEMU=1
TIMEOUT=150
while [ $# -gt 0 ]; do
    case "$1" in
        --no-qemu) USE_QEMU=0 ;;
        --timeout) TIMEOUT=${2:?--timeout needs a value}; shift ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

failures=0
fail() { echo "FAIL $*"; failures=$((failures + 1)); }
ok() { echo "ok   $*"; }

echo "== structural checks"

if [ ! -f "$IMAGE" ]; then
    fail "$IMAGE does not exist"
    echo; echo "preflight: FAILED"; exit 1
fi

size=$(/usr/bin/stat -c %s "$IMAGE")
if [ "$size" -ge "$MIN_BYTES" ] && [ "$size" -le "$MAX_BYTES" ]; then
    ok "size $size bytes fits 0x80200000..0x88000000 ($MAX_BYTES max)"
else
    fail "size $size bytes outside $MIN_BYTES..$MAX_BYTES (load region 0x80200000..0x88000000)"
fi

# A raw RISC-V Image starts with a jump (opcode 0x6f). An ELF or a stray text
# file does not, and U-Boot would happily `booti` it into a dead board.
magic=$(od -An -tx1 -N4 "$IMAGE" | tr -d ' \n')
case "$magic" in
    7f454c46) fail "file is an ELF, not a raw RISC-V Image (look for the .Image output)" ;;
    *) if [ $((0x${magic:0:2} & 0x7f)) -eq $((0x6f)) ]; then
           ok "entry instruction is a jump (raw RISC-V Image)"
       else
           fail "first instruction is not a RISC-V jump (magic $magic)"
       fi ;;
esac

echo "== boot smoke test"

if [ "$USE_QEMU" -eq 0 ]; then
    echo "skip (--no-qemu)"
else
    WORK="$REPO/target/board/preflight"
    mkdir -p "$WORK"
    INIT_BIN="$WORK/init"
    INITRD="$WORK/initramfs.cpio"
    if [ ! -f "$INIT_BIN" ] || [ "$HERE/preflight/init.c" -nt "$INIT_BIN" ]; then
        if ! riscv64-linux-gnu-gcc -O2 -static -o "$INIT_BIN" "$HERE/preflight/init.c"; then
            fail "could not build the preflight init (riscv64-linux-gnu-gcc)"
        fi
    fi
    if [ -f "$INIT_BIN" ]; then
        # The cpio needs a root "." entry: without it the kernel's unpack
        # leaves the initramfs empty and the boot dies on a negative dentry,
        # which looks exactly like a bad kernel.
        rm -rf "$WORK/root" && mkdir -p "$WORK/root"
        cp "$INIT_BIN" "$WORK/root/init"
        ( cd "$WORK/root" && printf '.\ninit\n' | cpio -o -H newc 2>/dev/null ) >"$INITRD"

        log="$WORK/boot.log"
        rm -f "$log"
        timeout "$TIMEOUT" qemu-system-riscv64 -M virt -m 2G -smp 1 -nographic \
            -kernel "$IMAGE" -initrd "$INITRD" \
            -append "console=ttyS0 loglevel=info init=/init" \
            >"$log" 2>&1 &
        qemu_pid=$!
        reached=0
        for _ in $(seq 1 "$TIMEOUT"); do
            if grep -aq ASTERINAS_PREFLIGHT_USERSPACE_OK "$log"; then reached=1; break; fi
            if grep -aq "Uncaught panic" "$log"; then break; fi
            sleep 1
        done
        kill -9 "$qemu_pid" 2>/dev/null
        wait "$qemu_pid" 2>/dev/null

        if [ "$reached" -eq 1 ]; then
            ok "reached userspace"
        else
            fail "did not reach userspace within ${TIMEOUT}s"
            # The reason is what makes the failure actionable; show it.
            if grep -aq "Uncaught panic" "$log"; then
                grep -a -A 2 "Uncaught panic" "$log" | head -4 | sed 's/^/     /'
            else
                echo "     (no panic in the log; see $log)"
            fi
        fi
    else
        fail "no preflight init binary"
    fi
fi

echo
if [ "$failures" -eq 0 ]; then
    echo "preflight: PASSED  ($IMAGE)"
    exit 0
fi
echo "preflight: FAILED  ($failures check(s))"
echo "Do not deploy this image: the board would need a physical power cycle."
exit 1
