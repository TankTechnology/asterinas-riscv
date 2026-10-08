#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

# Bypass script(1)'s pseudo-terminal so focused QEMU evidence reaches the
# configured kernel console directly.
if [ -c /dev/console ]; then
    exec >/dev/console 2>&1
elif [ -c /dev/ttyS0 ]; then
    exec >/dev/ttyS0 2>&1
fi

/test/fs/procfs/schedstat
