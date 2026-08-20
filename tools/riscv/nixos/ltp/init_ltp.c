// SPDX-License-Identifier: MPL-2.0
//
// /init for the LTP syscall gate. PID 1 attaches the serial console, mounts
// pseudo-filesystems, and runs /ltp_runner as a child while reaping any orphaned
// test descendants. It reports the runner outcome, emits the terminal marker,
// and remains alive. The test binaries are dynamically linked against musl
// libc and libltp.so, so the whole suite fits in a ~16 MiB initramfs.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
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

static int wait_for_runner(pid_t runner, int *runner_status) {
    for (;;) {
        int child_status;
        pid_t waited = waitpid(-1, &child_status, 0);
        if (waited < 0 && errno == EINTR)
            continue;
        if (waited < 0)
            return -1;
        if (waited == runner) {
            *runner_status = child_status;
            return 0;
        }
    }
}

static void reap_forever(void) {
    for (;;) {
        int child_status;
        pid_t waited = waitpid(-1, &child_status, 0);
        if (waited >= 0 || errno == EINTR)
            continue;
        (void)pause();
    }
}

int main(void) {
#ifndef SKIP_CONSOLE_ATTACH
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
#endif

#ifndef SKIP_PSEUDO_FS_MOUNTS
    say(">>> LTP init: mounting pseudo-filesystems <<<\n");
    (void)mount("proc", "/proc", "proc", 0, NULL);
    (void)mount("sysfs", "/sys", "sysfs", 0, NULL);
    (void)mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
#endif

    say(">>> LTP init: running " RUNNER_PATH " <<<\n");
    pid_t runner = fork();
    if (runner == 0) {
        char *const argv[] = { RUNNER_PATH, NULL };
        (void)execv(RUNNER_PATH, argv);
        say("init: exec " RUNNER_PATH " failed\n");
        _exit(127);
    }
    if (runner < 0) {
        dprintf(1, "[BROK] LTP runner fork failed: %d\n", errno);
    } else {
        int status;
        if (wait_for_runner(runner, &status) < 0) {
            dprintf(1, "[BROK] LTP runner waitpid failed: %d\n", errno);
        } else if (WIFSIGNALED(status)) {
            dprintf(1, "[BROK] LTP runner terminated by signal %d\n",
                    WTERMSIG(status));
        } else if (!WIFEXITED(status)) {
            say("[BROK] LTP runner ended with an unknown status\n");
        } else if (WEXITSTATUS(status) != 0) {
            dprintf(1, "[BROK] LTP runner exited with status %d\n",
                    WEXITSTATUS(status));
        }
        say(">>> LTP init: runner finished; holding PID 1 <<<\n");
    }
    say("__LTP_GATE_TERMINAL__\n");
    reap_forever();
}
