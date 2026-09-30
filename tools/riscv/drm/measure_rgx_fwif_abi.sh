#!/bin/sh
# SPDX-License-Identifier: MPL-2.0

# Run inside the Asterinas development container with a pinned Volcanic tree.
set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: $0 VENDOR_ROOT CONFIG_KERNEL_H OUTPUT_DIR" >&2
    exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
vendor=$1
config=$2
output=$3
mkdir -p "$output"

compile_probe() {
    compiler=$1
    output_object=$2
    shift 2
    "$compiler" -std=gnu11 -O0 -c "$script_dir/rgx_fwif_abi_probe.c" \
        -o "$output_object" -include "$config" "$@" \
        -I"$vendor/include" -I"$vendor/include/volcanic" \
        -I"$vendor/include/public" -I"$vendor/hwdefs/volcanic" \
        -I"$vendor/hwdefs/volcanic/km"
}

compile_probe riscv64-linux-gnu-gcc "$output/riscv64.o" \
    -I"$script_dir/rgx_fwif_abi_stubs"
compile_probe gcc "$output/native.o"
riscv64-linux-gnu-objcopy -O binary --only-section=.rgx_fwif_abi \
    "$output/riscv64.o" "$output/riscv64.bin"
objcopy -O binary --only-section=.rgx_fwif_abi \
    "$output/native.o" "$output/native.bin"
cmp "$output/riscv64.bin" "$output/native.bin"
python3 "$script_dir/decode_rgx_fwif_abi.py" "$output/riscv64.bin" \
    > "$output/layout.json"
echo "PowerVR FWIF ABI probe: RISC-V and native sections match"
