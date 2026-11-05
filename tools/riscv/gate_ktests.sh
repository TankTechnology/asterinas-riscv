#!/bin/bash
# SPDX-License-Identifier: MPL-2.0
# Status: active -- invoked by the Makefile's Megrez kernel-test target. See tools/riscv/LAYOUT.md.
#
# Kernel-mode tests for the components the Megrez board depends on most:
# the GMAC driver (PHY negotiation and port selection), the MMC driver (error
# decoding and recovery) and the TCP/IP glue.
#
#   tools/riscv/gate_ktests.sh              # all of them
#   tools/riscv/gate_ktests.sh aster-mmc    # just one
#
# Each crate runs in QEMU with KVM when it is available. The whole gate is a
# few minutes with a warm build, which is the point: it is cheap enough to run
# before every change that touches these drivers.

set -uo pipefail

# shellcheck source=../lib/shell.sh
_shell_self_dir=${BASH_SOURCE[0]%/*}
if [[ ${_shell_self_dir} == "${BASH_SOURCE[0]}" ]]; then _shell_self_dir=.; fi
source "${_shell_self_dir}/../lib/shell.sh"
unset _shell_self_dir
REPO_ROOT="$(repo_root)"
cd "$REPO_ROOT" || exit 2

CRATES=(
    "kernel/comps/dwmac:aster-dwmac"
    "kernel/comps/mmc:aster-mmc"
    "kernel/libs/aster-bigtcp:aster-bigtcp"
)

if [[ $# -gt 0 ]]; then
    filtered=()
    for entry in "${CRATES[@]}"; do
        for wanted in "$@"; do
            [[ "${entry##*:}" == "$wanted" ]] && filtered+=("$entry")
        done
    done
    ((${#filtered[@]})) || { echo "unknown crate: $*" >&2; exit 2; }
    CRATES=("${filtered[@]}")
fi

# The repository builds its own cargo-osdk; a cargo-osdk from another checkout
# bakes that checkout's paths into the generated base crate.
if [[ -x /tmp/osdkbin/cargo-osdk ]]; then
    export PATH=/tmp/osdkbin:$PATH
fi
export OSDK_LOCAL_DEV=1
if [[ -z "${VDSO_LIBRARY_DIR:-}" && -d "$HOME/.local/share/linux_vdso" ]]; then
    export VDSO_LIBRARY_DIR=$HOME/.local/share/linux_vdso
fi
export QEMU_ACCEL=${QEMU_ACCEL:-$( [[ -e /dev/kvm ]] && echo kvm || echo "" )}

failed=()
for entry in "${CRATES[@]}"; do
    directory=${entry%%:*}
    crate=${entry##*:}
    echo "== $crate"
    log=$(mktemp)
    (
        cd "$directory" && \
        timeout 1800 cargo osdk test \
            --target-arch riscv64 --scheme riscv \
            --boot-method qemu-direct \
            --kcmd-args=earlycon --kcmd-args=console=ttyS0
    ) >"$log" 2>&1
    status=$?
    # The results go to the serial log, because the QEMU mux sends the guest
    # console to a file rather than to stdout.
    serial="$REPO_ROOT/qemu-serial.log"
    summary=""
    if [[ -f "$serial" ]]; then
        summary=$(grep -a "test result:" "$serial" | tail -n 1)
    fi
    if [[ $status -eq 0 && "$summary" == *"0 failed"* ]]; then
        echo "   $summary"
    else
        echo "   FAILED (exit $status) $summary"
        tail -n 5 "$log" | sed 's/^/   /'
        failed+=("$crate")
    fi
    rm -f "$log"
done

if ((${#failed[@]})); then
    echo "ktest gate FAILED: ${failed[*]}" >&2
    exit 1
fi
echo "ktest gate PASSED"
