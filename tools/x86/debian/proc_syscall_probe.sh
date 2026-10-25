#!/bin/sh
# Verify /proc/<pid>/syscall sampling: readable, shows syscalls and running.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
log() { printf '%s\n' "$*" >/dev/console; }
log "syscall_probe_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log "self_sample=$(cat /proc/self/syscall 2>&1 | head -1)"
python3 - <<'PY'
import subprocess, time
child = subprocess.Popen([
    "python3", "-c",
    "import os,time\nend=time.time()+5\nwhile time.time()<end:\n os.getpid()\n open('/etc/hostname').read()\n"
])
time.sleep(0.4)
seen = []
unique = set()
for _ in range(100):
    try:
        line = open(f"/proc/{child.pid}/syscall").read().strip()
    except Exception as exc:
        print("syscall_read_error", exc)
        break
    seen.append(line)
    parts = line.split()
    if parts:
        unique.add(parts[0])
    time.sleep(0.02)
child.terminate()
child.wait()
print("syscall_samples", len(seen), "unique", len(unique), sorted(unique)[:12])
for line in seen[:8]:
    print("sample", line)
PY
log "syscall_probe_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
