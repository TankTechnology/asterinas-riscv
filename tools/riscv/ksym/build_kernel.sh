#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Release kernel build, plus the kernel's symbol table.
#
#   tools/riscv/ksym/build_kernel.sh [extra cargo-osdk arguments]
#
# The same `cargo osdk build` the rest of the tooling uses, followed by
# `kallsyms.py patch`: the compiler cannot build the table (it does not exist
# until the image is linked), so it is written into the reserved region of the
# produced artifacts afterwards. Deploy the Image this leaves behind; an image
# built with plain `cargo osdk build` still boots, it just has no symbols.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)

# The repository builds its own cargo-osdk; one from another checkout bakes
# that checkout's paths into the generated base crate.
if [ -x /tmp/osdkbin/cargo-osdk ]; then
    PATH=/tmp/osdkbin:$PATH
    export PATH
fi
export OSDK_LOCAL_DEV=${OSDK_LOCAL_DEV:-1}
export VDSO_LIBRARY_DIR=${VDSO_LIBRARY_DIR:-$HOME/.local/share/linux_vdso}

cd "$REPO"
cargo osdk build --release --target-arch riscv64 --scheme riscv \
    --features=riscv_sv39_mode "$@"

ELF=target/osdk/aster-kernel/aster-kernel-osdk-bin
IMAGE=target/osdk/aster-kernel/aster-kernel-osdk-bin.Image
TOP=target/osdk/aster-kernel-osdk-bin.Image

if [ ! -f "$ELF" ] || [ ! -f "$IMAGE" ]; then
    echo "build_kernel: expected $ELF and $IMAGE after the build" >&2
    exit 1
fi

if [ -f "$TOP" ]; then
    exec python3 "$HERE/kallsyms.py" patch "$ELF" "$IMAGE" "$TOP"
fi
exec python3 "$HERE/kallsyms.py" patch "$ELF" "$IMAGE"
