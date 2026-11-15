#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Release kernel build, plus the kernel's symbol table.
#
#   tools/riscv/ksym/build_kernel.sh [--function-trace] [extra cargo-osdk arguments]
#
# `--function-trace` adds `-Z instrument-mcount`, which puts a `call mcount` in
# every function prologue so `/proc/trace/events/ftrace/function/enable` works
# (see kernel/src/trace/function.rs). It instruments every crate, so the kernel
# grows and a hot tracing window is expensive -- build it only when needed.
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

FUNCTION_TRACE=0
ARGS=
for arg in "$@"; do
    case "$arg" in
        --function-trace) FUNCTION_TRACE=1 ;;
        *) ARGS="$ARGS $arg" ;;
    esac
done

cd "$REPO"
if [ "$FUNCTION_TRACE" = 1 ]; then
    RUSTFLAGS="${RUSTFLAGS:-} -Z instrument-mcount"
    export RUSTFLAGS
fi

# shellcheck disable=SC2086
cargo osdk build --release --target-arch riscv64 --scheme riscv \
    --features=riscv_sv39_mode $ARGS

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
