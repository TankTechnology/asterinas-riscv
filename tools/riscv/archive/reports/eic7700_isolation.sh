#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# EIC7700 isolation verification: pure QEMU, no physical board required.
# Proves the L3 flush register is reserved only for eswin,eic7700 DTBs.
#
# Host prerequisites: qemu-system-riscv64 (or a container), fdtput (dtc),
# a built RISC-V kernel ELF, and an initramfs.
set -euo pipefail

REPO="${1:?usage: eic7700-isolation.sh <repo-root> [qemu-wrapper]}"
WRAPPER="${2:-}"  # optional container wrapper, e.g. "docker run --rm -v ... image"
LAB="$(mktemp -d)"
ELF="$REPO/target/osdk/aster-kernel-osdk-bin.qemu_elf"
INITRAMFS="${EIC7700_INITRAMFS:-$REPO/target/megrez-rx/initramfs.cpio.gz}"

command -v fdtput >/dev/null || { echo "FAIL: fdtput (device-tree-compiler) required"; exit 1; }

run_qemu() {  # $1 = default|synthetic, $2 = log
  local dtb_args=()
  if [ "$1" = "synthetic" ]; then
    qemu-system-riscv64 -machine virt -m 1G -machine dumpdtb="$LAB/base.dtb" \
      -display none -S -serial none -monitor none 2>/dev/null || true
    cp "$LAB/base.dtb" "$LAB/synthetic.dtb"
    fdtput -ts "$LAB/synthetic.dtb" / compatible "eswin,eic7700"
    dtb_args=(-machine "virt,dtb=$LAB/synthetic.dtb")
  else
    dtb_args=(-machine virt)
  fi
  timeout 25 qemu-system-riscv64 "${dtb_args[@]}" -m 1G -smp 1 -nographic \
    -kernel "$ELF" -initrd "$INITRAMFS" > "$2" 2>&1 || true
}

# Negative: standard virt DTB must NOT register the L3 flush.
run_qemu default "$LAB/negative.log"
neg_count=$(grep -c 'EIC7700 L3 cache flush registered' "$LAB/negative.log" || true)
grep -q 'Enter riscv_boot' "$LAB/negative.log" || { echo "FAIL: kernel did not boot (negative)"; exit 1; }
[ "$neg_count" = "0" ] || { echo "FAIL: EIC7700 registered on virt (negative)"; exit 1; }

# Positive: synthetic eswin,eic7700 DTB MUST register it.
run_qemu synthetic "$LAB/positive.log"
pos_count=$(grep -c 'EIC7700 L3 cache flush registered' "$LAB/positive.log" || true)
[ "$pos_count" = "1" ] || { echo "FAIL: EIC7700 not registered on synthetic DTB (positive)"; exit 1; }

echo "PASS: EIC7700 isolation verified (negative=0 registrations, positive=1 registration)"
rm -rf "$LAB"
