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
#   * `-smp 1`, because the `xarray` tests `no_leakage` and
#     `remove_shrinks_empty_nodes` fail under `-smp 4` and pass under `-smp 1`
#     on the same image, and **why is not known**. What is known:
#
#       - the APs do come up. With `loglevel=debug` the boot prints
#         "Starting hart 0/2/3" each followed by "Successfully started hart
#         N", then "Processor 1/2/3 started. Spinning for tasks." Four CPUs run;
#         they are not absent.
#       - but the APs never leave the boot context. `ostd/src/boot/smp.rs`
#         parks each one on `AP_LATE_ENTRY.wait()`, and the only thing that
#         releases it is `register_ap_entry(ap_init)` in the kernel's `main()`
#         (kernel/src/init.rs:32).
#
#         **A ktest build never runs that `main()`.** `#[ostd::main]`
#         (ostd/libs/ostd-macros/src/lib.rs:76-94) emits its `__ostd_main`
#         entry point under `#[cfg(not(ktest))]`, so under `--cfg ktest` the
#         kernel's entry is compiled out entirely and the entry comes from
#         `osdk-test-kernel`'s `#[ostd::ktest::main]`
#         (osdk/deps/test-kernel/src/lib.rs:34) instead. The kernel's `main()`
#         is still compiled; nothing calls it.
#
#         The signature is exact and cheap to re-check: the first line of that
#         `main()`, "OSTD initialized. Preparing components.", appears once in
#         the gate kernel's serial log and **zero** times in the ktest
#         kernel's.
#       - an AP parked there runs no idle thread, never idles, and so never
#         reports a quiescent state: instrumenting the RCU monitor to print
#         each reporting CPU gives `report cpu=0` and never 1, 2 or 3.
#       - an RCU grace period completes only when every counted CPU has
#         reported, so the set stays unfilled, `after_grace_period` callbacks
#         are never invoked, and nothing an `RcuOption` held is ever freed.
#         That is what the two tests observe.
#
#     So this is a property of the test harness rather than a kernel defect:
#     under ktests the APs are permanently parked, and every run is effectively
#     single-CPU. `-smp 1` matches the machine the suite actually gets. A run
#     that wants SMP coverage needs `#[test_main]` to perform the kernel's boot
#     far enough to register the AP entry.
#
#     The same fact has a wider consequence, worth stating because it bounds
#     what a green suite means: `component::init_all` is called only from that
#     `main()` and the tasks it spawns (kernel/src/init.rs:21,155,173), so
#     **the ktests run against an uninitialized kernel** -- no components
#     registered, no devices, no filesystems. Most ktests are self-contained
#     and that is harmless, but it is exactly why a whole-machine property like
#     the AP rendezvous stayed broken without any test noticing.
#
#     Two attempted fixes -- reporting from `halt_cpu` and from `reschedule`
#     -- did not change the outcome and were reverted. `KTEST_SMP=4`
#     reproduces. This is a lead for whoever picks it up, not a conclusion.
#
#     (An earlier version of this comment claimed the APs never start. That
#     was wrong, and wrong because of a `head -20` that cut the boot log off
#     after hart 0 -- the same truncation that this tree's evidence script
#     warns about in its own comments.)
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
