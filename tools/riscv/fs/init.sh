#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
# Minimal init for the crash-consistency guest. The kernel mounts devtmpfs at
# /dev itself; proc/sysfs are not needed by the writer and mounting them here
# only adds a fork/exec path that complicates the crash timing.
mkdir -p /mnt
echo "CRASH_INIT starting"
# The gate drops a mode name here; no file means the record-stream writer.
MODE=""
if [ -f /mode ]; then
    MODE=$(cat /mode)
fi
exec /writer $MODE
