#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

/test/fs/quotactl/quotactl
/test/fs/readahead/readahead
echo "ASTERINAS_FS_SYSCALL_COMPAT_OK readahead=5 quotactl=1"
