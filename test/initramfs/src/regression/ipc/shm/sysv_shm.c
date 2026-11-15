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
#define PERMISSION_KEY 0x5a110001
#define UNPRIVILEGED_ID 65534

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

/*
 * Firefox creates its MIT-SHM image in this order: shmget, shmat,
 * IPC_RMID, and then asks the already-running X server to shmat the same ID.
 * Linux keeps a removed segment alive and attachable until the last shmdt.
 */
FN_TEST(shmat_accepts_segment_marked_for_deletion)
{
	int descriptors[2];
	pid_t child;
	int shmid;
	int *data;
	struct shmid_ds ds;

	TEST_SUCC(pipe(descriptors));
	child = TEST_SUCC(fork());
	if (child == 0) {
		int child_shmid;
		int *child_data;

		close(descriptors[1]);
		if (read(descriptors[0], &child_shmid, sizeof(child_shmid)) !=
		    (ssize_t)sizeof(child_shmid))
			_exit(1);
		close(descriptors[0]);

		child_data = shmat(child_shmid, NULL, 0);
		if (child_data == (void *)-1)
			_exit(2);
		if (*child_data != 0x1234)
			_exit(3);
		*child_data = 0x5678;
		if (shmdt(child_data) != 0)
			_exit(4);
		_exit(0);
	}

	TEST_SUCC(close(descriptors[0]));
	shmid = TEST_SUCC(create_segment());
	data = TEST_SUCC(shmat(shmid, NULL, 0));
	TEST_SUCC(*data = 0x1234);
	TEST_SUCC(remove_segment(shmid));
	/* A rejected attach must not retain a phantom attachment count. */
	TEST_ERRNO(shmat(shmid, (void *)1, 0), EINVAL);
	TEST_SUCC(shmctl(shmid, IPC_STAT, &ds));
	TEST_RES(ds.shm_perm.mode, (_ret & SHM_DEST) != 0);
	TEST_RES(ds.shm_nattch, _ret == 1);
	TEST_RES(write(descriptors[1], &shmid, sizeof(shmid)),
		 _ret == (ssize_t)sizeof(shmid));
	TEST_SUCC(close(descriptors[1]));

	{
		int status;
		TEST_RES(waitpid(child, &status, 0),
			 WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}
	TEST_RES(*data, _ret == 0x5678);
	TEST_SUCC(shmdt(data));
	TEST_ERRNO(shmctl(shmid, IPC_STAT, &ds), EINVAL);
}
END_TEST()

/* Regression coverage for TankTechnology/asterinas-riscv#18. */
FN_TEST(shm_permissions_reject_unprivileged_access)
{
	int shmid = TEST_SUCC(
		shmget(PERMISSION_KEY, SEG_SIZE, IPC_CREAT | IPC_EXCL | 0600));
	pid_t child = TEST_SUCC(fork());

	if (child == 0) {
		struct shmid_ds ds;
		void *addr;

		if (setresgid(UNPRIVILEGED_ID, UNPRIVILEGED_ID,
			      UNPRIVILEGED_ID) != 0 ||
		    setresuid(UNPRIVILEGED_ID, UNPRIVILEGED_ID,
			      UNPRIVILEGED_ID) != 0)
			_exit(1);

		/* Looking up an ID requests no access unless mode bits are set. */
		if (shmget(PERMISSION_KEY, SEG_SIZE, 0) != shmid)
			_exit(2);

		errno = 0;
		if (shmget(PERMISSION_KEY, SEG_SIZE, 0400) != -1 ||
		    errno != EACCES)
			_exit(3);

		errno = 0;
		addr = shmat(shmid, NULL, SHM_RDONLY);
		if (addr != (void *)-1) {
			shmdt(addr);
			_exit(4);
		}
		if (errno != EACCES)
			_exit(5);

		errno = 0;
		addr = shmat(shmid, NULL, 0);
		if (addr != (void *)-1) {
			shmdt(addr);
			_exit(6);
		}
		if (errno != EACCES)
			_exit(7);

		errno = 0;
		if (shmctl(shmid, IPC_STAT, &ds) != -1 || errno != EACCES)
			_exit(8);

		errno = 0;
		if (shmctl(shmid, IPC_RMID, NULL) != -1 || errno != EPERM)
			_exit(9);

		_exit(0);
	}

	{
		int status;
		TEST_RES(waitpid(child, &status, 0),
			 WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}
	TEST_SUCC(remove_segment(shmid));
}
END_TEST()

FN_TEST(shm_remove_accepts_cap_sys_admin)
{
	int descriptors[2];
	int shmid = -1;
	pid_t child;
	ssize_t bytes;

	TEST_SUCC(pipe(descriptors));
	child = TEST_SUCC(fork());
	if (child == 0) {
		int child_shmid;

		close(descriptors[0]);
		if (setresgid(UNPRIVILEGED_ID, UNPRIVILEGED_ID,
			      UNPRIVILEGED_ID) != 0 ||
		    setresuid(UNPRIVILEGED_ID, UNPRIVILEGED_ID,
			      UNPRIVILEGED_ID) != 0)
			_exit(1);
		child_shmid = shmget(IPC_PRIVATE, SEG_SIZE, IPC_CREAT | 0600);
		if (child_shmid < 0 ||
		    write(descriptors[1], &child_shmid, sizeof(child_shmid)) !=
			    (ssize_t)sizeof(child_shmid))
			_exit(2);
		close(descriptors[1]);
		_exit(0);
	}

	TEST_SUCC(close(descriptors[1]));
	bytes = TEST_SUCC(read(descriptors[0], &shmid, sizeof(shmid)));
	TEST_RES(bytes, _ret == (ssize_t)sizeof(shmid));
	TEST_SUCC(close(descriptors[0]));
	{
		int status;
		TEST_RES(waitpid(child, &status, 0),
			 WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}
	if (shmid >= 0)
		TEST_SUCC(remove_segment(shmid));
}
END_TEST()
