// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <sched.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../common/test.h"

#define NOBODY_UID 65534

FN_TEST(sched_set_permissions)
{
	pid_t parent = getpid();
	pid_t child = TEST_SUCC(fork());
	int status;

	if (child == 0) {
		struct sched_param param = { .sched_priority = 0 };
		int failures = 0;

		if (seteuid(NOBODY_UID) != 0) {
			perror("seteuid");
			_exit(EXIT_FAILURE);
		}

		errno = 0;
		if (sched_setparam(parent, &param) != -1 || errno != EPERM) {
			fprintf(stderr,
				"sched_setparam allowed a cross-user update\n");
			failures++;
		}

		param.sched_priority = 1;
		errno = 0;
		if (sched_setscheduler(0, SCHED_FIFO, &param) != -1 ||
		    errno != EPERM) {
			fprintf(stderr,
				"sched_setscheduler allowed unprivileged FIFO\n");
			failures++;
		}

		_exit(failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE);
	}

	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFEXITED(status) &&
			 WEXITSTATUS(status) == EXIT_SUCCESS);
}
END_TEST()
