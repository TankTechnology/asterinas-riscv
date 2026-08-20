// SPDX-License-Identifier: MPL-2.0
//
// /init for the LTP syscall gate. Runs as pid 1: attaches the serial console,
// best-effort mounts the proc/sys/tmp pseudo-filesystems, then execs the static
// /ltp_runner. The test binaries are dynamically linked against musl libc and
// libltp.so, so the whole suite fits in a ~16 MiB initramfs (the static
// glibc/musl builds would either exceed the kernel's large-initramfs limit or
// need a second block device, both blocked — see FOUNDATION-M2-report.md).

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef RUNNER_PATH
#define RUNNER_PATH "/ltp_runner"
#endif

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

int main(void) {
    int fd = open("/dev/console", O_RDWR);
    if (fd < 0)
        fd = open("/dev/ttyS0", O_RDWR);
    if (fd >= 0) {
        (void)dup2(fd, 0);
        (void)dup2(fd, 1);
        (void)dup2(fd, 2);
        if (fd > 2)
            (void)close(fd);
    }

    say(">>> LTP init: mounting pseudo-filesystems <<<\n");
    (void)mount("proc", "/proc", "proc", 0, NULL);
    (void)mount("sysfs", "/sys", "sysfs", 0, NULL);
    (void)mount("tmpfs", "/tmp", "tmpfs", 0, NULL);

    say(">>> LTP init: running " RUNNER_PATH " <<<\n");
    pid_t runner = fork();
    if (runner == 0) {
        char *const argv[] = { RUNNER_PATH, NULL };
        (void)execv(RUNNER_PATH, argv);
        say("init: exec " RUNNER_PATH " failed\n");
        _exit(127);
    }
    if (runner < 0) {
        say("init: fork for " RUNNER_PATH " failed\n");
    } else {
        int status;
        while (waitpid(runner, &status, 0) < 0 && errno == EINTR) {
        }
        say(">>> LTP init: runner finished; holding PID 1 <<<\n");
    }
    for (;;)
        (void)pause();
    return 0;
}
