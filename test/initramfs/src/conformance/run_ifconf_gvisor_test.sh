#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

export TEST_TMPDIR=/tmp
cd /opt/gvisor/tests
result=/tmp/ifconf_gvisor_test.log
if ! ./ioctl_test --gtest_filter=IoctlTest/IoctlTestSIOCGIFCONF.* --gtest_brief=1 >"$result" 2>&1; then
    cat "$result"
    exit 1
fi
cat "$result"
grep -Fq '[==========] 24 tests from 1 test suite ran.' "$result"
grep -Fxq '[  PASSED  ] 24 tests.' "$result"
echo "gVisor SIOCGIFCONF cases passed."
