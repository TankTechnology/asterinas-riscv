#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Round-5 Claude Code rounds in the guest (as the normal asterinas user):
#   1. a real C++ project built with CMake + make -j4 and its tests,
#   2. a Python package's pytest suite,
#   3. an HTTP server + client over loopback with 100 requests.
#
# Per-round logs and load/memory samples go to the console and /home.
#
# Markers: r5_* and DAILY_DONE=ok.

set -u
export PATH=/opt/claude-code/bin:/home/asterinas/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root

printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true

LOG=/tmp/claude-r5.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now() { cut -d' ' -f1 /proc/uptime; }
memavail() { awk '/^MemAvailable:/{print $2}' /proc/meminfo; }
loadavg() { cut -d' ' -f1 /proc/loadavg; }

CLAUDE=/opt/claude-code/bin/claude
WORK=/home/asterinas/work5

log "claude_r5 start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

timeout 240 apt-get update >/tmp/r5-apt-update.log 2>&1
apt_update_rc=$?
timeout 420 apt-get install -y git cmake make g++ python3-pip python3-venv >/tmp/r5-apt.log 2>&1
apt_rc=$?
log "r5_apt update_rc=$apt_update_rc install_rc=$apt_rc git=$(git --version 2>&1 | head -1) cmake=$(cmake --version 2>&1 | head -1) gxx=$(g++ --version 2>&1 | head -1)"

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
            >"$WORK/claude-r5-round$n.log" 2>&1
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
    bytes=$(wc -c <"$WORK/claude-r5-round$n.log" 2>/dev/null || echo 0)
    log "r5_round$n rc=$rc dur=${rdur}s bytes=$bytes max_load=$max_load min_memavail_kb=$min_mem"
    log "r5_round${n}_head=$(head -4 "$WORK/claude-r5-round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 320)"
    log "r5_round${n}_tail=$(tail -12 "$WORK/claude-r5-round$n.log" 2>/dev/null | tr '\n' ' ' | head -c 700)"
    rounds=$((rounds + 1))
}

if [ "$orc" -eq 0 ]; then
    run_round 1 "在当前环境里做一件真实的 C++ 开发工作：把 fmtlib/fmt（或同级规模的项目）克隆到 /home/asterinas/work5/cppproj，用 cmake -DCMAKE_BUILD_TYPE=Release 配置并用 make -j4 构建，然后运行项目自带测试（如 ctest 或 make test）。要求真的执行命令而不是只写计划；遇到环境问题先自己解决，解决不了如实写阻塞点。最后 10 行内报告：项目/版本、构建结果、测试结果、第一条真实错误与处理。" 1200
    run_round 2 "在本机用起来一个真实的 Python 项目测试套件：先 python3 -m pip install --no-cache-dir --break-system-packages pytest requests，然后把 psf/requests 或 pallets/click 克隆到 /home/asterinas/work5/pyproj 并运行 pytest -q。要求记录通过/失败/跳过数；如果依赖缺失，先尝试安装能装的依赖；解决不了就如实写阻塞点。最后 10 行内报告：项目/版本、安装命令、pytest 结果、第一条真实错误与处理。" 1200
    run_round 3 "写并运行一个真实的网络服务：在 /home/asterinas/work5/netproj 实现一个 HTTP server（socket 监听 127.0.0.1 的端口）和一个 client，client 向 server 发 100 个请求（可并发），要求 100/100 成功并校验响应内容一致；记录总耗时、连接建立/绑定的时延（如果观察到延迟要写出来）。最后 10 行内报告：实现方式、100 请求结果、耗时、第一条真实错误与处理。" 1200
else
    log "claude_r5_rounds=skipped phase1_rc=$orc"
fi

log "claude_r5_rounds_completed=$rounds"
if [ -d "$WORK" ]; then
    log "claude_r5_work_files=$(ls -la "$WORK" 2>/dev/null | tail -n +2 | tr '\n' ';' | head -c 600)"
fi

{
    echo "=== claude-r5 summary ==="
    echo "version_rc=$vrc ok_rc=$orc apt_update_rc=$apt_update_rc apt_rc=$apt_rc rounds=$rounds"
    echo "work_logs=$WORK/claude-r5-round{1,2,3}.log"
    echo "=== log ==="
    cat "$LOG"
} >/dev/console 2>&1
echo DAILY_DONE=ok >/dev/console
