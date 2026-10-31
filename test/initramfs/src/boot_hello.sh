#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

# The kernel must report the interpreter of this shebang script, not the
# script itself, through `/proc/<pid>/exe` (Linux binfmt_script behavior).
exe=$(readlink /proc/$$/exe)
case "$exe" in
    */boot_hello.sh)
        echo "Unexpected /proc/self/exe: $exe" >&2
        exit 1
        ;;
esac
echo "PROC_SELF_EXE=$exe"

echo "Successfully booted."
