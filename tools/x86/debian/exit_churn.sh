#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Task-exit churn regression for the idempotent exit fix.
# Repeats the scenario that panicked once (xclock exiting immediately with no
# X server) and adds process/thread churn: many short-lived tasks exiting
# concurrently.
#
# Markers: churn_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LOG=/tmp/exit-churn.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }

log "churn_start $(date -u '+%Y-%m-%dT%H:%M:%SZ') nproc=$(nproc)"

# 1) The original trigger: a GUI client that exits immediately (no X server).
if command -v xclock >/dev/null 2>&1; then
    i=0
    while [ "$i" -lt 100 ]; do
        DISPLAY=:99 timeout 2 xclock >/dev/null 2>&1
        i=$((i + 1))
    done
    log "churn_xclock exits=100"
else
    log "churn_xclock missing"
fi

# 2) fork/exec churn: 3000 short-lived processes.
python3 - <<'PY'
import subprocess
for _ in range(3000):
    subprocess.run(["true"], check=False)
print("churn_python exits=3000", flush=True)
PY
rc=$?
log "churn_fork_exec exits=3000 rc=$rc"

# 3) Concurrent short-lived threads in one process (thread exit churn).
python3 - <<'PY'
import threading

def body():
    pass

for _ in range(10):
    ts = [threading.Thread(target=body) for _ in range(200)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("churn_threads threads=2000", flush=True)
PY
log "churn_threads rc=$?"

# 4) Re-run the GUI trigger after the churn.
if command -v xclock >/dev/null 2>&1; then
    i=0
    while [ "$i" -lt 100 ]; do
        DISPLAY=:99 timeout 2 xclock >/dev/null 2>&1
        i=$((i + 1))
    done
    log "churn_xclock2 exits=100"
fi

log "churn_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
