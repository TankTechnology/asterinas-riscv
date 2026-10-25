#!/bin/sh
# Verify /proc/self/stat minflt/majflt now track real page faults.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
log() { printf '%s\n' "$*" >/dev/console; }
log "fault_probe_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
python3 - <<'PY'
def stats():
    data = open('/proc/self/stat').read()
    rest = data[data.rindex(')') + 2:].split()
    return int(rest[7]), int(rest[9])  # minflt, majflt

before = stats()
n = 64 * 1024 * 1024
buf = bytearray(n)
for i in range(0, n, 4096):
    buf[i] = 1
mid = stats()
for i in range(0, n, 4096):
    buf[i] = 2
after = stats()
print("fault_before minflt=%d majflt=%d" % before)
print("fault_after_touch64m minflt=%d majflt=%d" % mid)
print("fault_after_touch_again minflt=%d majflt=%d" % after)
PY
log "fault_probe_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
