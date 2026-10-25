#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Build zlib from the tarball injected at /root/zlib-1.3.1.tar.gz and run its
# own test suite. Prints the key output to /dev/console and keeps a copy in
# /tmp/zlib-workflow.log.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/tmp/zlib-workflow.log
: >"$LOG"
TARBALL=/root/zlib-1.3.1.tar.gz

echo "zlib_workflow start $(date -u '+%Y-%m-%dT%H:%M:%SZ')" | tee -a "$LOG" >/dev/console
if [ ! -f "$TARBALL" ]; then
    echo "zlib_workflow=fail missing_tarball=$TARBALL" | tee -a "$LOG" >/dev/console
    echo DAILY_DONE=ok >/dev/console
    exit 0
fi

cd /tmp || exit 1
rm -rf zlib-1.3.1
tar xzf "$TARBALL" || {
    echo "zlib_workflow=fail untar" | tee -a "$LOG" >/dev/console
    echo DAILY_DONE=ok >/dev/console
    exit 0
}
cd zlib-1.3.1 || exit 1
echo "zlib_version=$(awk '/#define ZLIB_VERSION/{print $3}' zlib.h | tr -d '\"')" | tee -a "$LOG" >/dev/console

./configure --prefix=/tmp/zi >/tmp/zlib-configure.log 2>&1
cfg_rc=$?
echo "configure_rc=$cfg_rc" | tee -a "$LOG" >/dev/console
tail -5 /tmp/zlib-configure.log | tee -a "$LOG" >/dev/console

make -j2 >/tmp/zlib-make.log 2>&1
make_rc=$?
echo "make_rc=$make_rc" | tee -a "$LOG" >/dev/console
tail -15 /tmp/zlib-make.log | tee -a "$LOG" >/dev/console

make test >/tmp/zlib-test.log 2>&1
test_rc=$?
echo "make_test_rc=$test_rc" | tee -a "$LOG" >/dev/console
tail -30 /tmp/zlib-test.log | tee -a "$LOG" >/dev/console
echo "test_ok_lines=$(grep -c 'zlib.*test OK' /tmp/zlib-test.log 2>/dev/null || true)" | tee -a "$LOG" >/dev/console
echo "test_failed_lines=$(grep -c 'test FAILED' /tmp/zlib-test.log 2>/dev/null || true)" | tee -a "$LOG" >/dev/console

{
    echo "=== artifacts ==="
    if command -v file >/dev/null 2>&1; then
        file libz.so.1.3.1 2>&1
    else
        readelf -h libz.so.1.3.1 2>&1 | head -8
    fi
    ldd libz.so.1.3.1 2>&1
} | tee -a "$LOG" >/dev/console

make clean >/dev/null 2>&1
make -j4 >/tmp/zlib-make-j4.log 2>&1
j4_rc=$?
echo "make_j4_rc=$j4_rc" | tee -a "$LOG" >/dev/console
tail -5 /tmp/zlib-make-j4.log | tee -a "$LOG" >/dev/console

{
    echo "=== workflow summary ==="
    cat "$LOG"
} >/dev/console 2>&1
echo DAILY_DONE=ok >/dev/console
