// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <linux/quota.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "../../common/test.h"

FN_TEST(quotactl_reports_unsupported)
{
	TEST_ERRNO(syscall(SYS_quotactl, QCMD(Q_GETFMT, USRQUOTA), "/", 0,
			   NULL),
		   ENOSYS);
}
END_TEST()
