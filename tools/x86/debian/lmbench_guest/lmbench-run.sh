#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Run the pinned native LMBench ALL suite (make results) inside the Debian
# guest. The runtime tree is already installed at /opt/lmbench by the host.
#
# Everything is echoed to the serial console, because the guest root
# filesystem is not a reliable channel for the run artifacts here.

set -x

echo "LMBENCH_RUN_START"
date

test -f /opt/lmbench/src/GNUmakefile && echo "LMBENCH_RUN_RUNTIME_OK" || {
    echo "LMBENCH_RUN_FAIL no-runtime"
    exit 1
}

getent passwd rpc >/dev/null 2>&1 ||
    useradd --system --no-create-home --shell /usr/sbin/nologin rpc
getent passwd rpc && echo "LMBENCH_RUN_RPC_USER_OK"

# Quiesce what this kernel cannot run: logind and rsyslog restart in a tight
# loop and add scheduling jitter to the timing-sensitive RPC and signal
# measurements.
for unit in systemd-logind.service rsyslog.service; do
    systemctl stop "$unit" 2>/dev/null
    systemctl mask "$unit" 2>/dev/null
done
echo "LMBENCH_RUN_QUIESCED $(systemctl is-system-running 2>/dev/null)"

echo "----- wildcard_bind_loopback regression test -----"
/opt/wildcard_bind_loopback 2>&1 | tail -40
echo "LMBENCH_RUN_WILDCARD_RC $(/opt/wildcard_bind_loopback >/dev/null 2>&1; echo $?)"

echo "LMBENCH_RUN_NATIVE_START $(date +%s)"
. /opt/lmbench/activate
cd /opt/lmbench/src || exit 1
make results
status=$?
echo "LMBENCH_RUN_NATIVE_EXIT $status $(date +%s)"

for d in /opt/lmbench/asterinas-runs/run-*; do
    [ -d "$d" ] || continue
    echo "LMBENCH_RUN_DUMP_BEGIN $d"
    echo "----- CONFIG -----"
    cat "$d/CONFIG" 2>/dev/null
    echo "----- stderr -----"
    tail -40 "$d/stderr" 2>/dev/null
    echo "----- native-results.txt (full) -----"
    cat "$d/native-results.txt" 2>/dev/null
    echo "LMBENCH_RUN_DUMP_END $d"
done

mkdir -p /opt/lmbench-artifacts
cp -a /opt/lmbench/asterinas-runs/. /opt/lmbench-artifacts/ 2>/dev/null
sync
echo "LMBENCH_RUN_DONE"
exit $status
