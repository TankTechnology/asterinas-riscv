// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <linux/sched.h>
#include <linux/sched/types.h>
#include <pthread.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "../../common/test.h"

static pthread_barrier_t worker_barrier;
static pid_t worker_tid;

static void *wait_for_priority_change(void *unused)
{
	worker_tid = syscall(SYS_gettid);
	pthread_barrier_wait(&worker_barrier);
	pthread_barrier_wait(&worker_barrier);
	return NULL;
}

FN_TEST(setpriority_updates_fair_scheduler)
{
	struct sched_attr attr = { 0 };

	TEST_RES(setpriority(PRIO_PROCESS, 0, 5), _ret == 0);
	TEST_RES(getpriority(PRIO_PROCESS, 0), _ret == 5);
	TEST_RES(syscall(SYS_sched_getattr, 0, &attr, sizeof(attr), 0),
		 _ret == 0 && attr.sched_policy == SCHED_NORMAL &&
			 attr.sched_nice == 5);
}
END_TEST()

FN_TEST(setpriority_updates_existing_worker)
{
	pthread_t worker;
	struct sched_attr attr = { 0 };

	CHECK_WITH(pthread_barrier_init(&worker_barrier, NULL, 2), _ret == 0);
	CHECK_WITH(pthread_create(&worker, NULL, wait_for_priority_change,
				  NULL),
		   _ret == 0);
	pthread_barrier_wait(&worker_barrier);
	TEST_RES(setpriority(PRIO_PROCESS, 0, 10), _ret == 0);
	TEST_RES(syscall(SYS_sched_getattr, worker_tid, &attr, sizeof(attr), 0),
		 _ret == 0 && attr.sched_policy == SCHED_NORMAL &&
			 attr.sched_nice == 10);
	pthread_barrier_wait(&worker_barrier);
	CHECK_WITH(pthread_join(worker, NULL), _ret == 0);
	CHECK_WITH(pthread_barrier_destroy(&worker_barrier), _ret == 0);
}
END_TEST()
