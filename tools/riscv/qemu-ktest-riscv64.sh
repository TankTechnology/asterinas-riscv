#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# A diskless QEMU adapter for running the kernel's ktests on riscv64.
#
# `cargo osdk test --target-arch riscv64` cannot work on its own. OSDK takes the
# test QEMU arguments from `OSDK.toml`'s `[test.qemu]`, which is arch-blind:
#
#     args = "$(./tools/qemu_args.sh test)"
#
# and `qemu_args.sh test` emits an x86_64 machine -- `-machine q35`,
# `-bios /root/ovmf/release/OVMF.fd`, `-cpu Icelake-Server`, plus three disk
# images and a NIC. A RISC-V kernel launched into that dies immediately, and
# **OSDK still exits 0**, so a run that executed nothing is indistinguishable
# from a run that passed everything. The repo has been bitten by exactly this
# before: docs/porting/evidence/2026-09-12-signal-job-control-m1.md records the
# same zero-test invocation and the same remedy ("No missing disk launch,
# zero-test invocation, or timeout is counted as a pass").
#
# OSDK appends `-kernel <aster-bin> -initrd <initramfs> -append <cmdline>` to
# the config's arguments (osdk/src/bundle/mod.rs:351-359). This adapter keeps
# those three and discards everything else, substituting a machine the RISC-V
# kernel can actually boot on:
#
#   * `-machine virt`, the machine this tree's QEMU/DTS setup targets;
#   * `sv48=true`, because a test build is an **Sv48** kernel. The gate's kernel
#     is built with `FEATURES=riscv_sv39_mode` and runs on `sv48=false`, but
#     `cargo osdk test --features riscv_sv39_mode` cannot reproduce that: the
#     flag is applied to every synthesized `*-test-base` crate, and only the
#     kernel package declares the feature, so the run dies with "the package
#     'osdk-frame-allocator-osdk-bin' does not contain this feature". The test
#     kernel therefore comes out Sv48, and booting it against `sv48=false`
#     hangs after OpenSBI's banner with **no output whatsoever** -- which reads
#     as a kernel that never started rather than as a CPU mismatch. Observed
#     both ways on 2026-09-20: sv48=false hangs, sv48=true runs the suite.
#   * no disk images and no NIC, because ktests need neither and their
#     absence is what makes the stock path fail rather than merely misconfigure;
#   * `-smp 1`, because under this boot path the kernel does not bring its
#     application processors up. With `-smp 4`, `num_cpus()` reports 4 while
#     only CPU 0 is ever observed to run: instrumenting the RCU monitor shows
#     `report cpu=0` and never 1, 2 or 3, and `ap_idle_loop`'s own
#     "Idle thread for CPU #N started" never appears.
#
#     That is not cosmetic. An RCU grace period completes only when *every*
#     counted CPU has reported a quiescent state, so three phantom CPUs make
#     the set unfillable: no `RcuOption` callback ever fires and nothing is
#     ever reclaimed. The `xarray` ktests fail on exactly that
#     (`no_leakage` and `remove_shrinks_empty_nodes`, both with nothing freed)
#     under `-smp 4` and pass under `-smp 1`. Whether the absent APs are an
#     artifact of booting `-kernel` directly on QEMU `virt` -- rather than
#     through U-Boot with the gate's hand-built DTB, which does bring them up
#     -- or a real bring-up bug, is not settled. `KTEST_SMP=4` reproduces it
#     for whoever picks that up.
#
# Usage:
#     cargo osdk test --target-arch riscv64 --scheme riscv \
#         --qemu-exe tools/riscv/qemu-ktest-riscv64.sh
#
# KTEST_MEM / KTEST_SMP / KTEST_CPU override the defaults below.
set -euo pipefail

kernel=
initramfs=
append=

while [ $# -gt 0 ]; do
    case "$1" in
        -kernel)  kernel="${2:-}";    shift 2 ;;
        -initrd)  initramfs="${2:-}"; shift 2 ;;
        -append)  append="${2:-}";    shift 2 ;;
        *)        shift ;;
    esac
done

if [ -z "$kernel" ]; then
    echo "qemu-ktest-riscv64.sh: no -kernel argument was passed" >&2
    exit 2
fi

set -- \
    -machine virt \
    -cpu "${KTEST_CPU:-rv64,sv48=true,svpbmt=true,zkr=true,svadu=false,svade=true}" \
    -m "${KTEST_MEM:-2G}" \
    -smp "${KTEST_SMP:-1}" \
    -nographic \
    -nic none \
    -no-reboot \
    -kernel "$kernel"

if [ -n "$initramfs" ]; then
    set -- "$@" -initrd "$initramfs"
fi

# The test scheme carries no cmdline of its own (the Makefile's `--kcmd-args`
# go into BUILD_ARGS, not TEST_ARGS), so an empty one is expected rather than a
# sign that something is wrong -- but the runner needs a console to report on.
exec qemu-system-riscv64 "$@" -append "${append:-console=ttyS0}"
