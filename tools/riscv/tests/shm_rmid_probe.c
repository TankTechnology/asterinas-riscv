// SPDX-License-Identifier: MPL-2.0
//
// SysV shared-memory lifecycle probe matching Linux's IPC_RMID semantics:
// the ID stays valid while the segment is still attached, repeated IPC_RMID
// succeeds (idempotent), and the ID disappears after the last detach.
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <unistd.h>

static int failures;

static void check(const char *what, int ok, int err) {
    printf("SHM_PROBE %-28s %s errno=%d\n", what, ok ? "ok" : "FAIL", err);
    fflush(stdout);
    if (!ok)
        failures++;
}

static int stat_ok(int id) {
    struct shmid_ds ds;
    errno = 0;
    return shmctl(id, IPC_STAT, &ds) == 0;
}

int main(void) {
    errno = 0;
    int id = shmget(IPC_PRIVATE, 4096, IPC_CREAT | 0666);
    check("shmget", id >= 0, errno);
    if (id < 0)
        goto out;

    void *p = shmat(id, NULL, 0);
    check("shmat", p != (void *)-1, errno);

    errno = 0;
    check("rmid-first", shmctl(id, IPC_RMID, NULL) == 0, errno);
    check("stat-after-rmid-attached", stat_ok(id), errno);

    errno = 0;
    check("rmid-second", shmctl(id, IPC_RMID, NULL) == 0, errno);

    /* Same key after removal must create a fresh segment. ID reuse is
     * allowed: this kernel's ID allocator hands out the lowest free ID,
     * unlike Linux's cyclic allocation. */
    int key = 0x5a5a;
    int a = shmget(key, 4096, IPC_CREAT | 0666);
    check("shmget-key-first", a >= 0, errno);
    errno = 0;
    check("rmid-key", shmctl(a, IPC_RMID, NULL) == 0, errno);
    errno = 0;
    int b = shmget(key, 4096, IPC_CREAT | 0666);
    check("shmget-key-after-rmid", b >= 0, errno);
    shmctl(b, IPC_RMID, NULL);

    if (p != (void *)-1) {
        errno = 0;
        check("shmdt", shmdt(p) == 0, errno);
    }
    errno = 0;
    check("rmid-after-last-detach", shmctl(id, IPC_RMID, NULL) == -1, errno);

out:
    printf("SHM_PROBE failures=%d\n", failures);
    fflush(stdout);
    for (;;)
        pause();
}
