#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Three progressive Claude Code rounds in the guest, all as the normal
# asterinas user: (1) pytest ecosystem, (2) a real C project built with
# make -j4, (3) a multi-process/multi-thread/file-lock stress tool.
# Per-round logs and load/memory samples go to the console and /root tmp.

set -u
export PATH=/opt/claude-code/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root

printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true

LOG=/tmp/claude-r3.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now() { cut -d' ' -f1 /proc/uptime; }
memavail() { awk '/^MemAvailable:/{print $2}' /proc/meminfo; }
loadavg() { cut -d' ' -f1 /proc/loadavg; }

CLAUDE=/opt/claude-code/bin/claude
WORK=/home/asterinas/work

log "claude_r3 start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# Agent toolchain: git and pip for the guest-side work.
timeout 240 apt-get update >/tmp/r3-apt-update.log 2>&1
apt_update_rc=$?
timeout 300 apt-get install -y git python3-pip >/tmp/r3-apt.log 2>&1
apt_rc=$?
log "r3_apt update_rc=$apt_update_rc install_rc=$apt_rc"

mkdir -p /home/asterinas/.claude "$WORK"
if [ -f /root/.claude/settings.json ]; then
    cp /root/.claude/settings.json /home/asterinas/.claude/settings.json
    chown 1000:1000 /home/asterinas/.claude /home/asterinas/.claude/settings.json "$WORK"
    chmod 0700 /home/asterinas/.claude
    chmod 0600 /home/asterinas/.claude/settings.json
fi

ver=$(timeout 60 "$CLAUDE" --version 2>&1)
vrc=$?
log "claude_version rc=$vrc out=$(printf '%s' "$ver" | head -2 | tr '\n' ' ' | head -c 160)"

ok_out=$(timeout 180 "$CLAUDE" -p "reply with exactly: OK" 2>&1)
orc=$?
log "claude_ok rc=$orc out=$(printf '%s' "$ok_out" | tail -3 | tr '\n' ' ' | head -c 200)"

rounds=0
run_round() {
    n=$1
    prompt=$2
    tmo=$3
    rt0=$(now)
    (
        cd "$WORK" || exit 1
        timeout "$tmo" setpriv --reuid=1000 --regid=1000 --init-groups \
            env HOME=/home/asterinas PATH="$PATH" "$CLAUDE" -p "$prompt" \
            --dangerously-skip-permissions --output-format text \
            >"$WORK/claude-r3-round$n.log" 2>&1
    ) &
    pid=$!
    max_load=0
    min_mem=999999999
    while kill -0 "$pid" 2>/dev/null; do
        la=$(loadavg)
        ma=$(memavail)
        max_load=$(awk -v a="$max_load" -v b="$la" 'BEGIN{print (b>a?b:a)}')
        min_mem=$(awk -v a="$min_mem" -v b="$ma" 'BEGIN{print (b<a?b:a)}')
        sleep 10
    done
    wait "$pid"
    rc=$?
    rdur=$(awk -v a="$rt0" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}')
    bytes=$(wc -c <"$WORK/claude-r3-round$n.log" 2>/dev/null || echo 0)
    log "r3_round$n rc=$rc dur=${rdur}s bytes=$bytes max_load=$max_load min_memavail_kb=$min_mem"
    log "r3_round${n}_head=$(head -3 "$WORK/claude-r3-round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 240)"
    log "r3_round${n}_tail=$(tail -8 "$WORK/claude-r3-round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 420)"
    rounds=$((rounds + 1))
}

if [ "$orc" -eq 0 ]; then
    run_round 1 "在本机装好并用起来一个真实的 Python 项目：先用 python3 -m pip install --no-cache-dir --break-system-packages pytest 安装 pytest，然后在 /home/asterinas/work/pyproj 建一个含 20 个以上测试的小项目（例如一个字符串/数学工具库），运行 pytest -q 并确保全绿。最后 10 行内报告：做了什么、关键命令、测试结果（pass/fail 数）、遇到的第一条真实错误与你的处理。" 900
    run_round 2 "克隆并构建一个中等规模的真实 C 项目（建议 lua 5.4 或 jq），用 make -j4 并行构建，至少运行一次项目自带测试；工作目录 /home/asterinas/work/cproj。最后 10 行内报告：项目/版本、构建结果、测试结果、遇到的第一条真实错误与处理。" 900
    run_round 3 "写并运行一个多进程 + 多线程 + 文件锁的压力小工具（例如并行哈希 /usr 下的文件，或用 xargs -P8），要求结果正确并重复运行验证；工作目录 /home/asterinas/work/stressproj。最后 10 行内报告：实现方式、正确性验证、CPU/内存峰值与耗时、遇到的第一条真实错误与处理。" 900
else
    log "claude_r3_rounds=skipped phase1_rc=$orc"
fi

log "claude_r3_rounds_completed=$rounds"
if [ -d "$WORK" ]; then
    log "claude_r3_work_files=$(ls -la "$WORK" 2>/dev/null | tail -n +2 | tr '\n' ';' | head -c 500)"
fi

{
    echo "=== claude-r3 summary ==="
    echo "version_rc=$vrc ok_rc=$orc apt_update_rc=$apt_update_rc apt_rc=$apt_rc rounds=$rounds"
    echo "work_logs=$WORK/claude-r3-round{1,2,3}.log"
    echo "=== log ==="
    cat "$LOG"
} >/dev/console 2>&1
echo DAILY_DONE=ok >/dev/console
