#!/bin/bash

# SPDX-License-Identifier: MPL-2.0

# Run the kernel test suite (`make ktest`) and print an aggregate result.
#
# The host toolchain may be too old for the prebuilt `cargo-osdk` binary, and
# the test initramfs is a Nix store path that only exists inside the
# development container.  When the host cannot run the suite, re-exec the same
# command inside the development container.  Set ASTERINAS_DEV_IMAGE to
# override the container image.

set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$REPO_ROOT"

can_run_on_host() {
    command -v cargo-osdk >/dev/null 2>&1 || return 1
    cargo osdk --version >/dev/null 2>&1 || return 1
    return 0
}

if [ "${ASTERINAS_KTEST_IN_CONTAINER:-0}" != "1" ] && ! can_run_on_host; then
    if [ -n "${ASTERINAS_DEV_IMAGE:-}" ]; then
        image=$ASTERINAS_DEV_IMAGE
    elif docker image inspect asterinas-env:with-cargo-cache >/dev/null 2>&1; then
        image=asterinas-env:with-cargo-cache
    else
        image=asterinas/asterinas:0.18.0-20260702
    fi

    offline_env=()
    if [ "${KTEST_OFFLINE:-0}" = "1" ]; then
        offline_env=(CARGO_NET_OFFLINE=true)
    fi

    exec "$REPO_ROOT/tools/docker/run_dev_container.sh" \
        --image "$image" \
        -- env ASTERINAS_KTEST_IN_CONTAINER=1 "${offline_env[@]}" \
            KTEST_CRATE_DIR="${KTEST_CRATE_DIR:-}" \
            KTEST_FILTER="${KTEST_FILTER:-}" \
            bash -lc 'cd /root/asterinas && exec tools/ktest.sh'
fi

log="$REPO_ROOT/target/ktest.log"
mkdir -p "$REPO_ROOT/target"

start=$(date +%s)
set +e
make ktest-local \
    KTEST_CRATE_DIR="${KTEST_CRATE_DIR:-}" \
    KTEST_FILTER="${KTEST_FILTER:-}" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
set -e
elapsed=$(( $(date +%s) - start ))

clean_log="$log.clean"
sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$log" > "$clean_log"
read -r passed failed filtered < <(
    awk '/test result:/ {
        for (i = 1; i <= NF; i++) {
            if ($i == "passed;") p += $(i - 1)
            if ($i == "failed;") f += $(i - 1)
            if ($i == "filtered") fl += $(i - 1)
        }
    } END { printf "%d %d %d\n", p, f, fl }' "$clean_log"
)
crates=$(grep -ac 'ktest runner] running' "$clean_log" || true)

echo
echo "KTEST_RESULT passed=$passed failed=$failed filtered=$filtered crates=$crates elapsed=${elapsed}s"
echo "KTEST_LOG target/ktest.log"
if [ "$rc" -ne 0 ]; then
    echo "KTEST_FAILED exit=$rc"
fi
exit "$rc"
