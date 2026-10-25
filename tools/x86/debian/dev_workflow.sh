#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Developer workflow probe: git clone (with a local bare fallback), a C++17
# program with std::thread, pip install/import, and zlib's configure/make/test
# from the cloned repository. Every result and the first real error is printed
# to /dev/console and kept in /tmp/dev-workflow.log.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/tmp/dev-workflow.log
: >"$LOG"
log() { echo "$*" | tee -a "$LOG" >/dev/console; }
now() { cut -d' ' -f1 /proc/uptime; }

# QEMU's user-mode network DNS; this image was built with 1.1.1.1.
printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true
log "dev_workflow start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# --- packages --------------------------------------------------------------
timeout 240 apt-get update >/tmp/dev-apt-update.log 2>&1
apt_update_rc=$?
timeout 300 apt-get install -y git python3-pip >/tmp/dev-apt.log 2>&1
apt_rc=$?
log "apt update_rc=$apt_update_rc install_rc=$apt_rc"
if [ "$apt_rc" -ne 0 ]; then
    log "apt_first_error $(head -2 /tmp/dev-apt.log | tr '\n' '_' | head -c 160)"
fi

# --- A: git ----------------------------------------------------------------
log "git_version=$(git --version 2>&1)"
rm -rf /tmp/zlib-git
t0=$(now)
timeout 180 git clone --depth 1 https://github.com/madler/zlib /tmp/zlib-git >/tmp/dev-git-clone.log 2>&1
clone_rc=$?
dur=$(awk -v a="$t0" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}')
clone_src=github
if [ "$clone_rc" -ne 0 ]; then
    log "git_clone rc=$clone_rc dur=${dur}s src=github err=$(head -2 /tmp/dev-git-clone.log | tr '\n' '_' | head -c 180)"
    rm -rf /tmp/zlib-local /tmp/zlib-bare
    mkdir -p /tmp/zlib-local
    tar xzf /root/zlib-1.3.1.tar.gz -C /tmp/zlib-local
    (
        cd /tmp/zlib-local/zlib-1.3.1 || exit 1
        git init -q
        git add -A
        git -c user.email=dev@example.invalid -c user.name=dev commit -qm init
        git clone --bare . /tmp/zlib-bare
    ) >/tmp/dev-git-local.log 2>&1
    local_rc=$?
    rm -rf /tmp/zlib-git
    timeout 60 git clone /tmp/zlib-bare /tmp/zlib-git >/tmp/dev-git-clone2.log 2>&1
    clone_rc=$?
    clone_src=local-bare
    log "git_local_bare rc=$local_rc clone_rc=$clone_rc err=$(head -2 /tmp/dev-git-local.log | tr '\n' '_' | head -c 160)"
else
    log "git_clone rc=0 dur=${dur}s src=github"
fi
if [ -d /tmp/zlib-git/.git ]; then
    cd /tmp/zlib-git || exit 1
    head=$(git log --oneline -1 2>&1)
    dirty=$(git status --porcelain 2>/dev/null | wc -l)
    size=$(du -sh /tmp/zlib-git 2>/dev/null | cut -f1)
    log "git_log=$head src=$clone_src size=$size dirty_files=$dirty"
    cd /tmp
else
    log "git_repo=missing src=$clone_src"
fi

# --- B: C++ with threads ---------------------------------------------------
cat >/tmp/t.cpp <<'CPP'
#include <vector>
#include <thread>
#include <numeric>
#include <cstdio>
int main(){std::vector<int> v(1000,1);int s=0;std::thread t([&]{s=std::accumulate(v.begin(),v.end(),0);});t.join();printf("%d\n",s);}
CPP
g++ -O2 -std=c++17 -pthread /tmp/t.cpp -o /tmp/t >/tmp/dev-gpp.log 2>&1
gpp_rc=$?
out=none
[ "$gpp_rc" -eq 0 ] && out=$(/tmp/t 2>&1)
log "cpp gpp_rc=$gpp_rc run_out=$out err=$(head -2 /tmp/dev-gpp.log | tr '\n' '_' | head -c 160)"

# --- C: pip ----------------------------------------------------------------
log "pip_version=$(python3 -m pip --version 2>&1)"
timeout 180 python3 -m pip install --no-cache-dir six >/tmp/dev-pip-six.log 2>&1
pip_rc=$?
pip_src=pypi
if [ "$pip_rc" -ne 0 ]; then
    log "pip_six rc=$pip_rc err=$(head -3 /tmp/dev-pip-six.log | tr '\n' '_' | head -c 220)"
    # Retry with the PEP 668 override; then a local wheel if the network fails.
    timeout 180 python3 -m pip install --no-cache-dir --break-system-packages six >/tmp/dev-pip-six2.log 2>&1
    pip_rc=$?
    pip_src=pypi-break-system
    if [ "$pip_rc" -ne 0 ]; then
        log "pip_six_retry rc=$pip_rc err=$(head -2 /tmp/dev-pip-six2.log | tr '\n' '_' | head -c 180)"
        wheel=$(ls /root/six-*.whl 2>/dev/null | head -1)
        if [ -n "$wheel" ]; then
            timeout 120 python3 -m pip install --no-cache-dir --no-index --break-system-packages "$wheel" >/tmp/dev-pip-six-local.log 2>&1
            pip_rc=$?
            pip_src=local-wheel
            log "pip_six_local rc=$pip_rc err=$(head -2 /tmp/dev-pip-six-local.log | tr '\n' '_' | head -c 160)"
        fi
    fi
fi
import_out=$(python3 -c 'import six; print("six_version="+six.__version__)' 2>&1)
import_rc=$?
log "pip_six src=$pip_src rc=$pip_rc import_rc=$import_rc import_out=$import_out"
if [ "$import_rc" -eq 0 ]; then
    timeout 180 python3 -m pip install --no-cache-dir --break-system-packages click >/tmp/dev-pip-click.log 2>&1
    click_rc=$?
    click_out=$(python3 -c 'import click; print("click_version="+click.__version__)' 2>&1)
    click_import=$?
    log "pip_click rc=$click_rc import_rc=$click_import out=$click_out err=$(head -2 /tmp/dev-pip-click.log | tr '\n' '_' | head -c 140)"
fi

# --- D: zlib configure/make/test from the repo -----------------------------
zlib_ok=no
if [ -d /tmp/zlib-git/.git ]; then
    cd /tmp/zlib-git || exit 1
    ./configure >/tmp/dev-zlib-cfg.log 2>&1
    cfg_rc=$?
    make -j2 >/tmp/dev-zlib-make.log 2>&1
    mk_rc=$?
    make test >/tmp/dev-zlib-test.log 2>&1
    test_rc=$?
    ok_lines=$(grep -c 'zlib.*test OK' /tmp/dev-zlib-test.log 2>/dev/null || true)
    if [ "$cfg_rc" -eq 0 ] && [ "$mk_rc" -eq 0 ] && [ "$test_rc" -eq 0 ]; then
        zlib_ok=yes
    fi
    log "zlib cfg_rc=$cfg_rc make_rc=$mk_rc test_rc=$test_rc ok_lines=$ok_lines result=$zlib_ok err=$(head -2 /tmp/dev-zlib-cfg.log | tr '\n' '_' | head -c 140)"
    cd /tmp
else
    log "zlib skipped: no repo"
fi

{
    echo "=== dev-workflow summary ==="
    echo "apt_update_rc=$apt_update_rc apt_install_rc=$apt_rc"
    echo "git_clone_src=$clone_src git_clone_rc=$clone_rc"
    echo "cpp gpp_rc=$gpp_rc out=$out"
    echo "pip six_src=$pip_src six_rc=$pip_rc import_rc=$import_rc out=$import_out"
    echo "zlib result=$zlib_ok"
    echo "=== log ==="
    cat "$LOG"
} >/dev/console 2>&1
echo DAILY_DONE=ok >/dev/console
