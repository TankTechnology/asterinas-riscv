#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
# Verify that tmpfs roots are 1777 and that a normal user can use POSIX shm
# and Python multiprocessing locks/pools.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log() { echo "$*" >/dev/console; }

log "tmpfs_check start"
log "tmpfs_mode=$(stat -c '%a %U %G' /dev/shm 2>&1)"
log "tmpfs_mount=$(mount 2>/dev/null | grep ' /dev/shm ' | head -1)"

setpriv --reuid=1000 --regid=1000 --init-groups sh -c 'touch /dev/shm/aste-probe && rm /dev/shm/aste-probe' 2>/tmp/shm-write.err
wrc=$?
log "shm_write_uid1000 rc=$wrc err=$(head -1 /tmp/shm-write.err 2>/dev/null)"

mp_out=$(setpriv --reuid=1000 --regid=1000 --init-groups \
    env HOME=/home/asterinas PATH="$PATH" python3 -c '
import multiprocessing as mp
ctx = mp.get_context("fork")
lock = ctx.Lock()
with ctx.Pool(2) as pool:
    result = pool.map(abs, [-1, -2, -3])
print("pool", result)
print("lock_ok", lock is not None)
' 2>&1)
mprc=$?
log "mp_test rc=$mprc out=$(printf '%s' "$mp_out" | tr '\n' ' ' | head -c 240)"

echo DAILY_DONE=ok >/dev/console
