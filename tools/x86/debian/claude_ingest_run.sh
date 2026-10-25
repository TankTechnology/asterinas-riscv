#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Guest half: verify the injected Claude Code, then let it choose and execute
# two real tasks in /root/work. Key lines go to /dev/console; full round text
# stays in /root/work/roundN.log.

set -u
export PATH=/opt/claude-code/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root

printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true

LOG=/tmp/claude-in-guest.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now() { cut -d' ' -f1 /proc/uptime; }

CLAUDE=/opt/claude-code/bin/claude
log "claude_guest start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [ ! -x "$CLAUDE" ]; then
    log "claude=missing path=$CLAUDE"
    echo DAILY_DONE=ok >/dev/console
    exit 0
fi
if [ ! -f /root/.claude/settings.json ]; then
    log "claude_settings=missing /root/.claude/settings.json"
fi

ver=$(timeout 60 "$CLAUDE" --version 2>&1)
vrc=$?
log "claude_version rc=$vrc out=$(printf '%s' "$ver" | head -2 | tr '\n' ' ' | head -c 160)"

t0=$(now)
ok_out=$(timeout 180 "$CLAUDE" -p "reply with exactly: OK" 2>&1)
orc=$?
odur=$(awk -v a="$t0" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}')
log "claude_ok rc=$orc dur=${odur}s out=$(printf '%s' "$ok_out" | tail -4 | tr '\n' ' ' | head -c 240)"

rounds=0
# Claude Code refuses --dangerously-skip-permissions as root. Run the agent
# as the guest's normal developer account instead, with its own settings and
# work directory, and leave the root copy untouched.
WORK=/home/asterinas/work
prepare_agent_home() {
    mkdir -p /home/asterinas/.claude "$WORK"
    if [ -f /root/.claude/settings.json ]; then
        cp /root/.claude/settings.json /home/asterinas/.claude/settings.json
        chown 1000:1000 /home/asterinas/.claude /home/asterinas/.claude/settings.json "$WORK"
        chmod 0700 /home/asterinas/.claude
        chmod 0600 /home/asterinas/.claude/settings.json
    fi
    chown 1000:1000 "$WORK" 2>/dev/null || true
}
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
            >"$WORK/round$n.log" 2>&1
    )
    rc=$?
    rdur=$(awk -v a="$rt0" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}')
    bytes=$(wc -c <"$WORK/round$n.log" 2>/dev/null || echo 0)
    log "claude_round$n rc=$rc dur=${rdur}s bytes=$bytes"
    log "claude_round${n}_head=$(head -3 "$WORK/round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 220)"
    log "claude_round${n}_tail=$(tail -5 "$WORK/round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 320)"
    rounds=$((rounds + 1))
}

if [ "$orc" -eq 0 ] && printf '%s' "$ok_out" | grep -q 'OK'; then
    prepare_agent_home
    cd "$WORK" || exit 1
    run_round 1 "你在一个 Debian 环境里（内核是非 Linux 的 Asterinas，但提供 Linux ABI）。请自己选一件对这个工作目录真正有用的事，动手做完：可以是写一个能用的命令行小工具并自测、整理/生成一份数据文件、写并跑一个脚本。最后用 10 行内报告：你做了什么、怎么验证结果、遇到什么障碍。不要只写计划，要真的执行。" 600
    run_round 2 "在上一步产物基础上再加一个功能和对应测试，仍然动手执行；最后 10 行内报告改了什么、怎么验证、有无障碍。" 600
else
    log "claude_rounds=skipped phase1_rc=$orc"
fi

log "claude_rounds_completed=$rounds"
if [ -d "$WORK" ]; then
    log "claude_work_files=$(ls -la "$WORK" 2>/dev/null | tail -n +2 | tr '\n' ';' | head -c 400)"
fi

{
    echo "=== claude-in-guest summary ==="
    echo "version_rc=$vrc ok_rc=$orc rounds=$rounds"
    echo "work_logs=/home/asterinas/work/round1.log,/home/asterinas/work/round2.log"
    echo "=== log ==="
    cat "$LOG"
} >/dev/console 2>&1
echo DAILY_DONE=ok >/dev/console
