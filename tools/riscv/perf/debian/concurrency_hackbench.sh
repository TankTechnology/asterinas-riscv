#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
exec chroot /ext2 /usr/local/libexec/asterinas-perf/concurrency_case.sh hackbench
