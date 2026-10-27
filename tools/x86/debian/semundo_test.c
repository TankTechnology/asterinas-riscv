// SPDX-License-Identifier: MPL-2.0

// System V semaphore SEM_UNDO test.
//
// A child adds 2 with SEM_UNDO (value 5 becomes 7); when it exits the kernel
// must subtract 2 again. A second child subtracts 2 with SEM_UNDO (5 -> 3) and
// its exit must add 2 back. Linux prints:
//
//   child_up_sees=7
//   after_up_exit=5
//   child_down_sees=3
//   after_down_exit=5
//
// Build static and use it as the init of a minimal initramfs:
//
//   cc -static -O2 -o init tools/x86/debian/semundo_test.c
//   (printf '.\ninit\n' | cpio -o -H newc) > semundo.cpio
//   qemu-system-x86_64 -kernel aster-kernel-osdk-bin.elf -initrd semundo.cpio \
//       -append "console=ttyS0 init=/init" ...

#include <stdio.h>
#include <sys/sem.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static int val(int id)
{
    return semctl(id, 0, GETVAL);
}

int main(void)
{
    int id = semget(IPC_PRIVATE, 1, 0600 | IPC_CREAT);
    if (id < 0) {
        perror("semget");
        return 1;
    }
    if (semctl(id, 0, SETVAL, 5) < 0) {
        perror("SETVAL");
        return 1;
    }

    pid_t p = fork();
    if (p == 0) {
        struct sembuf op = { 0, 2, SEM_UNDO };
        if (semop(id, &op, 1) < 0) {
            perror("semop-up");
            _exit(2);
        }
        printf("child_up_sees=%d\n", val(id));
        fflush(stdout);
        _exit(0);
    }
    int st;
    waitpid(p, &st, 0);
    usleep(200000);
    printf("after_up_exit=%d\n", val(id));
    fflush(stdout);

    p = fork();
    if (p == 0) {
        struct sembuf op = { 0, -2, SEM_UNDO };
        if (semop(id, &op, 1) < 0) {
            perror("semop-down");
            _exit(2);
        }
        printf("child_down_sees=%d\n", val(id));
        fflush(stdout);
        _exit(0);
    }
    waitpid(p, &st, 0);
    usleep(200000);
    printf("after_down_exit=%d\n", val(id));
    fflush(stdout);

    semctl(id, 0, IPC_RMID);
    return 0;
}
