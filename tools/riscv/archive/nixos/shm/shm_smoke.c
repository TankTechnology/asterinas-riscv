// SPDX-License-Identifier: MPL-2.0
//
// System V shared memory smoke test for the Asterinas RISC-V track.
//
// Exercises the three-syscall System V shm surface (shmget/shmat/shmctl plus
// shmdt) and, most importantly, proves that two processes can share the same
// segment: the parent attaches, forks, the child attaches the same shmid and
// writes through its own mapping, and the parent observes the write.

#define _GNU_SOURCE
#include <errno.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <sys/wait.h>
#include <unistd.h>

static int fails;

static void say(const char *s)
{
	(void)write(1, s, strlen(s));
}

static void say_line(const char *s)
{
	say(s);
	say("\n");
}

#define CHECK(cond, msg)                                             \
	do {                                                         \
		if (cond) {                                          \
			say_line("OK: " msg);                        \
		} else {                                             \
			say_line("FAIL: " msg);                      \
			fails++;                                     \
		}                                                    \
	} while (0)

int main(void)
{
	int shmid;
	struct shmid_ds ds;
	int *data;
	pid_t child;
	int status;

	shmid = shmget(IPC_PRIVATE, 4096, IPC_CREAT | 0600);
	CHECK(shmid >= 0, "shmget(IPC_PRIVATE)");

	memset(&ds, 0, sizeof(ds));
	CHECK(shmctl(shmid, IPC_STAT, &ds) == 0, "shmctl(IPC_STAT)");
	CHECK(ds.shm_segsz == 4096, "shm_segsz == 4096");
	CHECK(ds.shm_nattch == 0, "shm_nattch == 0 (before attach)");

	data = shmat(shmid, NULL, 0);
	CHECK(data != (void *)-1, "shmat(NULL)");
	*data = 0;

	child = fork();
	if (child == 0) {
		int *cdata = shmat(shmid, NULL, 0);
		if (cdata == (void *)-1)
			_exit(1);
		if (*cdata != 0)
			_exit(2);
		*cdata = 0xdeadbeef;
		shmdt(cdata);
		_exit(0);
	}

	CHECK(child > 0, "fork()");
	waitpid(child, &status, 0);
	CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0,
	      "child shmat + write + shmdt");

	CHECK(*data == 0xdeadbeef, "parent observes child write (shared)");

	memset(&ds, 0, sizeof(ds));
	shmctl(shmid, IPC_STAT, &ds);
	CHECK(ds.shm_nattch == 1, "shm_nattch == 1 (after child detach)");

	CHECK(shmdt(data) == 0, "shmdt(parent)");
	CHECK(shmctl(shmid, IPC_RMID, NULL) == 0, "shmctl(IPC_RMID)");

	if (fails) {
		say_line("___SYSV_SHM_FAIL__");
		return 1;
	}
	say_line("__SYSV_SHM_OK__");
	return 0;
}
