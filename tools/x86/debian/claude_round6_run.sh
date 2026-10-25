#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Round-6 Claude Code rounds: Rust toolchain.
#   1. install Rust (apt rustc/cargo preferred, rustup as fallback), then
#      `cargo new hello && cargo test`.
#   2. write a small crate using std::thread + HashMap with unit tests and
#      run `cargo test`.
#
# The image has limited free space, so rustup is only a fallback and apt
# caches/old work trees are removed first.
#
# Markers: r6_* and DAILY_DONE=ok.

set -u
export PATH=/opt/claude-code/bin:/home/asterinas/.cargo/bin:/home/asterinas/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root

printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true

LOG=/tmp/claude-r6.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now() { cut -d' ' -f1 /proc/uptime; }
memavail() { awk '/^MemAvailable:/{print $2}' /proc/meminfo; }
loadavg() { cut -d' ' -f1 /proc/loadavg; }

CLAUDE=/opt/claude-code/bin/claude
WORK=/home/asterinas/work6

log "claude_r6 start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# Free space: old round work trees and apt archives are not needed.
rm -rf /home/asterinas/work /home/asterinas/work4 /home/asterinas/work5
apt-get clean >/dev/null 2>&1 || true

timeout 240 apt-get update >/tmp/r6-apt-update.log 2>&1
apt_update_rc=$?
timeout 420 apt-get install -y git build-essential curl ca-certificates rustc cargo >/tmp/r6-apt.log 2>&1
apt_rc=$?
log "r6_apt update_rc=$apt_update_rc install_rc=$apt_rc rustc=$(rustc --version 2>&1 | head -1) cargo=$(cargo --version 2>&1 | head -1)"

mkdir -p /home/asterinas/.claude "$WORK"
if [ -f /root/.claude/settings.json ]; then
    cp /root/.claude/settings.json /home/asterinas/.claude/settings.json
    chmod 0700 /home/asterinas/.claude
    chmod 0600 /home/asterinas/.claude/settings.json
fi
chown -R 1000:1000 /home/asterinas/.claude "$WORK" 2>/dev/null || true

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
            >"$WORK/claude-r6-round$n.log" 2>&1
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
    bytes=$(wc -c <"$WORK/claude-r6-round$n.log" 2>/dev/null || echo 0)
    log "r6_round$n rc=$rc dur=${rdur}s bytes=$bytes max_load=$max_load min_memavail_kb=$min_mem"
    log "r6_round${n}_head=$(head -4 "$WORK/claude-r6-round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 320)"
    log "r6_round${n}_tail=$(tail -12 "$WORK/claude-r6-round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 700)"
    rounds=$((rounds + 1))
}

if [ "$orc" -eq 0 ]; then
    run_round 1 "在本机把 Rust 工具链用起来：优先用 apt 已装好的 rustc/cargo（如果不可用再试 rustup，但镜像空间有限，先清缓存）；然后在 /home/asterinas/work6 执行 cargo new hello && cd hello && cargo test，确保测试通过。要求真的执行命令而不是只写计划；遇到环境问题先自己解决，解决不了如实写阻塞点。最后 10 行内报告：Rust 安装方式与版本、cargo new/test 结果、第一条真实错误与处理。" 900
    run_round 2 "在 /home/asterinas/work6/rustproj 写一个小 Rust crate（cargo new --lib）：实现一个使用 std::thread 与 std::collections::HashMap 的小功能（例如多线程统计词频或分片求和），带 5 个以上单元测试，运行 cargo test 并确保全绿。要求真的执行命令；遇到环境问题先自己解决，解决不了如实写阻塞点。最后 10 行内报告：功能、测试结果（passed/failed）、第一条真实错误与处理。" 900
else
    log "claude_r6_rounds=skipped phase1_rc=$orc"
fi

log "claude_r6_rounds_completed=$rounds"
if [ -d "$WORK" ]; then
    log "claude_r6_work_files=$(ls -la "$WORK" 2>/dev/null | tail -n +2 | tr '\n' ';' | head -c 600)"
fi
# Host-side re-run of cargo test in the crate the agent wrote.
if [ -d "$WORK/rustproj" ]; then
    out=$(setpriv --reuid=1000 --regid=1000 --init-groups env HOME=/home/asterinas \
        sh -c "cd '$WORK/rustproj' && cargo test 2>&1" 2>&1)
    rc=$?
    log "r6_cargo_test rc=$rc tail=$(printf '%s' "$out" | tail -8 | tr '\n' ' ' | head -c 600)"
fi

{
    echo "=== claude-r6 summary ==="
    echo "version_rc=$vrc ok_rc=$orc apt_update_rc=$apt_update_rc apt_rc=$apt_rc rounds=$rounds"
    echo "work_logs=$WORK/claude-r6-round{1,2}.log"
    echo "=== log ==="
    cat "$LOG"
} >/dev/console 2>&1
echo DAILY_DONE=ok >/dev/console
