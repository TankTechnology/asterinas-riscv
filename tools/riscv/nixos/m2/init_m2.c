// SPDX-License-Identifier: MPL-2.0

/* M2 /init (static glibc): mount /proc, run the dynamically linked musl
 * hello_dyn, then fall back to busybox sh for the final marker. */
#include <stdio.h>
#include <stdlib.h>
#include <sys/mount.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void)
{
    puts(">>> M2 init: dynamic musl smoke <<<");

    mount("proc", "/proc", "proc", 0, NULL);
    mount("sysfs", "/sys", "sysfs", 0, NULL);

    pid_t pid = fork();
    if (pid == 0) {
        execl("/bin/hello_dyn", "hello_dyn", NULL);
        perror("execl hello_dyn");
        _exit(127);
    }
    int st = 0;
    waitpid(pid, &st, 0);

    execl("/bin/busybox", "sh", "-c",
          "echo __M2_SHELL_OK__; ls /lib", NULL);
    perror("execl busybox");
    return 1;
}
