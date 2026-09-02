#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Runs the curated IGT GPU Tools subset against the virtio-gpu DRM device
# and reports structured markers on the console for the IGT gate.  This runs
# instead of the desktop session (the desktop services are disabled by the
# asterinas.igt=1 kernel command-line parameter), so KMS tests can become
# the DRM master.

set -uo pipefail

readonly CONSOLE="${ASTERINAS_DRM_IGT_CONSOLE:-/dev/console}"
readonly IGT_BIN="${ASTERINAS_DRM_IGT_BIN:-/opt/igt/bin/igt-gpu-tools}"
readonly PER_TEST_TIMEOUT="${ASTERINAS_DRM_IGT_TEST_TIMEOUT:-300}"

# Tests are grouped by the driver's claimed feature set
# (tools/riscv/drm/VALIDATION.md): core UAPI, read/events, dumb buffers, GEM,
# syncobj (binary/timeline/eventfd), PRIME/dma-buf, and KMS (legacy + atomic
# modeset, flips, cursor) on a single CRTC.
# Excluded: core_auth, drm_read, kms_rmfb, kms_feature_discovery,
# kms_universal_plane, kms_sequence, kms_flip_event_leak — these require
# debugfs which the kernel does not implement.
readonly TESTS=(
    core_getversion core_getclient core_setmaster core_setmaster_vs_auth
    drm_virtgpu dumb_buffer gem_basic dmabuf dmabuf_sync_file
    syncobj_basic syncobj_wait syncobj_timeline syncobj_eventfd
    prime_self_import prime_busy prime_mmap prime_mmap_coherency
    kms_addfb_basic kms_getfb kms_prop_blob kms_properties
    kms_invalid_mode kms_force_connector_basic kms_setmode
    kms_flip kms_cursor_legacy kms_atomic kms_vblank
)

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }

# IGT's bundled libraries are pulled in transitively through libigt.so.0,
# whose own RUNPATH does not cover /opt/igt/lib (DT_RUNPATH does not apply
# transitively), so set the search path explicitly.
export LD_LIBRARY_PATH="/opt/igt/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# IGT's DRIVER_ANY deliberately excludes DRIVER_VIRTIO (upstream assumes a
# virtio-gpu is never the primary device).  Force the driver so tests run
# against the only GPU this guest has.
export IGT_FORCE_DRIVER="virtio_gpu"

for _ in $(seq 1 60); do
    [[ -c /dev/dri/card0 && -c /dev/dri/renderD128 ]] && break
    sleep 1
done
if [[ ! -c /dev/dri/card0 ]]; then
    emit "ASTERINAS_IGT_FAIL reason=no-drm-device"
    exit 1
fi

pass=0
skip=0
fail=0
emit "ASTERINAS_IGT_BEGIN tests=${#TESTS[@]}"
ls -la /dev/dri >>"$CONSOLE" 2>&1 || true
for test_name in "${TESTS[@]}"; do
    output="$(timeout "$PER_TEST_TIMEOUT" "$IGT_BIN/$test_name" 2>&1)"
    rc=$?
    if (( rc == 0 )); then
        status=PASS
        pass=$((pass + 1))
    elif (( rc == 77 )); then
        status=SKIP
        skip=$((skip + 1))
    else
        status=FAIL
        fail=$((fail + 1))
    fi
    emit "ASTERINAS_IGT_RESULT test=$test_name rc=$rc status=$status"
    if [[ "$status" == FAIL ]]; then
        printf '%s\n' "$output" | tail -c 2048 >>"$CONSOLE" || true
    elif [[ "$status" == SKIP ]]; then
        printf '%s\n' "$output" | tail -c 400 >>"$CONSOLE" || true
    fi
done
emit "ASTERINAS_IGT_DONE pass=$pass skip=$skip fail=$fail"
