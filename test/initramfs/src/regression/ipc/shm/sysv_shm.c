// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../common/test.h"

#define SEG_SIZE 0x1000
#define CUSTOM_KEY 0xdeadbeef

static int create_segment(void)
{
	return shmget(IPC_PRIVATE, SEG_SIZE, IPC_CREAT | 0600);
}

static int remove_segment(int shmid)
{
	return shmctl(shmid, IPC_RMID, NULL);
}

FN_TEST(shmget_rejects_bad_size)
{
	TEST_ERRNO(shmget(IPC_PRIVATE, 0, IPC_CREAT | 0600), EINVAL);
}
END_TEST()

FN_TEST(shmget_accepts_arbitrary_keys)
{
	int shmid = TEST_SUCC(shmget(CUSTOM_KEY, SEG_SIZE, IPC_CREAT | 0600));
	int shmid2;

	TEST_RES(shmget(CUSTOM_KEY, SEG_SIZE, 0), _ret == shmid);
	TEST_ERRNO(shmget(CUSTOM_KEY, SEG_SIZE, IPC_CREAT | IPC_EXCL | 0600),
		   EEXIST);
	TEST_ERRNO(shmget(CUSTOM_KEY, SEG_SIZE * 2, 0), EINVAL);
	TEST_ERRNO(shmget(CUSTOM_KEY + 1, SEG_SIZE, 0), ENOENT);

	shmid2 = TEST_SUCC(shmget(CUSTOM_KEY + 1, SEG_SIZE, IPC_CREAT | 0600));
	TEST_RES(shmid2, _ret != shmid);

	TEST_SUCC(remove_segment(shmid));
	TEST_SUCC(remove_segment(shmid2));
}
END_TEST()

FN_TEST(shmat_and_shmdt)
{
	int shmid = TEST_SUCC(create_segment());
	int *data = TEST_SUCC(shmat(shmid, NULL, 0));

	TEST_SUCC(*data = 0x1234);
	TEST_RES(*data, _ret == 0x1234);

	TEST_SUCC(shmdt(data));
	TEST_SUCC(remove_segment(shmid));
}
END_TEST()

FN_TEST(shmctl_ipc_stat)
{
	struct shmid_ds ds;
	int shmid = TEST_SUCC(create_segment());

	TEST_SUCC(shmctl(shmid, IPC_STAT, &ds));
	TEST_RES(ds.shm_segsz, _ret == SEG_SIZE);
	TEST_RES(ds.shm_nattch, _ret == 0);
	TEST_RES(ds.shm_cpid, _ret == (unsigned long)getpid());

	TEST_SUCC(remove_segment(shmid));
}
END_TEST()

FN_TEST(shmctl_rejects_bad_shmid)
{
	TEST_ERRNO(shmctl(-1, IPC_STAT, NULL), EINVAL);
	TEST_ERRNO(shmctl(-1, IPC_RMID, NULL), EINVAL);
}
END_TEST()

FN_TEST(shm_shared_between_processes)
{
	int shmid = TEST_SUCC(create_segment());
	int *data = TEST_SUCC(shmat(shmid, NULL, 0));
	pid_t child;

	TEST_SUCC(*data = 0);
	child = TEST_SUCC(fork());

	if (child == 0) {
		int *cdata = shmat(shmid, NULL, 0);
		if (cdata == (void *)-1)
			_exit(1);
		if (*cdata != 0)
			_exit(2);
		*cdata = 0xdead;
		shmdt(cdata);
		_exit(0);
	}

	{
		int status;
		TEST_RES(waitpid(child, &status, 0),
			 WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}
	TEST_RES(*data, _ret == 0xdead);

	TEST_SUCC(shmdt(data));
	TEST_SUCC(remove_segment(shmid));
}
END_TEST()
