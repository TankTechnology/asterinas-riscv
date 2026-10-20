#!/bin/sh
# SPDX-License-Identifier: MPL-2.0

# Run the matched host QEMU package without rebuilding or copying it for each
# gate. Override ASTERINAS_QEMU_ROOT when the package is stored elsewhere.
set -eu

root=${ASTERINAS_QEMU_ROOT:-/mnt/mydisk/asterinas-qemu-gui}
if [ -n "${ASTERINAS_QEMU_BIN:-}" ]; then
    qemu=$ASTERINAS_QEMU_BIN
    lib=${ASTERINAS_QEMU_LIB_DIR:-/lib/x86_64-linux-gnu}
    module_dir=${ASTERINAS_QEMU_MODULE_DIR:-}
else
    qemu="$root/usr/bin/qemu-system-riscv64"
    lib="$root/usr/lib/x86_64-linux-gnu:$root/lib/x86_64-linux-gnu"
    module_dir="$root/usr/lib/x86_64-linux-gnu/qemu"
fi

if [ ! -x "$qemu" ]; then
    echo "qemu_virgl_env: missing executable: $qemu" >&2
    exit 127
fi

export LD_LIBRARY_PATH="$lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
if [ -n "$module_dir" ]; then
    export QEMU_MODULE_DIR="$module_dir"
else
    unset QEMU_MODULE_DIR
fi

if [ -n "${ASTERINAS_QEMU_GL_CONTAINER:-}" ]; then
    if [ -n "${QEMU_MODULE_DIR:-}" ]; then
        exec docker exec "$ASTERINAS_QEMU_GL_CONTAINER" env \
            LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
            QEMU_MODULE_DIR="$QEMU_MODULE_DIR" \
            "$qemu" "$@"
    fi
    exec docker exec "$ASTERINAS_QEMU_GL_CONTAINER" env \
        LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
        "$qemu" "$@"
fi

exec "$qemu" "$@"
