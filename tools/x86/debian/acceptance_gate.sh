#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# One-click acceptance gate for the Debian usability work.
#
# Steps:
#   daily  - the standard daily-use matrix (needs a matrix-capable root image)
#   dev    - git / C++ / pip / zlib developer workflow
#   gui    - ten concurrent GUI clients + 1.5 GiB I/O + three suspends
#   stress - ten rounds of memory + compile pressure
#
# Each step is parsed from its guest console, not from the daily gate's exit
# code, because the custom checklists intentionally do not emit the standard
# DAILY_<domain> lines. A missing matrix image turns the daily step into SKIP:
# that image is an external artifact, not part of this repository.

set -uo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
KERNEL="$REPO_ROOT/target/osdk/asterinas-osdk-bin.elf"
INITRAMFS="$REPO_ROOT/target/debian-x86/stage1/initramfs.cpio"
WORK_ROOT="$REPO_ROOT/target/debian-x86-toolchain3/rootfs/debian-root.ext2"
MATRIX_ROOT="$REPO_ROOT/target/final-matrix/xwrap/image18.ext2"
WORKSPACE="$REPO_ROOT/target/acceptance"
ACCEL=kvm
STEPS="daily,dev,gui,stress"
DRY_RUN=0
DAILY_TIMEOUT=2400
DEV_TIMEOUT=1200
GUI_TIMEOUT=1800
STRESS_TIMEOUT=900

usage() {
    cat <<'USAGE'
Usage: acceptance_gate.sh [options]

  --kernel PATH        Asterinas x86_64 kernel (default: target/osdk/asterinas-osdk-bin.elf)
  --initramfs PATH     Stage1 initramfs (default: target/debian-x86/stage1/initramfs.cpio)
  --work-root PATH     Toolchain root image for dev/gui/stress
  --matrix-root PATH   Matrix-capable root image for the daily step
  --workspace DIR      Output root (default: target/acceptance)
  --accel NAME         QEMU accelerator (default: kvm)
  --steps LIST         Comma list of daily,dev,gui,stress (default: all)
  --dry-run            Print the planned commands and exit
  -h, --help           This help
USAGE
}

while (($# > 0)); do
    case "$1" in
        --kernel) KERNEL=$2; shift 2 ;;
        --initramfs) INITRAMFS=$2; shift 2 ;;
        --work-root) WORK_ROOT=$2; shift 2 ;;
        --matrix-root) MATRIX_ROOT=$2; shift 2 ;;
        --workspace) WORKSPACE=$2; shift 2 ;;
        --accel) ACCEL=$2; shift 2 ;;
        --steps) STEPS=$2; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

for f in "$KERNEL" "$INITRAMFS" "$WORK_ROOT"; do
    [[ -f $f ]] || { echo "missing required input: $f" >&2; exit 2; }
done

IFS=, read -r -a STEP_LIST <<<"$STEPS"
for s in "${STEP_LIST[@]}"; do
    case "$s" in daily | dev | gui | stress) ;; *)
        echo "unknown step: $s" >&2
        exit 2
        ;;
    esac
done

mkdir -p "$WORKSPACE"
declare -a NAMES=() RESULTS=() DURATIONS=()
record() { NAMES+=("$1"); RESULTS+=("$2"); DURATIONS+=("$3"); }

make_step() {
    # name workspace base_root checklist timeout [extra make assignments...]
    local name=$1 ws=$2 base=$3 checklist=$4 timeout=$5
    shift 5
    mkdir -p "$ws"
    local t0 t1 rc
    t0=$(date +%s)
    (
        cd "$REPO_ROOT" || exit 1
        env ${checklist:+DAILY_USE_CHECKLIST="$checklist"} "$@" \
            make test_x86_daily_use \
            X86_DEBIAN_KERNEL="$KERNEL" \
            X86_DEBIAN_STAGE1_INITRAMFS="$INITRAMFS" \
            X86_DEBIAN_XORG_BASE_ROOT="$base" \
            X86_DEBIAN_XORG_ACCEL="$ACCEL" \
            X86_DEBIAN_XORG_TIMEOUT="$timeout" \
            DAILY_USE_WORKSPACE="$ws" >"$ws/host.log" 2>&1
    )
    rc=$?
    t1=$(date +%s)
    STEP_RC=$rc
    STEP_DUR=$((t1 - t0))
    STEP_CONSOLE="$ws/console.txt"
}

require_markers() {
    local file=$1
    shift
    local missing=0
    for marker in "$@"; do
        if ! grep -aqE -- "$marker" "$file"; then
            echo "  missing marker: $marker"
            missing=1
        fi
    done
    return "$missing"
}

no_panic() {
    local file=$1
    if grep -aq "ERROR : Uncaught panic" "$file"; then
        echo "  found an Uncaught panic in $file"
        return 1
    fi
    return 0
}

class_count() {
    local cls=$1 file=$2
    grep -a "gui_window_class class=$cls count=" "$file" | tail -1 |
        sed -E 's/.*count=([0-9]+).*/\1/'
}

step_daily() {
    local ws="$WORKSPACE/daily"
    if [[ ! -f "$MATRIX_ROOT" ]]; then
        echo "daily: SKIP (matrix image not found: $MATRIX_ROOT)"
        record daily SKIP 0
        return 0
    fi
    echo "daily: matrix over $MATRIX_ROOT"
    make_step daily "$ws" "$MATRIX_ROOT" "" "$DAILY_TIMEOUT"
    if ((STEP_RC == 0)); then
        record daily PASS "$STEP_DUR"
    else
        echo "  make test_x86_daily_use rc=$STEP_RC (see $ws/host.log)"
        record daily FAIL "$STEP_DUR"
    fi
}

step_dev() {
    local ws="$WORKSPACE/dev"
    echo "dev: git/C++/pip/zlib over $WORK_ROOT"
    make_step dev "$ws" "$WORK_ROOT" \
        "$REPO_ROOT/tools/x86/debian/dev_workflow.sh" "$DEV_TIMEOUT"
    if [[ ! -f "$STEP_CONSOLE" ]] ||
        ! require_markers "$STEP_CONSOLE" \
            'git_clone rc=0' \
            'cpp gpp_rc=0 run_out=1000' \
            'pip_six src=.* rc=0 import_rc=0' \
            'zlib .*result=yes'; then
        record dev FAIL "$STEP_DUR"
    else
        record dev PASS "$STEP_DUR"
    fi
}

step_gui() {
    local ws="$WORKSPACE/gui"
    echo "gui: ten clients + 1.5 GiB I/O + three suspends over $WORK_ROOT"
    make_step gui "$ws" "$WORK_ROOT" \
        "$REPO_ROOT/tools/x86/debian/gui_stress.sh" "$GUI_TIMEOUT"
    local ok=0
    if [[ ! -f "$STEP_CONSOLE" ]]; then
        ok=1
    else
        require_markers "$STEP_CONSOLE" \
            'DAILY_DESKTOP=ok Xorg\+DRI3' \
            'dd_rc=0 sha_rc=0' \
            'suspend_ok=3/3' \
            'gui_suspend round=1 .*result=ok' \
            'gui_suspend round=2 .*result=ok' \
            'gui_suspend round=3 .*result=ok' || ok=1
        no_panic "$STEP_CONSOLE" || ok=1
        for cls in pcmanfm eog xterm xclock xcalc xeyes; do
            count=$(class_count "$cls" "$STEP_CONSOLE")
            if [[ -z $count || $count -lt 1 ]]; then
                echo "  class $cls has no window"
                ok=1
            fi
        done
    fi
    if ((ok == 0)); then
        record gui PASS "$STEP_DUR"
    else
        record gui FAIL "$STEP_DUR"
    fi
}

step_stress() {
    local ws="$WORKSPACE/stress"
    echo "stress: 10 rounds of memory + compile pressure over $WORK_ROOT"
    make_step stress "$ws" "$WORK_ROOT" \
        "$REPO_ROOT/tools/x86/debian/stress_memory.sh" "$STRESS_TIMEOUT" \
        STRESS_ROUNDS=10
    local ok=0
    if [[ ! -f "$STEP_CONSOLE" ]]; then
        ok=1
    else
        require_markers "$STEP_CONSOLE" \
            'compiler_test rc=0 out=ok' \
            'stress_failures python3=0/10 gcc=0/10 gcc_ok=10/10' || ok=1
        no_panic "$STEP_CONSOLE" || ok=1
        echo "  note: dmesg is ENOSYS on the microvm kernel; the console is authoritative"
    fi
    if ((ok == 0)); then
        record stress PASS "$STEP_DUR"
    else
        record stress FAIL "$STEP_DUR"
    fi
}

echo "acceptance gate"
echo "  kernel   : $KERNEL"
echo "  initramfs: $INITRAMFS"
echo "  work-root: $WORK_ROOT"
echo "  matrix   : $MATRIX_ROOT"
echo "  workspace: $WORKSPACE"
echo "  steps    : $STEPS"
echo

if ((DRY_RUN)); then
    for s in "${STEP_LIST[@]}"; do
        case "$s" in
            daily) echo "would run: daily over $MATRIX_ROOT" ;;
            dev) echo "would run: dev over $WORK_ROOT with dev_workflow.sh" ;;
            gui) echo "would run: gui over $WORK_ROOT with gui_stress.sh" ;;
            stress) echo "would run: stress over $WORK_ROOT with stress_memory.sh STRESS_ROUNDS=10" ;;
        esac
    done
    exit 0
fi

for s in "${STEP_LIST[@]}"; do
    "step_$s"
    echo
done

echo "=== acceptance summary ==="
printf '%-8s %-6s %8s\n' STEP RESULT SECONDS
fail=0
for i in "${!NAMES[@]}"; do
    printf '%-8s %-6s %8s\n' "${NAMES[$i]}" "${RESULTS[$i]}" "${DURATIONS[$i]}"
    [[ ${RESULTS[$i]} == FAIL ]] && fail=1
done
echo
echo "console/host logs under $WORKSPACE/<step>/"
exit "$fail"
