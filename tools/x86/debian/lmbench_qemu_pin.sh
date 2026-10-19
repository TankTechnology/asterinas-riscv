#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Wrap qemu-system-x86_64 so the LMbench run executes on a fixed set of host
# CPUs. Pass it to boot_debian.sh / `make test_x86_debian_lmbench` as the QEMU
# wrapper:
#
#   make test_x86_debian_lmbench X86_DEBIAN_LMBENCH_QEMU=tools/x86/debian/lmbench_qemu_pin.sh
#
# The benchmark subtracts two independently measured costs (`lat_sig prot`
# subtracts its own `lat_sig catch` measurement). On a hybrid or
# frequency-scaling host, letting the vCPUs migrate between P-cores, E-cores
# and idle states moves each measurement by several percent, which is larger
# than the difference being measured. Pinning does not change the guest, only
# where its vCPUs run.
#
# The CPU set is controlled by LMBENCH_QEMU_CPUS. A good choice is one thread
# of each P-core; for the i5-1340P used for the recorded results that is
# "0,2,4,6" (P-cores are 0-7, two SMT threads per core, in sibling pairs).
# The default is intentionally conservative: the first four CPUs, which is
# wrong on SMT machines that interleave siblings (use an explicit list there).
LMBENCH_QEMU_CPUS="${LMBENCH_QEMU_CPUS:-0-3}"

if ! command -v taskset >/dev/null 2>&1; then
    echo "lmbench_qemu_pin: taskset not found; running QEMU unpinned" >&2
    exec qemu-system-x86_64 "$@"
fi

exec taskset -c "$LMBENCH_QEMU_CPUS" qemu-system-x86_64 "$@"
